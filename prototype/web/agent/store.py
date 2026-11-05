#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
执行轨迹与人工确认的持久化层（agent/store.py）
================================================================
三张表，对应三个问题：

  agent_runs     一次提问 = 一次运行（run）。解决「这次问答用了多少轮、最后什么状态」
  agent_steps    每次决策/工具调用一行。解决「模型每一步做了什么、花了多久、结果如何」
  agent_actions  待人工确认的写操作。解决「中断后浏览器刷新/用户走开，怎么恢复」

为什么不能只塞进 messages.tool_trace（JSON 字段）：
  1. 轨迹质量分析要按「轮次 / 工具 / 耗时 / 阻断」做聚合与排序，JSON 没法建索引；
  2. 人工确认必须跨 HTTP 请求存活——/chat 的 SSE 流已经结束，确认发生在几分钟后的
     另一个请求里，只有落库才能把两段串起来；
  3. 一次问答会被中断拆成两个请求，必须有一个稳定的 run_id 作为关联键。

表结构刻意保持「宽表 + 冗余」：宁可多存几个摘要字段，也不要在做分析时反复回表。
"""

import json

import db

# 轨迹中用于标记「模型决策」这一行的伪工具名（不是真实工具）
MODEL_STEP = "__model__"


# ============================================================
# 建表（幂等）
# ============================================================

def init_tables():
    """创建三张表。由 db.init_db() 在启动时调用，重复执行无副作用。"""
    with db.get_db() as conn:
        conn.executescript("""
        -- ① 一次问答运行
        CREATE TABLE IF NOT EXISTS agent_runs (
            id                TEXT PRIMARY KEY,     -- run_id（12 位十六进制）
            session_id        TEXT NOT NULL,
            user_id           INTEGER,
            store_id          TEXT,
            question          TEXT,
            status            TEXT DEFAULT 'running',
                              -- running / waiting_confirm / done / cancelled
                              -- / failed / rounds_exhausted
            round_used        INTEGER DEFAULT 0,    -- 已消耗的决策轮次
            max_rounds        INTEGER DEFAULT 5,    -- 轮次上限（可配置，便于 A/B）
            messages          TEXT,                 -- 中断时的消息快照（JSON）
            pending_action_id INTEGER,              -- 当前挂起的人工确认卡
            answer            TEXT,                 -- 最终回答
            created_at        TEXT,
            updated_at        TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_runs_session ON agent_runs(session_id);
        CREATE INDEX IF NOT EXISTS idx_runs_status  ON agent_runs(status);

        -- ② 轨迹明细：一行 = 一次模型决策 或 一次工具调用
        CREATE TABLE IF NOT EXISTS agent_steps (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id         TEXT NOT NULL,
            round_no       INTEGER NOT NULL,        -- 第几轮（从 1 开始）
            step_no        INTEGER NOT NULL,        -- 轮内序号；0 = 本轮模型决策
            tool           TEXT NOT NULL,           -- 工具名；模型决策行固定为 __model__
            args           TEXT,                    -- 入参 JSON（审计可复现）
            decision       TEXT,                    -- plan / final_answer / tool_call
                                                    -- / interrupt / confirmed / cancelled / skipped
            ok             INTEGER DEFAULT 1,       -- 是否正常执行
            blocking       INTEGER DEFAULT 0,       -- 是否命中业务停止条件
            code           TEXT,                    -- 错误码（TOKEN_INVALID 等）
            duration_ms    INTEGER DEFAULT 0,       -- 决策耗时 / 工具耗时（毫秒）
            result_summary TEXT,                    -- 结果摘要（前 300 字，足够人工扫一眼）
            created_at     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_steps_run ON agent_steps(run_id, round_no, step_no);

        -- ③ 人工确认卡（写操作执行前的挂起记录）
        CREATE TABLE IF NOT EXISTS agent_actions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id        TEXT NOT NULL,
            session_id    TEXT,
            tool          TEXT NOT NULL,            -- 被挂起的工具（目前只有建草稿）
            args          TEXT,                     -- 工具入参（确认后原样执行）
            tool_call_id  TEXT,                     -- 对应的 tool_call id（回填消息要用）
            preview       TEXT,                     -- 给店长看的预览（SKU/数量/整箱换算/配送日）
            status        TEXT DEFAULT 'pending',   -- pending / confirmed / cancelled / expired
            created_at    TEXT,
            decided_at    TEXT,
            decided_by    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_actions_run ON agent_actions(run_id);
        CREATE INDEX IF NOT EXISTS idx_actions_pending ON agent_actions(session_id, status);
        """)


# ============================================================
# agent_runs
# ============================================================

def create_run(run_id: str, session_id: str, user_id, store_id: str,
               question: str, max_rounds: int, messages: list) -> None:
    """新建一次运行。messages 先落一份快照，便于中断后恢复上下文。"""
    now = db.now()
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO agent_runs (id, session_id, user_id, store_id, question, status, "
            "round_used, max_rounds, messages, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, session_id, user_id, store_id, question, "running",
             0, max_rounds, json.dumps(messages, ensure_ascii=False), now, now),
        )


# 允许被外部更新的列（白名单，避免拼接 SQL 时手滑）
_RUN_UPDATABLE = {"status", "round_used", "messages", "pending_action_id", "answer"}


def update_run(run_id: str, **fields) -> None:
    """更新运行状态。messages 传 list 会自动序列化。"""
    sets, params = [], []
    for k, v in fields.items():
        if k not in _RUN_UPDATABLE:
            continue
        if k == "messages" and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        sets.append(f"{k} = ?")
        params.append(v)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.extend([db.now(), run_id])
    with db.get_db() as conn:
        conn.execute(f"UPDATE agent_runs SET {', '.join(sets)} WHERE id = ?", params)


def get_run(run_id: str):
    """按 id 取运行；返回 dict（messages 已反序列化）。"""
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        return None
    run = dict(row)
    run["messages"] = json.loads(run["messages"]) if run.get("messages") else []
    return run


def list_runs(session_id: str = None, limit: int = 50) -> list:
    """轨迹列表 + 质量指标（轮数、工具数、总耗时、阻断次数）。

    指标直接在 SQL 里聚合，页面/分析脚本拿来即用：
      rounds      —— 实际决策轮数（含最终回答那一轮）
      tool_calls  —— 工具调用次数（不含模型决策行）
      blocked     —— 命中停止条件的次数（越多说明越依赖人工介入）
      decision_ms —— 模型决策总耗时
      tool_ms     —— 工具执行总耗时
    """
    sql = """
        SELECT r.id, r.session_id, r.store_id, r.question, r.status,
               r.round_used, r.max_rounds, r.created_at, r.updated_at,
               (SELECT COUNT(*) FROM agent_steps s
                 WHERE s.run_id = r.id
                   AND s.decision IN ('tool_call','interrupt'))            AS tool_calls,
               (SELECT COUNT(*) FROM agent_steps s
                 WHERE s.run_id = r.id AND s.tool = '__model__')           AS rounds,
               (SELECT COUNT(*) FROM agent_steps s
                 WHERE s.run_id = r.id AND s.decision = 'tool_call'
                   AND s.blocking = 1)                                     AS blocked,
               (SELECT IFNULL(SUM(s.duration_ms),0) FROM agent_steps s
                 WHERE s.run_id = r.id AND s.tool = '__model__')           AS decision_ms,
               (SELECT IFNULL(SUM(s.duration_ms),0) FROM agent_steps s
                 WHERE s.run_id = r.id AND s.tool <> '__model__')          AS tool_ms
        FROM agent_runs r
        WHERE 1 = 1
    """
    params = []
    if session_id:
        sql += " AND r.session_id = ?"
        params.append(session_id)
    sql += " ORDER BY r.created_at DESC, r.rowid DESC LIMIT ?"
    params.append(limit)
    with db.get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ============================================================
# agent_steps
# ============================================================

def log_step(run_id: str, round_no: int, step_no: int, *,
             tool: str, decision: str, args: dict = None,
             ok: bool = True, blocking: bool = False, code: str = None,
             duration_ms: int = 0, result_summary: str = "") -> None:
    """写一行轨迹。

    result_summary 截断到 300 字：
      轨迹分析看的是「做了什么、结论是什么」，
      不需要把整段工具返回（可能上千字）都存进来，避免日志表膨胀。
    """
    summary = (result_summary or "").strip()
    if len(summary) > 300:
        summary = summary[:300] + "…"
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO agent_steps (run_id, round_no, step_no, tool, args, decision, "
            "ok, blocking, code, duration_ms, result_summary, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, round_no, step_no, tool,
             json.dumps(args or {}, ensure_ascii=False) if args is not None else None,
             decision, 1 if ok else 0, 1 if blocking else 0, code,
             int(duration_ms or 0), summary, db.now()),
        )


def list_steps(run_id: str) -> list:
    """按轮次/序号返回轨迹明细（args 已反序列化）。"""
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_steps WHERE run_id = ? ORDER BY round_no, step_no, id",
            (run_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["args"] = json.loads(d["args"]) if d.get("args") else {}
        except (TypeError, ValueError):
            d["args"] = {}
        d["ok"] = bool(d["ok"])
        d["blocking"] = bool(d["blocking"])
        out.append(d)
    return out


def trace_quality(run_id: str) -> dict:
    """对单次运行算一组「轨迹质量」指标，供分析页/离线评估使用。

    这些指标不是业务 KPI，而是**判断智能体是否健康**的信号：
      tool_calls      工具调用次数（越多越可能绕路）
      repeats         同一工具+同一参数重复调用次数（>0 = 模型在原地打转）
      blocked         命中停止条件次数（应由人工接管，不是模型错误）
      step_failures   工具失败次数（参数错/编码不存在，说明模型理解有偏差）
      round_efficiency 有效工具调用 / 轮次（越高说明一轮内批量处理做得越好）
    """
    steps = list_steps(run_id)
    calls = [s for s in steps if s["tool"] != MODEL_STEP]
    seen, repeats = set(), 0
    for s in calls:
        # 只统计「模型真的发起过两次相同调用」；中断/确认是同一笔写操作的两条记录，不算重复
        if s["decision"] != "tool_call":
            continue
        key = (s["tool"], json.dumps(s["args"], sort_keys=True, ensure_ascii=False))
        if key in seen:
            repeats += 1
        seen.add(key)
    # 轮次 = 模型决策行数（每轮恰好一行，含最后给出回答的那一轮）
    rounds = len([s for s in steps if s["tool"] == MODEL_STEP]) or 1
    # 只统计「模型发起的真实调用」：中断/确认/取消是同一笔操作的记录，不计入成功率与阻断数
    invoked = [s for s in calls if s["decision"] in ("tool_call", "interrupt")]
    return {
        "run_id": run_id,
        "rounds": rounds,
        "tool_calls": len(invoked),
        "repeats": repeats,
        "blocked": len([s for s in invoked if s["blocking"]]),
        "step_failures": len([s for s in invoked
                              if not s["ok"] and s["decision"] == "tool_call"]),
        "round_efficiency": round(len(calls) / rounds, 2),
        "total_ms": sum(int(s["duration_ms"] or 0) for s in steps),
    }


# ============================================================
# agent_actions（人工确认卡）
# ============================================================

def create_action(run_id: str, session_id: str, tool: str, args: dict,
                  tool_call_id: str, preview: dict) -> dict:
    """挂起一个写操作，等待店长确认。"""
    with db.get_db() as conn:
        cur = conn.execute(
            "INSERT INTO agent_actions (run_id, session_id, tool, args, tool_call_id, "
            "preview, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, session_id, tool, json.dumps(args, ensure_ascii=False),
             tool_call_id, json.dumps(preview, ensure_ascii=False), "pending", db.now()),
        )
        action_id = cur.lastrowid
    return get_action(action_id)


def get_action(action_id: int):
    """取确认卡（preview / args 反序列化）。"""
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM agent_actions WHERE id = ?", (action_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["args"] = json.loads(d["args"]) if d.get("args") else {}
    d["preview"] = json.loads(d["preview"]) if d.get("preview") else {}
    return d


def get_pending_by_session(session_id: str):
    """会话内仍待确认的卡片（页面刷新后恢复用）。"""
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_actions WHERE session_id = ? AND status = 'pending' "
            "ORDER BY id DESC", (session_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["args"] = json.loads(d["args"]) if d.get("args") else {}
        d["preview"] = json.loads(d["preview"]) if d.get("preview") else {}
        out.append(d)
    return out


def decide_action(action_id: int, status: str, operator: str) -> None:
    """记录人工决策结果：confirmed / cancelled / expired。"""
    with db.get_db() as conn:
        conn.execute(
            "UPDATE agent_actions SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
            (status, db.now(), operator, action_id),
        )


def expire_pending(run_id: str) -> int:
    """把一次运行里所有未处理的卡片置为 expired（异常兜底用）。"""
    with db.get_db() as conn:
        cur = conn.execute(
            "UPDATE agent_actions SET status = 'expired', decided_at = ? "
            "WHERE run_id = ? AND status = 'pending'", (db.now(), run_id))
        return cur.rowcount
