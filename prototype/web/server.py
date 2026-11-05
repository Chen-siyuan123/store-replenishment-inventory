#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
门店补货与库存协同智能体系统 · Web 服务（FastAPI 后端）
================================================================
批次 1（认证体系）：
  POST /api/login          登录（JWT）
  GET  /api/me             当前用户信息
  GET  /login              登录页（SSR）

问答与智能体（阶段 2）：
  POST /chat               问答主接口（SSE 流式，需登录）
                           内部走 agent/harness.py 的执行循环：模型决策 → 工具 → 回填，最多 5 轮
  POST /api/agent/confirm  写操作人工确认后从断点继续（SSE，同样的事件协议）
  GET  /api/agent/pending  会话内待确认的写操作（页面刷新后恢复确认卡）
  GET  /api/agent/runs     执行轨迹列表 + 质量指标（管理员）
  GET  /api/agent/runs/{id}/steps  单次运行的完整轨迹（管理员）
  GET  /admin/trace        执行轨迹页（管理员）
  GET  /sessions           会话列表（只看本人）
  GET  /sessions/{id}/messages / DELETE /sessions/{id}
  GET  /                   问答页

错误约定：
  - 校验类错误以 JSON 返回 {code, message}（中文）；
    401 = 未登录/过期（UNAUTHORIZED / TOKEN_EXPIRED）
    403 = 权限不足（PERMISSION_DENIED）
  - /chat 建流后发生的错误通过 SSE error 事件推送。

启动方式：
  ① 复制配置模板并填入密钥（只需一次）：
       copy .env.example .env      然后编辑 .env
  ② 双击 run_local.bat（Windows），或手动执行：
       python -m uvicorn server:app --host 127.0.0.1 --port 8765
  ③ 配置体检（不启动服务）：
       python config.py
"""

import json
import os
import re
import uuid

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional

import config   # 配置加载 + 自检。必须排在 kbstore 之前：
                # kbstore 的检索门限是模块级常量，import 时就要读到 .env
import db       # 数据层（建表/种子/连接）
import kbstore  # 【核心模块①②】知识库存取 + 混合检索（批次 2 数据库化）
from agent import harness, store as agent_store   # 执行循环 + 轨迹/确认持久化
from auth import (create_token, verify_password,
                  get_current_user, require_admin)
from kbstore import KBParseError

# ============================================================
# 全局配置
# ============================================================

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(WEB_DIR, "index.html")
templates = Jinja2Templates(directory=os.path.join(WEB_DIR, "templates"))

MAX_QUESTION_LEN = 500
HISTORY_ROUNDS = 6  # 每次请求携带最近 6 轮历史（12 条消息）

app = FastAPI(title="门店补货与库存协同 · 店长问答与管理后台")


# ============================================================
# 统一错误响应
# ============================================================

def err(code: str, message: str, status: int):
    return JSONResponse(status_code=status, content={"code": code, "message": message})


def sse_event(event: str, data: dict) -> str:
    """构造一条 SSE 事件文本。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ============================================================
# 启动事件：初始化数据库 + 构建知识库
# ============================================================

@app.on_event("startup")
def startup():
    # ---- 配置自检先跑：把「模型名填错位置」这类问题挡在第一个请求之前 ----
    config_ok = config.print_diagnose()

    db.init_db()  # 建表 + 种子账号 + 默认提示词模板
    try:
        app.state.kb = kbstore.KBStore()
        if app.state.kb.rebuild() == 0:
            # 知识库为空时自动导入种子文档（补货制度.md）
            if app.state.kb.seed_default():
                print("[知识库] 已自动导入种子文档：补货制度.md")
        kb = app.state.kb
        print(f"[知识库] 构建完成：{len(kb.chunks)} 个切片 | "
              f"向量来源 {kbstore.kbcore.embed_provider_id()}")
        if kb.last_reembed:
            r = kb.last_reembed
            print(f"[知识库] 向量已重算：{r['from']} → {r['to']}（{r['chunks']} 个切片）")
        if kb.embed_stale:
            print(f"[知识库][WARN] 语义检索已禁用，仅用关键词：{kb.last_embed_error}")
    except Exception as e:
        app.state.kb = None
        print(f"[知识库] 构建失败：{e}")

    if not config_ok:
        print("[启动] 模型配置存在错误：问答会返回中文错误提示，修正 .env 后重启即可。")


# ============================================================
# 页面路由
# ============================================================

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    """登录页（SSR）。"""
    return templates.TemplateResponse(request, "login.html")


@app.get("/", response_class=HTMLResponse)
def index():
    """店长问答页。"""
    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ============================================================
# 认证接口
# ============================================================

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(req: LoginRequest):
    """登录：校验密码 → 签发 JWT（payload 含角色与门店）。"""
    with db.get_db() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE username = ? AND status = 'active'",
            (req.username.strip(),),
        ).fetchone()

    if not user or not verify_password(req.password, user["password_hash"]):
        return err("LOGIN_FAILED", "用户名或密码错误", 401)

    return {
        "token": create_token(user),
        "username": user["username"],
        "role": user["role"],
        "store_id": user["store_id"],
        "store_name": user["store_name"],
    }


@app.get("/api/me")
def me(user: dict = Depends(get_current_user)):
    """当前用户信息（前端页面挂载时校验登录态用）。"""
    return {"user": user}


# ============================================================
# 问答接口（需登录）
# ============================================================

class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    question: str


@app.post("/chat")
async def chat(req: ChatRequest, user: dict = Depends(get_current_user)):
    """问答主接口：登录后可用；会话归属当前用户。"""
    # ---- 建流前参数校验 ----
    question = (req.question or "").strip()
    if not question:
        return err("PARAM_INVALID", "请求参数不完整：question 不能为空", 400)
    if len(question) > MAX_QUESTION_LEN:
        return err("QUESTION_TOO_LONG",
                   f"问题长度超过 {MAX_QUESTION_LEN} 字，请精简后重试", 400)

    # ---- 会话处理：无 session_id 则新建（记录归属用户/门店）----
    session_id = req.session_id
    with db.get_db() as conn:
        if session_id:
            row = conn.execute(
                "SELECT id FROM sessions WHERE id = ? AND user_id = ?",
                (session_id, user["uid"]),
            ).fetchone()
            if not row:
                return err("SESSION_NOT_FOUND", "会话不存在或已被删除", 404)
        else:
            session_id = uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO sessions (id, user_id, store_id, title, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (session_id, user["uid"], user.get("store_id"), question[:20],
                 db.now(), db.now()),
            )

    return StreamingResponse(
        event_generator(session_id, question, user),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============================================================
# 审计异常标记（批次 4）
# 说明：启发式规则，用于人工复核排序，不作为处罚依据
# ============================================================

R1_KEYWORDS = [
    "绕过", "低于起订", "低于最小", "拆零", "不按整箱", "超过上限", "超上限",
    "多要", "特批", "通融", "加急", "先发货", "私下", "走后门",
]


def detect_audit_flags(question: str, answer: str, hits):
    """对一问一答执行三项异常检测，返回标记列表（无异常返回空列表）。

    R1 疑似越权要求：问题命中绕过制度类关键词（拆零/低于起订/超上限…）
    R2 模糊回答：检索到了条文，回答却以「未找到/无法」兜底，且没有任何条文引用
    R3 低质量检索：top1 原始余弦得分 < 0.3（引用条文大概率与问题无关）
    """
    flags = []

    # ---- R1：问题关键词 ----
    hit_kw = [k for k in R1_KEYWORDS if k in question]
    if hit_kw:
        flags.append({"rule": "R1", "label": "疑似越权要求",
                      "detail": "命中关键词：" + "、".join(hit_kw)})

    # ---- R2：模糊兜底回答（无引用的「未找到/无法」式回答）----
    # 注意：检索结果为空的「制度中未找到」是正确答案（NO_HIT 分支），不能判为异常；
    # 只有「检索到了条文却不用、直接说未找到」才计入。
    evasive = ("未找到" in answer) or ("无法" in answer) or ("无法回答" in answer)
    has_citation = ("制度条款" in answer) or ("引用来源" in answer)
    if hits and evasive and not has_citation:
        flags.append({"rule": "R2", "label": "模糊回答",
                      "detail": "检索到条文但回答以「未找到/无法」兜底且未引用任何制度条文"})

    # ---- R3：低质量检索（top1 原始余弦分；注意不能用 min-max 融合分，
    #      融合分经过归一化后 top1 恒接近 1，无法区分好坏）----
    if hits and hits[0].get("vec_score", 1.0) < 0.3:
        flags.append({"rule": "R3", "label": "低质量检索",
                      "detail": f"top1 余弦得分 {hits[0]['vec_score']} < 0.3，引用条文可能与问题无关"})

    return flags


# ============================================================
# 智能体执行循环（Agent Harness）接入
# ============================================================
# 循环本体在 agent/harness.py，本文件只做四件事：
#   ① 把 HTTP 请求翻译成 Harness 输入（会话 / 历史 / 提示词 / 知识库）
#   ② 把 Harness 产出的事件流转成 SSE
#   ③ 流结束后统一落库（消息、引用、审计标记）
#   ④ 暴露人工确认接口与轨迹查询接口
# ============================================================

def _pump(events, h, session_id: str, question: str):
    """事件泵：Harness 事件 → SSE 文本，并在流结束时落库。

    为什么落库放在生成器末尾而不是开头：
      SSE 回答是边生成边推送的，开始推送时回答长度未知；
      只有流真正结束（正常回答 / 中断 / 失败）才能拿到完整 answer 和 tool_trace。

    注意：浏览器中途断开时生成器会被关闭，落库可能被跳过。
    生产版本应把「落库」放进 finally，并配合 agent_runs 的 status=running 做补偿清理。
    """
    for event, payload in events:
        yield sse_event(event, payload)

    # ---- 流结束：写 assistant 消息 ----
    answer = h.answer
    if h.status == "waiting_confirm":
        # 中断时 answer 还没生成：写一条占位，保证会话历史里能看出「这里停过一次」
        answer = answer or "已生成补货申请预览，等待你确认后创建草稿。"
    elif not answer and h.status == "failed":
        # 调用失败（如模型未配置）：也要留痕，否则历史里是一条空消息
        answer = "抱歉，本次处理未能完成，请稍后重试（可在管理后台「执行轨迹」查看卡在哪一步）。"

    hits = h.ctx.get("last_hits") or []
    flags = detect_audit_flags(question, answer, hits)

    # R4：模型尝试跳过校验 / 复用令牌 / 未经确认建单，被代码门禁拒绝
    # 注意：STEP_VIOLATION 属于「补齐前置步骤」的正常流程，不计入违规
    if any(t.get("code") in ("TOKEN_INVALID", "TOKEN_MISMATCH", "TOKEN_EXPIRED",
                             "STOCK_NOT_VERIFIED", "TOKEN_USED")
           for t in h.tool_trace):
        flags.append({"rule": "R4", "label": "流程违规尝试",
                      "detail": "模型尝试跳过校验或未经人工确认创建草稿，已被代码门禁拒绝"})
    audit_json = json.dumps({"flags": flags}, ensure_ascii=False) if flags else None

    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, sources, audit, tool_trace, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (session_id, "assistant", answer,
             json.dumps(h.sources, ensure_ascii=False) if h.sources else None,
             audit_json,
             json.dumps(h.tool_trace, ensure_ascii=False) if h.tool_trace else None,
             db.now()),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?",
                     (db.now(), session_id))

    yield sse_event("sources", {"sources": h.sources})
    yield sse_event("done", {"session_id": session_id, "run_id": h.run_id, "status": h.status})


def event_generator(session_id: str, question: str, user: dict):
    """走 SSE 的问答入口：组装上下文 → 交给 Harness 跑循环。"""
    yield sse_event("status", {"message": "正在理解您的问题…"})

    if app.state.kb is None or app.state.kb.is_empty():
        yield sse_event("error", {"code": "KB_EMPTY",
                                  "message": "制度知识库为空，请联系管理员导入文档"})
        return

    # ---- 保存用户消息 + 更新会话活跃时间 ----
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, sources, audit, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (session_id, "user", question, None, None, db.now()),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?",
                     (db.now(), session_id))

    # ---- 最近 6 轮历史（不含本轮问题）----
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? "
            "AND role IN ('user','assistant') ORDER BY id DESC LIMIT ?",
            (session_id, HISTORY_ROUNDS * 2),
        ).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)][:-1]

    # ---- 系统提示词：实时读库 + 保证工作流契约存在（管理员改模板不影响门禁语义）----
    # 用「执行预算」这一段作为哨兵：老版本模板即使含有「工作流契约」字样，
    # 只要没有 5 轮预算的约束，也要补上——否则模型会用旧纪律跑新框架。
    system_prompt = db.get_active_prompt()
    if "【执行预算：最多 5 轮】" not in system_prompt:
        system_prompt += "\n\n" + db.AGENT_CONTRACT

    h = harness.Harness(
        session_id=session_id,
        store_id=user.get("store_id") or "S001",    # 管理员无门店，原型默认 S001
        user=user,
        username=user.get("sub") or "",
        kb=app.state.kb,
        system_prompt=system_prompt,
        history=history,
        question=question,
    )
    yield from _pump(h.run(), h, session_id, question)


# ============================================================
# 人工确认（Human-in-the-loop）
# 设计要点：写操作在**执行前**中断，确认后从断点恢复，而不是执行完再补一个确认按钮
# ============================================================

class ConfirmRequest(BaseModel):
    run_id: str
    action_id: int
    decision: str = "confirm"        # confirm / cancel


@app.get("/api/agent/pending")
def agent_pending(session_id: str, user: dict = Depends(get_current_user)):
    """查询会话内仍待确认的写操作（页面刷新/重新进入会话时恢复确认卡）。"""
    with db.get_db() as conn:
        row = conn.execute("SELECT id FROM sessions WHERE id = ? AND user_id = ?",
                           (session_id, user["uid"])).fetchone()
    if not row:
        return err("SESSION_NOT_FOUND", "会话不存在或已被删除", 404)
    return {"actions": agent_store.get_pending_by_session(session_id)}


@app.post("/api/agent/confirm")
def agent_confirm(req: ConfirmRequest, user: dict = Depends(get_current_user)):
    """店长确认/取消后，从断点继续执行。

    返回的同样是 SSE 流：
      前端拿到流后继续渲染（delta 回答 + draft 草稿卡片），体验上与首次提问一致。
    """
    run = agent_store.get_run(req.run_id)
    if not run:
        return err("RUN_NOT_FOUND", "执行记录不存在或已过期", 404)

    # 行级权限：只能确认自己会话的运行；管理员可代为确认
    if user["role"] != "admin" and run.get("user_id") != user["uid"]:
        return err("PERMISSION_DENIED", "无权操作他人的补货申请", 403)

    if run.get("status") != "waiting_confirm":
        return err("RUN_STATUS_INVALID",
                   f"该申请当前状态为「{run.get('status')}」，不需要再次确认", 400)

    action = agent_store.get_action(req.action_id)
    if not action or action.get("run_id") != req.run_id:
        return err("ACTION_NOT_FOUND", "确认卡不存在或与本次申请不匹配", 404)
    if action.get("status") != "pending":
        return err("ACTION_NOT_PENDING", "该确认卡已处理或已失效", 400)
    if req.decision not in ("confirm", "cancel"):
        return err("PARAM_INVALID", "decision 只能是 confirm 或 cancel", 400)

    h = harness.Harness.from_run(run, kb=app.state.kb, user=user,
                                 username=user.get("sub") or "")
    return StreamingResponse(
        _pump(h.resume(req.decision, user.get("sub") or ""), h,
              run["session_id"], run["question"]),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============================================================
# 轨迹质量分析（管理员）
# ============================================================

@app.get("/admin/trace", response_class=HTMLResponse)
def admin_trace_page(request: Request, user: dict = Depends(get_current_user)):
    """执行轨迹页（页面内 JS 再校验 admin 角色）。"""
    return templates.TemplateResponse(request, "trace.html")


@app.get("/api/agent/runs")
def agent_runs_list(session_id: Optional[str] = None,
                    limit: int = 50,
                    user: dict = Depends(require_admin)):
    """运行列表 + 质量指标（轮数/工具数/阻断数/耗时）。"""
    return {"runs": agent_store.list_runs(session_id, limit=max(1, min(limit, 200)))}


@app.get("/api/agent/runs/{run_id}/steps")
def agent_run_steps(run_id: str, user: dict = Depends(require_admin)):
    """单次运行的完整轨迹 + 质量评分。"""
    run = agent_store.get_run(run_id)
    if not run:
        return err("RUN_NOT_FOUND", "执行记录不存在", 404)
    meta = {k: run[k] for k in
            ("id", "session_id", "store_id", "question", "status",
             "round_used", "max_rounds", "answer", "created_at", "updated_at")}
    return {
        "run": meta,
        "steps": agent_store.list_steps(run_id),
        "quality": agent_store.trace_quality(run_id),
    }


# ============================================================
# 会话接口（行级权限：只操作本人的会话）
# ============================================================

@app.get("/sessions")
def list_sessions(user: dict = Depends(get_current_user)):
    """会话列表：仅返回当前用户的会话。"""
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT id, title, updated_at FROM sessions WHERE user_id = ? "
            "ORDER BY updated_at DESC LIMIT 50",
            (user["uid"],),
        ).fetchall()
    return {"sessions": [dict(r) for r in rows]}


@app.get("/sessions/{session_id}/messages")
def get_messages(session_id: str, user: dict = Depends(get_current_user)):
    """加载会话历史：校验归属。"""
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM sessions WHERE id = ? AND user_id = ?",
            (session_id, user["uid"]),
        ).fetchone()
        if not row:
            return err("SESSION_NOT_FOUND", "会话不存在或已被删除", 404)
        rows = conn.execute(
            "SELECT role, content, sources, created_at FROM messages "
            "WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    msgs = []
    for r in rows:
        m = {"role": r["role"], "content": r["content"], "created_at": r["created_at"]}
        if r["sources"]:
            m["sources"] = json.loads(r["sources"])
        msgs.append(m)
    return {"messages": msgs}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str, user: dict = Depends(get_current_user)):
    """删除会话：校验归属，级联删消息。"""
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM sessions WHERE id = ? AND user_id = ?",
            (session_id, user["uid"]),
        ).fetchone()
        if not row:
            return err("SESSION_NOT_FOUND", "会话不存在或已被删除", 404)
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return {"ok": True}


# ============================================================
# 补货草稿：人工确认与提交（智能体阶段）
# 说明：草稿只能由模型创建，提交只能由人操作——模型没有提交权限
# ============================================================

@app.get("/api/drafts")
def list_drafts(session_id: Optional[str] = None,
                user: dict = Depends(get_current_user)):
    """草稿列表：仅返回本人会话产生的草稿（管理员可看全部门店）。"""
    with db.get_db() as conn:
        if user["role"] == "admin":
            rows = conn.execute(
                "SELECT d.*, s.unit FROM replenishment_drafts d "
                "LEFT JOIN skus s ON s.sku = d.sku "
                "WHERE (? IS NULL OR d.session_id = ?) "
                "ORDER BY d.id DESC LIMIT 50", (session_id, session_id)).fetchall()
        else:
            rows = conn.execute(
                "SELECT d.*, k.unit FROM replenishment_drafts d "
                "JOIN sessions s ON s.id = d.session_id "
                "LEFT JOIN skus k ON k.sku = d.sku "
                "WHERE s.user_id = ? AND (? IS NULL OR d.session_id = ?) "
                "ORDER BY d.id DESC LIMIT 50",
                (user["uid"], session_id, session_id)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["basis"] = json.loads(d["basis"]) if d.get("basis") else {}
        out.append(d)
    return {"drafts": out}


@app.post("/api/drafts/{draft_id}/submit")
def submit_draft(draft_id: int, user: dict = Depends(get_current_user)):
    """店长在界面确认提交：draft → submitted。这是唯一的人工写操作入口。"""
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT d.*, s.user_id FROM replenishment_drafts d "
            "LEFT JOIN sessions s ON s.id = d.session_id WHERE d.id = ?",
            (draft_id,)).fetchone()
        if not row:
            return err("DRAFT_NOT_FOUND", "补货草稿不存在或已被删除", 404)
        if user["role"] != "admin" and row["user_id"] != user["uid"]:
            return err("PERMISSION_DENIED", "无权操作他人的补货草稿", 403)
        if row["status"] != "draft":
            return err("DRAFT_STATUS_INVALID",
                       f"草稿当前状态为「{row['status']}」，不能再次提交", 400)
        conn.execute(
            "UPDATE replenishment_drafts SET status = 'submitted', submitted_at = ? "
            "WHERE id = ?", (db.now(), draft_id))
    return {"ok": True, "id": draft_id, "status": "submitted",
            "message": "补货申请已提交，等待总部确认与仓库分配"}


@app.post("/api/drafts/{draft_id}/cancel")
def cancel_draft(draft_id: int, user: dict = Depends(get_current_user)):
    """店长放弃草稿。"""
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT d.*, s.user_id FROM replenishment_drafts d "
            "LEFT JOIN sessions s ON s.id = d.session_id WHERE d.id = ?",
            (draft_id,)).fetchone()
        if not row:
            return err("DRAFT_NOT_FOUND", "补货草稿不存在或已被删除", 404)
        if user["role"] != "admin" and row["user_id"] != user["uid"]:
            return err("PERMISSION_DENIED", "无权操作他人的补货草稿", 403)
        if row["status"] != "draft":
            return err("DRAFT_STATUS_INVALID", "只有草稿状态可以撤回", 400)
        conn.execute("UPDATE replenishment_drafts SET status = 'rejected' WHERE id = ?",
                     (draft_id,))
    return {"ok": True, "id": draft_id, "status": "rejected", "message": "草稿已撤回"}


# ============================================================
# 管理后台：知识库管理（批次 2，仅管理员）
# ============================================================

@app.get("/admin/kb", response_class=HTMLResponse)
def admin_kb_page(request: Request, user: dict = Depends(get_current_user)):
    """知识库管理页（页面内 JS 再校验 admin 角色，未授权跳登录页）。"""
    return templates.TemplateResponse(request, "kb.html")


@app.post("/api/kb/documents")
async def upload_document(
    request: Request,
    user: dict = Depends(require_admin),
):
    """上传制度文档（multipart：file + doc_type）→ 解析切片向量化入库。"""
    try:
        form = await request.form()
        upload = form.get("file")
        doc_type = (form.get("doc_type") or "其他").strip()

        if upload is None or not getattr(upload, "filename", ""):
            return err("PARAM_INVALID", "请求参数不完整：请选择要上传的文件", 400)

        data = await upload.read()
        chunk_count = app.state.kb.add_document(
            upload.filename, data, doc_type, user["sub"])
        return {"ok": True, "chunk_count": chunk_count,
                "message": f"上传成功，已切片 {chunk_count} 段并入库"}
    except KBParseError as e:
        return err("KB_PARSE_FAILED", str(e), 400)
    except kbstore.kbcore.EmbedError as e:
        # 向量化失败必须整体回滚语义：切片已写库但向量算不出来，等于半成品，
        # 这里直接拒绝并在前端说明原因，不留下无法检索的脏数据
        return err("KB_EMBED_FAILED", f"文档已解析但向量化失败：{e.message}", 502)
    except Exception as e:
        return err("INTERNAL_ERROR", f"上传处理异常：{e}", 500)


@app.get("/api/kb/documents")
def list_documents(user: dict = Depends(require_admin)):
    """文档列表（含切片数、上传人、时间）。"""
    return {"documents": app.state.kb.list_documents()}


@app.delete("/api/kb/documents/{doc_id}")
def delete_document(doc_id: int, user: dict = Depends(require_admin)):
    """删除文档及切片，重建索引。"""
    try:
        app.state.kb.delete_document(doc_id)
        return {"ok": True, "message": "文档已删除，索引已重建"}
    except KeyError:
        return err("DOC_NOT_FOUND", "文档不存在或已被删除", 404)


@app.post("/api/kb/seed")
def seed_kb(user: dict = Depends(require_admin)):
    """手动导入种子文档（补货制度.md）。"""
    if app.state.kb.seed_default(user["sub"]):
        return {"ok": True, "message": "种子文档已导入"}
    return err("SEED_FAILED", "种子文档不存在或导入失败（可能已导入过）", 400)


# ============================================================
# 管理后台：提示词模板管理（批次 3，仅管理员）
# ============================================================

class PromptRequest(BaseModel):
    name: str
    content: str


@app.get("/admin/prompts", response_class=HTMLResponse)
def admin_prompts_page(request: Request, user: dict = Depends(get_current_user)):
    """提示词模板管理页（页面内 JS 再校验 admin 角色）。"""
    return templates.TemplateResponse(request, "prompts.html")


@app.get("/api/prompts")
def list_prompts(user: dict = Depends(require_admin)):
    """模板列表（含生效标记），按更新时间倒序。"""
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, content, is_active, updated_by, updated_at "
            "FROM prompt_templates ORDER BY is_active DESC, updated_at DESC"
        ).fetchall()
    return {"prompts": [dict(r) for r in rows]}


@app.post("/api/prompts")
def create_prompt(req: PromptRequest, user: dict = Depends(require_admin)):
    """新建模板版本（存库，不覆盖旧版，可追溯）。"""
    name = (req.name or "").strip()
    content = (req.content or "").strip()
    if not content:
        return err("PARAM_INVALID", "模板内容不能为空", 400)
    if len(content) > 2000:
        return err("PROMPT_TOO_LONG", "模板内容超过 2000 字上限", 400)
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO prompt_templates (name, content, is_active, updated_by, updated_at) "
            "VALUES (?,?,0,?,?)",
            (name or f"模板 {db.now()}", content, user["sub"], db.now()),
        )
    return {"ok": True, "message": "模板已保存（未生效）"}


@app.put("/api/prompts/{prompt_id}/activate")
def activate_prompt(prompt_id: int, user: dict = Depends(require_admin)):
    """设为生效版本：先全部置 0 再置 1，保证全局唯一生效；问答实时读库立即生效。"""
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM prompt_templates WHERE id = ?", (prompt_id,)).fetchone()
        if not row:
            return err("PROMPT_NOT_FOUND", "模板不存在或已被删除", 404)
        conn.execute("UPDATE prompt_templates SET is_active = 0")
        conn.execute(
            "UPDATE prompt_templates SET is_active = 1, updated_by = ?, updated_at = ? "
            "WHERE id = ?",
            (user["sub"], db.now(), prompt_id),
        )
    return {"ok": True, "message": "模板已生效，问答立即使用新版本"}


# ============================================================
# 管理后台：对话审计（批次 4，仅管理员）
# ============================================================

@app.get("/admin/audit", response_class=HTMLResponse)
def admin_audit_page(request: Request, user: dict = Depends(get_current_user)):
    """对话审计页（页面内 JS 再校验 admin 角色）。"""
    return templates.TemplateResponse(request, "audit.html")


@app.get("/api/audit/meta")
def audit_meta(user: dict = Depends(require_admin)):
    """筛选项元数据：门店下拉 + 店长下拉。"""
    with db.get_db() as conn:
        stores = conn.execute(
            "SELECT DISTINCT store_id, store_name FROM users "
            "WHERE store_id IS NOT NULL ORDER BY store_id"
        ).fetchall()
        users = conn.execute(
            "SELECT id, username, store_name, role FROM users ORDER BY id"
        ).fetchall()
    return {
        "stores": [dict(r) for r in stores],
        "users": [dict(r) for r in users],
    }


@app.get("/api/audit")
def audit_records(
    store_id: Optional[str] = None,
    user_id: Optional[int] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    flagged_only: bool = False,
    user: dict = Depends(require_admin),
):
    """问答审计记录：按门店/店长/时间筛选，可只看异常。

    每条记录 = 一次问答对（assistant 消息 + 其同会话上一条 user 消息）。
    """
    sql = (
        "SELECT a.id AS message_id, a.session_id, a.content AS answer, "
        "       a.sources, a.audit, a.created_at, "
        "       s.user_id, u.username, u.store_name, "
        "       (SELECT q.content FROM messages q "
        "         WHERE q.session_id = a.session_id AND q.role = 'user' "
        "           AND q.id < a.id ORDER BY q.id DESC LIMIT 1) AS question "
        "FROM messages a "
        "JOIN sessions s ON a.session_id = s.id "
        "JOIN users u ON s.user_id = u.id "
        "WHERE a.role = 'assistant'"
    )
    params = []
    if store_id:
        sql += " AND u.store_id = ?"
        params.append(store_id)
    if user_id:
        sql += " AND s.user_id = ?"
        params.append(user_id)
    if start:
        sql += " AND a.created_at >= ?"
        params.append(start)
    if end:
        sql += " AND a.created_at <= ?"     # 日期闭区间：补齐当天末尾
        params.append(end + "T23:59:59" if len(end) == 10 else end)
    if flagged_only:
        sql += " AND a.audit IS NOT NULL"
    sql += " ORDER BY a.id DESC LIMIT 200"

    with db.get_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    records = []
    for r in rows:
        rec = dict(r)
        rec["flags"] = json.loads(r["audit"])["flags"] if r["audit"] else []
        rec["sources"] = json.loads(r["sources"]) if r["sources"] else []
        # 脱敏：审计列表不返回全文，详情前端按需展示（记录里保留全文，量小）
        records.append(rec)
    return {"records": records}
