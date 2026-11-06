"""把一次真实执行轨迹导出为 Markdown 实录。

用途：给文档 / 答辩留可核对的过程证据——不是手写截图，而是从 chat.db 的
agent_runs / agent_steps / agent_actions 三张表直接读出来。

用法：
    python scripts/export_run_trace.py --run-id f7d05cf6ffbd \
        --out "docs/delivery/运行实录-20箱牛奶案例.md"

不带 --run-id 时，列出最近 10 次运行供挑选。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "src" / "web" / "chat.db"

# 结果摘要里只保留这些关键字段，避免把整段 JSON 塞进文档
KEEP_KEYS = (
    "qty", "boxes", "box_text", "moq", "box_size", "alloc_cap",
    "sellable_qty", "avg_daily", "remaining", "next_delivery_date",
    "passed", "violations", "blocking_reason", "code", "id", "status",
    "shortfall", "adjust", "unsatisfied_reason",
)


def connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        sys.exit(f"找不到数据库：{DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def list_runs(conn: sqlite3.Connection, limit: int = 10) -> None:
    rows = conn.execute(
        "select id, session_id, status, round_used, question, created_at "
        "from agent_runs order by created_at desc limit ?",
        (limit,),
    ).fetchall()
    print(f"{'run_id':<14}{'轮次':<6}{'状态':<8}{'时间':<21}问题")
    for r in rows:
        print(f"{r['id']:<14}{str(r['round_used']):<6}{r['status']:<8}{r['created_at']:<21}{r['question']}")


def brief(payload: str, limit: int = 6) -> str:
    """从工具返回的 JSON 里挑关键字段，生成一行摘要。"""
    try:
        obj = json.loads(payload)
    except (TypeError, ValueError):
        return (payload or "").strip()[:200]

    parts = []
    msg = obj.get("message")
    if msg:
        parts.append(msg)
    data = obj.get("data") or {}
    picked = {k: data[k] for k in KEEP_KEYS if k in data}
    if picked:
        text = json.dumps(picked, ensure_ascii=False)
        if len(text) > 240:
            text = text[:240] + "…"
        parts.append(text)
    return "；".join(parts)


def export(conn: sqlite3.Connection, run_id: str) -> str:
    run = conn.execute("select * from agent_runs where id = ?", (run_id,)).fetchone()
    if not run:
        sys.exit(f"没有这个运行记录：{run_id}")

    steps = conn.execute(
        "select * from agent_steps where run_id = ? order by round_no, step_no", (run_id,)
    ).fetchall()
    actions = conn.execute(
        "select * from agent_actions where run_id = ? order by id", (run_id,)
    ).fetchall()

    lines = [
        f"# 运行实录：{run['question']}",
        "",
        "> 本文件由 `scripts/export_run_trace.py` 从 `chat.db` 的轨迹三表导出，未做人工改写。",
        f"> run_id `{run['id']}`，会话 `{run['session_id']}`，门店 `{run['store_id']}`，"
        f"开始时间 {run['created_at']}，状态 `{run['status']}`，"
        f"用掉 {run['round_used']}/{run['max_rounds']} 轮。",
    ]
    if str(run["session_id"]).startswith("demo-"):
        lines.append(
            "> 说明：该会话由 `agent/scenarios.py` 的 ScriptedLLM 离线回放驱动（不消耗 API 额度），"
            "但工具层、四重校验、HITL 中断、落库全部走真实代码路径，因此工具返回、校验结论、"
            "草稿字段与真实调用一致；真实模型下的措辞可能不同，系统行为不变。"
        )
    lines += ["", "## 一、逐轮工具调用"]

    cur_round = None
    for s in steps:
        if s["round_no"] != cur_round:
            cur_round = s["round_no"]
            lines += ["", f"### 第 {cur_round} 轮", ""]
        flag = "**阻塞**" if s["blocking"] else ("ok" if s["ok"] else "失败")
        if s["tool"] == "__model__":
            lines.append(f"- {s['result_summary']}")
            continue
        lines.append(f"- `{s['tool']}`（{flag}，{s['duration_ms']} ms）")
        if s["args"] and str(s["args"]) not in ("None", "{}"):
            lines.append(f"  - 入参：`{s['args']}`")
        lines.append(f"  - 结果：{brief(s['result_summary'])}")
        if s["code"]:
            lines.append(f"  - 错误码：`{s['code']}`")

    if actions:
        lines += ["", "## 二、写操作中断（HITL）", ""]
        for a in actions:
            lines += [
                f"- 工具 `{a['tool']}`，状态 **{a['status']}**"
                + (f"，由 `{a['decided_by']}` 在 {a['decided_at']} 确认" if a["decided_by"] else ""),
                f"  - 入参：`{a['args']}`",
            ]
            try:
                preview = json.loads(a["preview"])
            except (TypeError, ValueError):
                preview = {}
            checks = preview.get("checks") or []
            if checks:
                lines.append("  - 模型看到的预检项：")
                lines += [f"    - {'✅' if c.get('ok') else '❌'} {c.get('name')}：{c.get('detail')}" for c in checks]
            if preview.get("adjust"):
                adj = preview["adjust"]
                lines.append(
                    f"  - 数量调整：原 {adj.get('from_qty')} → 实际 {adj.get('to_qty')}"
                    f"（{adj.get('reason')}）"
                )

    lines += ["", "## 三、最终回答", "", (run["answer"] or "（无）").strip(), ""]

    drafts = conn.execute(
        "select * from replenishment_drafts where session_id = ? order by id desc limit 3",
        (run["session_id"],),
    ).fetchall()
    if drafts:
        lines += [
            "## 四、落库结果（补货草稿）",
            "",
            "| 草稿 | SKU | 店长原要求 | 实际建单量 | 配送日 | 状态 |",
            "|---|---|---|---|---|---|",
        ]
        for d in drafts:
            lines.append(
                f"| #{d['id']} | {d['sku']} {d['sku_name']} | {d['qty_requested']} | "
                f"{d['qty_suggested']} | {d['delivery_date']} | {d['status']} |"
            )
        lines.append("")

    lines += [
        "## 附：怎么复现这条轨迹",
        "",
        "```bash",
        "cd src/web && python -m agent.scenarios        # 跑三条离线轨迹（写库）",
        "python scripts/export_run_trace.py                   # 列出最近运行，拿到 run_id",
        f"python scripts/export_run_trace.py --run-id {run['id']} --out docs/delivery/运行实录.md",
        "```",
        "",
    ]

    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="导出一次执行轨迹为 Markdown")
    ap.add_argument("--run-id", help="agent_runs.id；省略则列出最近运行")
    ap.add_argument("--out", help="输出 md 路径（相对仓库根目录）")
    args = ap.parse_args()

    conn = connect()
    if not args.run_id:
        list_runs(conn)
        return

    text = export(conn, args.run_id)
    if args.out:
        out = ROOT / args.out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"已写入 {out}（{len(text)} 字符）")
    else:
        print(text)


if __name__ == "__main__":
    main()
