#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
会话与调用日志的留存 / 归档（retention.py）
================================================================
依据访谈 f43：
  「本轮合成试点评价记录暂留 90 天，到期由商品与损耗经理确认删除或归档；
    真实组织的资料留存期限需另行核实。」
以及 f45：审核日志要能重建当次处理，但「不记录口令或非必要个人信息」。

本模块的边界（很重要，别把业务单据一起删了）：
  会清理：messages（问答原文）、agent_runs / agent_steps / agent_actions /
          agent_incidents（调用轨迹、确认记录、转交单）、无草稿的空会话
  不清理：replenishment_drafts（补货申请是经营/财务凭证，留存期限另行规定）、
          users、kb_documents / kb_chunks（制度原文）、sku 主数据

安全设计：
  1. 先归档再删除——每条被删记录先写进 archive/ 下的 JSONL，落盘成功才执行 DELETE；
  2. 幂等可重跑——按主键删除，重复执行不会误删新数据；
  3. 默认只预览——execute 需要显式传入 confirm="ARCHIVE"，且由管理员在页面上二次确认；
  4. 未处理的转交单不删——待办还在，删了就等于把未闭环问题抹掉。
"""

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import db

# 保留天数：默认 90（试点口径），可用环境变量覆盖
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "90"))

# 归档目录（与 sqlite 文件同级，已在 .gitignore 中排除）
ARCHIVE_DIR = Path(__file__).resolve().parent / "archive"

# 表名 → (时间列, 额外筛选条件)
# pending 的确认卡/转交单不清理：它们还在等人工处理
PURGE_TABLES = [
    ("messages",        "created_at", ""),
    ("agent_steps",     "created_at", ""),
    ("agent_actions",   "created_at", "AND status <> 'pending'"),
    ("agent_incidents", "created_at", "AND status <> 'pending'"),
    ("agent_runs",      "created_at", "AND status NOT IN ('running','waiting_confirm')"),
]

# 空会话：没有草稿的会话才允许随日志一起清理
SESSION_WHERE = ("created_at < ? AND id NOT IN "
                 "(SELECT IFNULL(session_id,'') FROM replenishment_drafts)")


def cutoff_date(days: int = None) -> str:
    """到期分界线（YYYY-MM-DD）：早于这天的记录视为过期。"""
    d = int(days if days is not None else RETENTION_DAYS)
    return str(date.today() - timedelta(days=d))


def preview(days: int = None) -> dict:
    """只数不删：给管理员看清楚「这次会动多少数据」。"""
    cut = cutoff_date(days)
    out = {}
    with db.get_db() as conn:
        for table, col, extra in PURGE_TABLES:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {col} < ? {extra}", (cut,)).fetchone()
            out[table] = int(row["n"] or 0)
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE created_at < ? "
            "AND id NOT IN (SELECT IFNULL(session_id,'') FROM replenishment_drafts)",
            (cut,)).fetchone()
        out["sessions"] = int(row["n"] or 0)
    return {
        "days": int(days if days is not None else RETENTION_DAYS),
        "cutoff": cut,
        "counts": out,
        "total": sum(out.values()),
        "protected": {
            "replenishment_drafts": "补货申请为经营凭证，不随会话日志清理（留存期限另行规定）",
            "pending": "未处理的确认卡与转交单不清理，避免未闭环问题被删掉",
        },
    }


def execute(operator: str, days: int = None, confirm: str = "") -> dict:
    """归档并删除过期记录。confirm 必须等于 'ARCHIVE' 才会真正执行。"""
    if (confirm or "").strip() != "ARCHIVE":
        return {"ok": False, "code": "CONFIRM_REQUIRED",
                "message": "需要显式确认（confirm=ARCHIVE）才会执行归档删除"}

    cut = cutoff_date(days)
    plan = preview(days)
    if plan["total"] == 0:
        return {"ok": True, "cutoff": cut, "deleted": {}, "archived": {},
                "archive_file": None, "message": "没有到期记录，无需归档"}

    ARCHIVE_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_file = ARCHIVE_DIR / f"retention-{cut}-{stamp}.jsonl"

    deleted, archived = {}, {}
    with db.get_db() as conn:
        # ---- 第一步：先把要删的行完整导出（落盘成功才进入删除）----
        with archive_file.open("w", encoding="utf-8") as f:
            for table, col, extra in PURGE_TABLES:
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE {col} < ? {extra}", (cut,)).fetchall()
                archived[table] = len(rows)
                for r in rows:
                    f.write(json.dumps({"table": table, "row": dict(r)},
                                       ensure_ascii=False, default=str) + "\n")
            sess = conn.execute(
                "SELECT * FROM sessions WHERE created_at < ? "
                "AND id NOT IN (SELECT IFNULL(session_id,'') FROM replenishment_drafts)",
                (cut,)).fetchall()
            archived["sessions"] = len(sess)
            for r in sess:
                f.write(json.dumps({"table": "sessions", "row": dict(r)},
                                   ensure_ascii=False, default=str) + "\n")

        # ---- 第二步：按同一条件删除（同一事务内，失败则一起回滚）----
        for table, col, extra in PURGE_TABLES:
            cur = conn.execute(f"DELETE FROM {table} WHERE {col} < ? {extra}", (cut,))
            deleted[table] = cur.rowcount
        cur = conn.execute(
            "DELETE FROM sessions WHERE created_at < ? "
            "AND id NOT IN (SELECT IFNULL(session_id,'') FROM replenishment_drafts)", (cut,))
        deleted["sessions"] = cur.rowcount

    return {
        "ok": True,
        "operator": operator,
        "cutoff": cut,
        "days": plan["days"],
        "archived": archived,
        "deleted": deleted,
        "archive_file": str(archive_file),
        "message": (f"已归档并删除 {sum(deleted.values())} 条到期记录（早于 {cut}），"
                    f"归档文件：{archive_file.name}"),
    }
