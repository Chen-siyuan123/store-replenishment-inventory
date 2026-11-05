#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
停止条件转交（agent/incidents.py）
================================================================
解决什么问题
--------------
访谈报告 f44 原文：
  「出现临时促销、缺货截断、可售库存与账面库存不一致、依据缺失或关键系统不可用时，
    停止自动建议并标明**原因、接手人及人工确认时间**。」

改造前系统只做到了一半：`blocking=True` 会让模型停下并说明原因，但「谁来接手、
什么时候前处理完」没有任何落点——异常停在那里，没人知道是谁的待办。
f25 还要求「分派异常时要写明处理人、截止时间与退回原因，不能只把状态改成已转交」。

因此本模块做三件事：
  ① 按错误码映射到访谈报告里真实存在的岗位与人（不凭空造角色）；
  ② 按风险等级算出处理时限（促销拦截 8h、编码治理 48h 是两类不同节奏）；
  ③ 交 store 落一张 agent_incidents 表，带 status / due_at / handled_by，
     让「已转交」变成一个可查询、可超时统计的事实，而不是一句界面文案。

为什么放在 agent 层而不是 tools 层：
  工具只负责「判断能不能继续」，转交是运行层对异常的处置动作，
  离线回放（scenarios.py）也能复现同一套转交结果。
"""

from datetime import datetime, timedelta

from agent import store

# ============================================================
# 归属规则：错误码 → (岗位, 责任人, 时限小时, 处理说明)
# 人员与职责直接取自《业务需求发现报告》的六位受访者，不虚构。
# ============================================================

RULES = {
    # 账实不一致：f15「先由补货计划员核对」，店长配合现场盘点
    "STOCK_MISMATCH": (
        "补货计划员", "唐映秋", 24,
        "核对门店盘点记录与系统库存快照，确认可售口径后回填（f15）",
    ),
    # 促销版本未确认：门店现场才能确认当次促销版本（f13 / f21）
    "PROMO_VERSION_UNCONFIRMED": (
        "门店店长", "林亦舟", 8,
        "在门店终端确认当次促销版本号，确认后重新发起核对（f13）",
    ),
    # 促销截断：样本不足，需要计划员按促销计划人工给量（f44）
    "PROMO_TRUNCATED": (
        "补货计划员", "唐映秋", 24,
        "按促销计划与同期客流人工核定需求，标注依据后补录（f44）",
    ),
    # 编码未归并：数据治理责任在数据工程师（f30 / f33）
    "CODE_NOT_MERGED": (
        "数据工程师", "秦芮", 48,
        "按主编号归并新旧码/套装码，写入编码映射表后本条自动解除（f30）",
    ),
    # 无销量记录 / 零销量：可能是新品、停售或断货截断（f31）
    "NO_SALES_RECORD": (
        "补货计划员", "唐映秋", 24,
        "确认是新品未开售还是数据未同步，决定是否按人工经验给量（f31）",
    ),
    "ZERO_SALES": (
        "补货计划员", "唐映秋", 24,
        "核实零销量是否缺货截断，确认后改用可售库存口径（f31）",
    ),
    # 四项校验未通过：多数是超限，回到计划员按上限拆分或调整线路
    "VALIDATION_FAILED": (
        "补货计划员", "唐映秋", 24,
        "按分配上限与线路余量调整申请量，或改由其他配送日排入（f20 / f50）",
    ),
    # 上限内无可补数量：属于分配策略问题，需计划员确认是否走跨店调拨
    "NO_QTY_WITHIN_CAP": (
        "补货计划员", "唐映秋", 24,
        "确认是否改为跨店调拨（需区域经理批准），或本周期不再补货（f22）",
    ),
    # 制度未命中：知识库缺文档，责任在总部管理员
    "NO_HIT": (
        "总部管理员", "知识库管理员", 48,
        "补充对应制度/配送/促销文档并重建索引，避免同类问题重复判空",
    ),
    # 建单门禁失败：令牌失效、账实未确认、促销版本缺失
    "STOCK_NOT_VERIFIED": (
        "门店店长", "林亦舟", 8,
        "在确认卡上完成账实核对，重新发起补货申请（f15）",
    ),
    "PROMO_VERSION_REQUIRED": (
        "门店店长", "林亦舟", 8,
        "确认促销版本号后重新发起补货申请（f21）",
    ),
    "TOKEN_INVALID": (
        "补货计划员", "唐映秋", 4,
        "校验令牌失效（服务重启或超 15 分钟），重新走一遍四项校验即可",
    ),
    "TOKEN_EXPIRED": (
        "补货计划员", "唐映秋", 4,
        "校验令牌已过期，重新走一遍四项校验即可",
    ),
}

# 兜底：未登记的错误码统一交给计划员判定，不允许「无人认领」
DEFAULT_RULE = (
    "补货计划员", "唐映秋", 24,
    "未登记类别的停止条件，先由补货计划员判定归属后再转交（f25）",
)


def assign(code: str) -> tuple:
    """按错误码取归属规则；大小写不敏感，未命中走兜底。"""
    return RULES.get((code or "").strip().upper(), DEFAULT_RULE)


def record(*, run_id: str, session_id: str, store_id: str, tool: str,
           code: str, reason: str, sku: str = "") -> dict:
    """把一次停止条件落成转交单，返回给前端的展示结构。

    去重规则在 store.create_incident 里：同一 run + 同一错误码只留一条 pending，
    避免模型重试时刷出一串重复待办（真实系统里这会直接把待办列表冲爆）。
    """
    role, name, hours, note = assign(code)
    due = datetime.now() + timedelta(hours=hours)
    reason_text = (reason or "").strip()
    if sku:
        reason_text = f"[{sku}] {reason_text}"

    row = store.create_incident(
        run_id=run_id, session_id=session_id, store_id=store_id, tool=tool,
        code=code, reason=reason_text, handler_role=role, handler_name=name,
        due_at=due.isoformat(timespec="minutes"),
    )
    return {
        "id": row["id"],
        "code": code,
        "reason": reason_text,
        "handler_role": role,
        "handler_name": name,
        "due_at": row["due_at"],
        "due_text": due.strftime("%m-%d %H:%M"),
        "note": note,
        # 一句话给店长看：这条为什么停、谁接手、什么时候前有结果
        "text": f"已转交 {role} {name}，{due.strftime('%m-%d %H:%M')} 前处理：{note}",
    }
