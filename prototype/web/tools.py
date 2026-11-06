#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工具层 tools.py（智能体阶段：Function Calling 规范 + 流程门禁）
================================================================
设计原则：门禁做在代码里，不靠模型自觉。

四层防御：
  ① 系统提示词工作流契约（软约束，见 db.AGENT_PROMPT）
  ② 会话状态机（本文件）：校验工具调用顺序，跳步直接拒绝
  ③ 校验令牌（本文件）：validate_replenishment 通过才签发，create_draft 强校验
  ④ 人工确认（服务层）：草稿只在界面由店长点「确认提交」，模型无权提交

工具清单：
  只读（5）
    search_sku             商品编码检索
    get_sellable_inventory 系统库存 / 可售库存 / 后仓货架分布（账实不一致 → blocking）
    check_promotion_status 促销与变价状态、促销版本（需人工确认）
    get_sales_context      销量依据（零销量 / 编码未归并 → blocking）
    get_delivery_schedule  配送日历与线路容量
    get_replenishment_policy 起订量 / 整箱规格 / 分配上限（制度数值）
    search_policy_docs     制度条文混合检索（RAG）
  确定性校验（1，只读但签发令牌）
    validate_replenishment 四项校验，通过则签发令牌（15 分钟，绑定门店+SKU+数量+日期）
  写操作（1，强确认）
    create_replenishment_draft 创建补货申请草稿（唯一写工具）

阶段 2 迁移目标：app/tools/（按模块拆分）+ app/agents/orchestrator.py（编排循环）
"""

import json
import re
import time
import uuid
from datetime import date, datetime, timedelta

import db

# 校验令牌有效期（秒）：15 分钟
TOKEN_TTL = 900

# ============================================================
# 工具风险分级（执行框架据此决定「要不要人工确认」）
#   read   只读查询，可并行、可重复
#   verify 只读但签发凭证（校验令牌），不可乱调
#   write  写操作，必须在执行前中断等人工确认
# 说明：分级写在代码里而不是提示词里——模型无权决定自己是否需要确认。
# ============================================================

TOOL_RISK = {
    "search_sku": "read",
    "get_sku_context": "read",
    "get_sellable_inventory": "read",
    "check_promotion_status": "read",
    "get_sales_context": "read",
    "get_replenishment_policy": "read",
    "get_delivery_schedule": "read",
    "search_policy_docs": "read",
    "suggest_quantity": "read",
    "validate_replenishment": "verify",
    "create_replenishment_draft": "write",
}

# 执行框架在调用这些工具前会暂停（Human-in-the-loop）
HITL_TOOLS = {name for name, level in TOOL_RISK.items() if level == "write"}

# ============================================================
# 工具元信息（前端展示用的中文标签）
# ============================================================

TOOL_LABELS = {
    "search_sku": "商品编码检索",
    "get_sku_context": "补货上下文（库存/促销/销量/制度/配送）",
    "get_sellable_inventory": "可售库存核对",
    "check_promotion_status": "促销状态确认",
    "get_sales_context": "销量依据核对",
    "get_replenishment_policy": "制度参数查询",
    "get_delivery_schedule": "配送日历查询",
    "search_policy_docs": "制度条文检索",
    "suggest_quantity": "建议量计算",
    "validate_replenishment": "四项校验",
    "create_replenishment_draft": "创建补货草稿",
}

# ============================================================
# Function Calling 规范（OpenAI 兼容 tools 数组）
# ============================================================

TOOLS_SPEC = [
    {
        "type": "function",
        "function": {
            "name": "search_sku",
            "description": "按商品名称或编码模糊检索商品，返回 SKU 编码、名称、规格。用户只说了商品名时必须先调用本工具确认编码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "商品名称关键词或编码，如「矿泉水」「A001」"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sellable_inventory",
            "description": (
                "查询门店某商品的可售库存明细（系统库存、可售库存、后仓数量、货架数量、快照日期）。"
                "若系统库存与可售库存不一致（后仓有货未上架等），返回 blocking=true，"
                "此时必须先请店长人工核对后仓与货架，确认后再次调用并把 human_confirmed 设为 true。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sku": {"type": "string", "description": "商品编码"},
                    "human_confirmed": {
                        "type": "boolean",
                        "description": "店长是否已人工核对并确认账实一致，默认 false",
                    },
                },
                "required": ["sku"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_promotion_status",
            "description": (
                "查询商品当前是否处于促销或临时变价窗口，返回促销版本号与历史销量是否可用的判断。"
                "促销期必须由店长确认促销版本号（把版本号传给 confirm_version）；"
                "促销刚开始（样本不足 3 天）时返回 blocking=true，属于促销截断，不得生成补货建议。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sku": {"type": "string", "description": "商品编码"},
                    "confirm_version": {
                        "type": "string",
                        "description": "店长确认的促销版本号；未确认则不传",
                    },
                },
                "required": ["sku"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sales_context",
            "description": (
                "查询商品近 7/30 天销量与依据可用性。若近 30 天零销量、无销售记录或新旧编码未归并，"
                "返回 blocking=true（依据缺失），不得据此推算建议量。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sku": {"type": "string", "description": "商品编码"},
                    "days": {"type": "integer", "description": "统计窗口天数，默认 7，可用 7 或 30"},
                },
                "required": ["sku"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_replenishment_policy",
            "description": "查询商品的补货制度参数：最小起订量（MOQ）、整箱规格、分配上限，并返回制度条文引用。",
            "parameters": {
                "type": "object",
                "properties": {"sku": {"type": "string", "description": "商品编码"}},
                "required": ["sku"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_delivery_schedule",
            "description": "查询门店配送日历（周一/三/五）与线路容量余量，返回下一个可用配送日；可选校验某个期望配送日是否可发。",
            "parameters": {
                "type": "object",
                "properties": {
                    "delivery_date": {
                        "type": "string",
                        "description": "期望配送日 YYYY-MM-DD，可选",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sku_context",
            "description": (
                "【补货申请第 2 步，优先用这个】一次取回该商品补货决策所需的全部只读上下文："
                "可售库存与账实一致性、促销状态、近 7 天销量、起订量/整箱/分配上限、配送日历与线路余量。"
                "用 search_sku 拿到编码后调用本工具，不要再逐个调用 get_sellable_inventory / "
                "check_promotion_status / get_sales_context / get_replenishment_policy / get_delivery_schedule"
                "（那样会耗尽 5 轮预算）。任何一项不满足门禁（如账实不一致）会原样返回 blocking。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sku": {"type": "string", "description": "商品编码，如 A002"},
                    "delivery_date": {
                        "type": "string",
                        "description": "期望配送日 YYYY-MM-DD，可选；不传则返回最近可发日",
                    },
                },
                "required": ["sku"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_policy_docs",
            "description": "在补货制度知识库中做混合检索（BM25+向量），返回最相关条款原文。回答制度类问题时必须调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词或问题"},
                    "top_k": {"type": "integer", "description": "返回条数，默认 3"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "suggest_quantity",
            "description": (
                "按制度参数计算建议补货量（确定性计算，不要自己心算）："
                "建议量 = 向上取整到整箱(日均销量×覆盖天数 − 可售库存)，并限制在最小起订量与分配上限之间。"
                "前置条件：已完成库存核对、促销状态确认与销量核对。"
                "返回的 suggested_qty 直接作为 validate_replenishment 的 qty。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sku": {"type": "string", "description": "商品编码"},
                    "coverage_days": {"type": "integer", "description": "覆盖天数，默认 3 天"},
                },
                "required": ["sku"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_replenishment",
            "description": (
                "对补货意图做四项硬校验：① 不低于最小起订量 ② 符合整箱倍数 "
                "③ 不超过分配上限 ④ 配送日在日历内且线路容量足够。"
                "全部通过才签发校验令牌（validation_token），该令牌是创建草稿的唯一凭证。"
                "前置条件：本 SKU 已完成库存核对、促销确认、销量核对。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sku": {"type": "string", "description": "商品编码"},
                    "qty": {"type": "integer", "description": "申请数量（件）"},
                    "delivery_date": {"type": "string", "description": "期望配送日 YYYY-MM-DD"},
                },
                "required": ["sku", "qty", "delivery_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_replenishment_draft",
            "description": (
                "创建补货申请草稿（唯一写操作，只创建草稿不提交）。"
                "必须携带 validate_replenishment 签发的 validation_token；"
                "账实不一致场景必须携带 stock_verified_by_human=true，"
                "促销期申请必须携带 promotion_version_confirmed=促销版本号。缺一即被拒绝。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sku": {"type": "string", "description": "商品编码"},
                    "qty": {"type": "integer", "description": "申请数量（件）"},
                    "delivery_date": {"type": "string", "description": "配送日 YYYY-MM-DD"},
                    "validation_token": {"type": "string", "description": "校验令牌"},
                    "stock_verified_by_human": {
                        "type": "boolean",
                        "description": "店长已人工核对该商品账实一致",
                    },
                    "promotion_version_confirmed": {
                        "type": "string",
                        "description": "促销期需填写店长确认的促销版本号；若本会话内已通过 check_promotion_status 确认过可留空",
                    },
                },
                "required": ["sku", "qty", "delivery_date", "validation_token"],
            },
        },
    },
]


# ============================================================
# 会话状态机（原型放内存；正式版应落 Redis/数据库）
# ============================================================

_SESSIONS = {}


def _sess(session_id: str) -> dict:
    """取（或初始化）某个会话的智能体状态。

    结构：
      steps: {sku: {"inventory": True, "inventory_confirmed": bool,
                    "promo": True, "promo_version": str,
                    "sales": True, "policy": True, "schedule": True,
                    "validated": True}}
      tokens: {token: {...}}
      drafts: [draft_id...]
    """
    st = _SESSIONS.get(session_id)
    if st is None:
        st = {"steps": {}, "tokens": {}, "drafts": []}
        _SESSIONS[session_id] = st
    return st


def _steps(session_id: str, sku: str) -> dict:
    st = _sess(session_id)
    return st["steps"].setdefault(sku, {})


def _mark(session_id: str, sku: str, key: str, value=True):
    _steps(session_id, sku)[key] = value


def reset_session(session_id: str):
    """清空会话状态（新建会话 / 调试用）。"""
    _SESSIONS.pop(session_id, None)


# ============================================================
# 返回结构统一约定
#    ok       工具是否正常执行
#    blocking 是否命中业务停止条件（命中即不得继续下一步）
#    message  给模型看的自然语言摘要（同时作为前端工具轨迹文案）
#    data     结构化数据
# ============================================================

def _ok(message: str, data=None, blocking=False, blocking_reason=None, **extra):
    res = {"ok": True, "blocking": blocking, "message": message, "data": data or {}}
    if blocking:
        res["blocking_reason"] = blocking_reason or message
    res.update(extra)
    return res


def _fail(code: str, message: str, blocking: bool = True, **extra):
    """工具失败返回。

    blocking=True  业务停止条件：模型必须停下说明原因，不得自行绕过（默认）
    blocking=False 可恢复失败：如 STEP_VIOLATION（前置步骤未完成）、NO_HIT（没检索到），
                   模型应按提示继续，而不是直接终止对话
    """
    res = {"ok": False, "blocking": blocking, "code": code, "message": message}
    if blocking:
        res["blocking_reason"] = message
    res.update(extra)
    return res


def _resolve_sku(value: str):
    """把模型传入的商品标识归一化为标准 SKU 编码。

    模型在多轮对话里常常直接传「鲜牛奶」这种名称（而不是 A002），
    这里统一解析：编码精确匹配 → 名称/别名模糊匹配（唯一命中才算成功）。
    解析不了返回 None，由各工具给出「请先调用 search_sku」的提示。
    """
    v = (value or "").strip()
    if not v:
        return None
    with db.get_db() as conn:
        row = conn.execute("SELECT sku FROM skus WHERE sku = ?", (v.upper(),)).fetchone()
        if row:
            return row["sku"]
        # 旧编码/套装码 → 主编号（数据工程师归并后写入 sku_code_map）
        # 好处：店长或报表里出现旧码时，不必先纠正就能继续核对
        mapped = conn.execute("SELECT main_code FROM sku_code_map WHERE alias_code = ?",
                              (v.upper(),)).fetchone()
        if mapped:
            return mapped["main_code"]
        rows = conn.execute(
            "SELECT sku FROM skus WHERE name LIKE ? OR IFNULL(alias,'') LIKE ?",
            (f"%{v}%", f"%{v}%"),
        ).fetchall()
    return rows[0]["sku"] if len(rows) == 1 else None


def _need_sku(value: str):
    """归一化失败时的统一错误（提示模型先查编码）。"""
    return _fail("SKU_NOT_FOUND",
                 f"无法确定商品「{value}」的编码，请先调用 search_sku 确认商品编码")


# ============================================================
# 数量意图抽取（框架层确定性解析，不依赖模型）
# ============================================================

# 匹配「20 箱 / 10盒 / 三箱」这类明确数量；排除「近 7 天」这类非申请量语境
_QTY_RE = re.compile(r"([0-9]{1,4}|[一二两三四五六七八九十]{1,3})\s*(箱|盒|件|瓶|包|袋)")

_CN_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(s: str):
    """阿拉伯数字或中文数字转整数；解析不了返回 0。

    支持「三」「十」「十五」「二十」「二十三」这类日常说法，超出范围返回 0 视为没提数量。
    """
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):                      # 十五
        return 10 + _CN_DIGIT.get(s[1:], 0)
    if "十" in s:                               # 二十 / 二十三
        a, _, b = s.partition("十")
        return _CN_DIGIT.get(a, 1) * 10 + (_CN_DIGIT.get(b, 0) if b else 0)
    return _CN_DIGIT.get(s[-1], 0) if len(s) == 1 else 0


def extract_requested_qty(text: str):
    """从店长原话里抽取「明确指定的补货数量」，解析不出返回 None。

    返回 {"count": 20, "unit": "箱", "text": "20 箱"}

    为什么数量必须在框架层解析、不能靠提示词：
      实测 GLM-4-Flash 会无视「店长已给数量就不要调用 suggest_quantity」这条约束，
      先调 suggest_quantity 拿到系统建议量（12 盒），再用它覆盖店长的明确要求（20 箱），
      确认卡上就变成「1 箱」。店长若没逐字核对就点确认，等于订错货。
      数量意图是确定性的字符串事实，正则比模型可靠得多。

    只在「疑问句之外」的数量才算数——「20 箱够不够」这类询问由模型判断，
    本函数仍会抽到 20，但只用于「提示模型按这个量校验」，不强制写库，
    因此误抽不会造成错误订单，最坏是多给一条提示。
    """
    if not text:
        return None
    m = _QTY_RE.search(text)
    if not m:
        return None
    n = _cn_to_int(m.group(1))
    if n <= 0:
        return None
    return {"count": n, "unit": m.group(2), "text": m.group(0).strip()}


def _qty_from_request(req, sku_row):
    """把店长要求的数量换算成该商品的库存单位件数。

    返回 (qty, note)：
      qty  —— 换算后的件数；无法换算时为 None
      note —— 换算说明（用于回答与日志）
    单位不匹配（如商品按「盒」卖、店长说「20 件」）时返回 None，不做任何干预。
    """
    if not req:
        return None, ""
    unit = (sku_row["unit"] or "件")
    bs = int(sku_row["box_size"] or 1) or 1
    if req["unit"] == "箱":
        qty = req["count"] * bs
        return qty, f"{req['count']} 箱 × {bs} {unit}/箱 = {qty} {unit}"
    if req["unit"] == unit:
        return req["count"], f"{req['count']} {unit}"
    return None, ""


# ============================================================
# 只读工具实现
# ============================================================

def search_sku(ctx, keyword: str):
    """商品检索：按名称/编码模糊匹配，返回候选列表。"""
    kw = (keyword or "").strip()
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT sku, name, spec, moq, box_size, alloc_cap FROM skus "
            "WHERE sku LIKE ? OR name LIKE ? OR IFNULL(alias,'') LIKE ? ORDER BY sku",
            (f"%{kw}%", f"%{kw}%", f"%{kw}%"),
        ).fetchall()
    if not rows:
        return _fail("SKU_NOT_FOUND", f"未找到与「{kw}」匹配的商品，请确认商品名称或编码")
    items = [dict(r) for r in rows]
    hit = items[0]
    return _ok(
        f"找到 {len(items)} 个候选商品，首个为 {hit['sku']} {hit['name']}（{hit['spec']}）",
        {"items": items, "sku": hit["sku"]},
    )


def get_sellable_inventory(ctx, sku: str, human_confirmed: bool = False):
    """核对系统库存与可售库存；不一致时要求人工确认（账实差异 8% 的根因）。"""
    raw = sku
    sku = _resolve_sku(raw)
    if not sku:
        return _need_sku(raw)
    store_id = ctx["store_id"]
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT i.*, s.name, s.unit FROM store_inventory i "
            "JOIN skus s ON s.sku = i.sku WHERE i.store_id = ? AND i.sku = ?",
            (store_id, sku),
        ).fetchone()
    if not row:
        return _fail("INVENTORY_NOT_FOUND", f"未找到门店 {store_id} 商品 {sku} 的库存数据，请确认数据是否已同步")

    data = dict(row)
    discrepancy = int(row["sys_qty"]) != int(row["sellable_qty"])

    if discrepancy and not human_confirmed:
        # 硬门禁：账实不一致必须先人工核对，不标记步骤完成
        return _ok(
            f"账实不一致：系统库存 {row['sys_qty']}，可售库存 {row['sellable_qty']}"
            f"（后仓 {row['backroom_qty']}，货架 {row['shelf_qty']}）。"
            "请店长核对后仓与货架后确认，再继续。",
            data,
            blocking=True,
            blocking_reason="账实不一致（系统库存≠可售库存），需店长人工核对后仓与货架（f44 停止条件）",
            needs_manual_confirm=True,
            code="STOCK_MISMATCH",
            # 停止不等于结束：给出人工可执行的下一步，避免店长只看到「不能办」
            manual_path=(f"先盘点后仓 {row['backroom_qty']} 与货架 {row['shelf_qty']}，"
                         f"与系统 {row['sys_qty']} 核对一致后，在确认卡上点「账实一致」再重新发起，"
                         "我会按可售库存重新计算"),
            reference_sellable=int(row["sellable_qty"] or 0),
        )

    _mark(ctx["session_id"], sku, "inventory", True)
    _mark(ctx["session_id"], sku, "inventory_confirmed", bool(human_confirmed) or not discrepancy)

    note = "（店长已人工确认账实一致）" if discrepancy and human_confirmed else ""
    return _ok(
        f"库存核对完成：系统 {row['sys_qty']} / 可售 {row['sellable_qty']}，"
        f"后仓 {row['backroom_qty']} / 货架 {row['shelf_qty']}{note}",
        data,
    )


def check_promotion_status(ctx, sku: str, confirm_version: str = ""):
    """确认促销/变价窗口与版本；促销期历史销量不可直接作为需求依据。"""
    raw = sku
    sku = _resolve_sku(raw)
    if not sku:
        return _need_sku(raw)
    today = date.today()
    with db.get_db() as conn:
        promo = conn.execute(
            "SELECT * FROM promotions WHERE sku = ? AND (store_id IS NULL OR store_id = ?) "
            "AND start_date <= ? AND end_date >= ? ORDER BY id DESC LIMIT 1",
            (sku, ctx["store_id"], str(today), str(today)),
        ).fetchone()

    if not promo:
        _mark(ctx["session_id"], sku, "promo", True)
        _mark(ctx["session_id"], sku, "promo_version", "")
        return _ok("该商品当前不在促销/变价窗口，历史销量可作为需求依据",
                   {"in_promo": False, "history_usable": True})

    start = datetime.strptime(promo["start_date"], "%Y-%m-%d").date()
    promo_days = (today - start).days + 1
    version = promo["version"]
    data = {"in_promo": True, "version": version, "kind": promo["kind"],
            "start_date": promo["start_date"], "end_date": promo["end_date"],
            "promo_days": promo_days, "history_usable": False, "note": promo["note"]}

    # 停止条件①：促销截断（样本不足 3 天，历史销量失效且无替代依据）
    if promo_days < 3:
        return _ok(
            f"该商品促销刚开始 {promo_days} 天，促销期销量样本不足，历史销量不可用，"
            "无法生成补货建议（促销截断）",
            data,
            blocking=True,
            blocking_reason="促销截断：促销样本不足 3 天，历史销量不可作为需求依据（f44 停止条件）",
            code="PROMO_TRUNCATED",
            manual_path=("促销前 3 天销量不足以建模，请把这个 SKU 交给补货计划员按促销计划人工给量，"
                         "或在促销结束后再让我按历史销量计算"),
        )

    # 促销版本需人工确认
    if (confirm_version or "").strip() != version:
        return _ok(
            f"该商品处于促销期（{promo['start_date']}~{promo['end_date']}，版本 {version}）。"
            "请店长确认促销版本后再继续",
            data,
            blocking=True,
            blocking_reason=f"促销版本未确认，需店长确认版本号 {version} 后才可引用销量",
            need_version_confirm=True,
            version=version,
            code="PROMO_VERSION_UNCONFIRMED",
            manual_path=(f"核对门店终端上的促销价签版本是否为 {version}，"
                         "确认后告诉我「版本 {0} 确认」，我再按该版本口径继续".format(version)),
        )

    _mark(ctx["session_id"], sku, "promo", True)
    _mark(ctx["session_id"], sku, "promo_version", version)
    return _ok(f"促销版本已确认：{version}（促销期将按确认后的口径折算需求）", data)


def sales_window(days: int, today=None):
    """销量统计窗口：已经结束的 N 个自然日，不含今天。

    今天还没过完，销量数据不完整——把它算进窗口会拉低日均（原口径偏低
    的一部分原因就在这里）；分母再改成「有记录天数」时，这个不完整的当天
    会更直接地把均值压下去。所以窗口和分母必须一起改，只改一个会换一种错法。

    返回 (since, end)，均为 YYYY-MM-DD 字符串。
    """
    end = (today or date.today()) - timedelta(days=1)
    return str(end - timedelta(days=days - 1)), str(end)


def avg_daily_from_rows(rows):
    """日均销量：分母只算「有销售记录的天数」，不按自然日天数摊。

    原口径 total / days 会把断货日当成「卖了 0 件」计入分母，日均被系统性低估，
    建议量随之下调、补了还是不够，而系统不会报错（与 f31 断货截断同源）。
    分母换成有记录天数后，断货日不再稀释日均；代价是若该期间确实断过货，
    日均会偏高——所以返回值带上覆盖天数，由调用方明确告知门店复核。

    返回 (total, avg_daily, covered_days)
    """
    total = sum(int(r["qty"] or 0) for r in rows)
    covered = len({r["sale_date"] for r in rows})
    avg = round(total / covered, 1) if covered else 0.0
    return total, avg, covered


def approval_check(suggested, requested, threshold: float = 0.5):
    """提交时的偏差判定：只回答「要不要线下找计划员确认」，不是审批。

    系统内没有审批路由、没有 approver 字段，allocated / rejected 两个状态也没有
    任何写入点，所以这里不能叫 approve。它只比较申请量与建议量的偏离幅度，
    超过阈值就提示人工复核，避免系统伪造一条自己走不通的流程。

    返回 (needs_approval, deviation)；deviation 为 None 表示缺依据、无法比较。
    """
    try:
        s, r = int(suggested or 0), int(requested or 0)
    except (TypeError, ValueError):
        return False, None
    if s <= 0 or r <= 0:
        return False, None
    dev = round(abs(r - s) / s, 4)
    return dev > threshold, dev


def get_sales_context(ctx, sku: str, days: int = 7):
    """销量依据核对：零销量 / 无记录 / 编码未归并 → 依据缺失，停止。"""
    raw = sku
    sku = _resolve_sku(raw)
    if not sku:
        return _need_sku(raw)
    days = 30 if int(days or 7) >= 30 else 7
    since, _end = sales_window(days)

    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT sale_date, qty FROM sales_daily WHERE store_id = ? AND sku = ? "
            "AND sale_date >= ? ORDER BY sale_date",
            (ctx["store_id"], sku, since),
        ).fetchall()
        sku_row = conn.execute("SELECT name, legacy_code, code_merged FROM skus WHERE sku = ?",
                               (sku,)).fetchone()
        # 旧码是否已被数据工程师归并到主编号（REQ-1 的最小切片）：
        # 只要映射表里有这条记录，就不必再阻断——归并工作量因此从「改 SKU 主表」
        # 降级为「插一行映射」，数据侧可以先跑起来再补历史。
        mapping = None
        if sku_row and sku_row["legacy_code"]:
            mapping = conn.execute(
                "SELECT main_code, note FROM sku_code_map WHERE alias_code = ?",
                (sku_row["legacy_code"],)).fetchone()

    total, avg, covered = avg_daily_from_rows(rows)
    data = {"days": days, "total": total, "avg_daily": avg,
            "records": len(rows), "since": since,
            "coverage_days": covered,
            "coverage_ratio": round(covered / days, 2) if days else 0.0}

    # 停止条件③：依据缺失（新旧编码未归并，且映射表里也没有）
    if sku_row and sku_row["legacy_code"] and not sku_row["code_merged"]:
        if mapping and mapping["main_code"] == sku:
            # 已归并到主编号：继续走正常口径，并在结果里说明依据来源
            data["code_merged_via_map"] = True
            data["legacy_code"] = sku_row["legacy_code"]
        else:
            return _ok("该商品存在未归并的旧编码，销量口径不完整，不能作为需求依据",
                       data, blocking=True,
                       blocking_reason="依据缺失：新旧编码未归并，销量口径不可用（f44 停止条件）",
                       code="CODE_NOT_MERGED",
                       legacy_code=sku_row["legacy_code"],
                       manual_path=(f"旧编码 {sku_row['legacy_code']} 尚未映射到主编号 {sku}，"
                                    "需要数据侧在编码映射表补一条记录；归并后本条自动解除，无需重启"))

    # 停止条件③：依据缺失（无销量记录 / 零销量）
    if not rows:
        return _ok(f"近 {days} 天无该商品销量记录（新品或未开售），无需求依据",
                   data, blocking=True,
                   blocking_reason="依据缺失：无历史销量记录，不能推算建议量（f44 停止条件）",
                   code="NO_SALES_RECORD",
                   manual_path=("确认是新上架商品还是数据未同步：新品可按同类商品首铺量人工给量，"
                                "数据问题请联系数据工程师补同步"))
    if total == 0:
        return _ok(f"近 {days} 天销量为 0，属于零销量截断，不能据此判定缺货或补货",
                   data, blocking=True,
                   blocking_reason="依据缺失：零销量（可能是停售/断货/未上架），不得推算建议量（f44 停止条件）",
                   code="ZERO_SALES",
                   manual_path=("零销量既可能是卖不动、也可能是缺货截断（f31）："
                                "先看货架是否空、是否停售，确认后告诉我原因，我再决定是否按人工经验给量"))

    _mark(ctx["session_id"], sku, "sales", True)
    _mark(ctx["session_id"], sku, "avg_daily", avg)
    # 口径必须随结果一起给出：分母改用有记录天数后，断货日不再稀释日均，
    # 但若该期间确实断过货，日均会被高估——门店需据此复核，不能当成纯事实。
    if covered < days:
        scope = (f"（按有销售记录的 {covered}/{days} 天计算；"
                 "缺失日若是断货会造成日均偏高、若是漏同步则偏低，请先核对原因）")
    else:
        scope = ""
    return _ok(f"销量依据核对完成：近 {days} 天合计 {total}，日均 {avg}{scope}", data)


def get_replenishment_policy(ctx, sku: str):
    """制度参数：MOQ / 整箱规格 / 分配上限（附条款引用，供回答引用数值）。"""
    raw = sku
    sku = _resolve_sku(raw)
    if not sku:
        return _need_sku(raw)
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT sku, name, spec, moq, box_size, alloc_cap, unit FROM skus WHERE sku = ?",
            (sku,),
        ).fetchone()
    if not row:
        return _fail("SKU_NOT_FOUND", f"商品 {sku} 不存在")

    _mark(ctx["session_id"], sku, "policy", True)
    return _ok(
        f"{row['name']}：最小起订量 {row['moq']} {row['unit']}，整箱规格 {row['spec']}，"
        f"分配上限 {row['alloc_cap']} {row['unit']}",
        dict(row),
        evidence="《补货制度》·「最小起订量与整箱规格」",
    )


def get_delivery_schedule(ctx, delivery_date: str = ""):
    """配送日历与线路容量：返回下一个可用配送日，并校验指定日期是否可发。"""
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM delivery_lines WHERE store_id = ?",
                           (ctx["store_id"],)).fetchone()
    if not row:
        return _fail("SCHEDULE_NOT_FOUND", f"未配置门店 {ctx['store_id']} 的配送线路")

    day_map = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
    allowed = {day_map[d.strip()] for d in (row["delivery_days"] or "").split(",") if d.strip()}
    today = date.today()
    next_date = None
    for i in range(0, 14):
        d = today + timedelta(days=i)
        if d.weekday() in allowed:
            next_date = d
            break

    remaining = int(row["capacity"]) - int(row["used"])
    data = {
        "store_id": row["store_id"], "line_name": row["line_name"],
        "delivery_days": row["delivery_days"], "capacity": row["capacity"],
        "used": row["used"], "remaining": remaining,
        "next_delivery_date": str(next_date) if next_date else None,
    }

    _mark(ctx["session_id"], "", "schedule", True)

    if delivery_date:
        try:
            want = datetime.strptime(delivery_date, "%Y-%m-%d").date()
        except ValueError:
            return _fail("DATE_INVALID", "配送日格式应为 YYYY-MM-DD，请重新提供")
        data["requested_date"] = delivery_date
        data["requested_valid"] = (want.weekday() in allowed and want >= today)
        if not data["requested_valid"]:
            names = {"MON": "周一", "WED": "周三", "FRI": "周五"}
            day_names = "、".join(names.get(d.strip(), d.strip()) for d in row["delivery_days"].split(","))
            return _ok(
                f"{delivery_date} 不在配送日历（{day_names}发车），最近可发日为 {next_date}",
                data,
            )

    return _ok(
        f"配送线路 {row['line_name']}：{row['delivery_days']} 发车，"
        f"容量 {row['capacity']} 件、已占 {row['used']} 件、余量 {remaining} 件，"
        f"下一个可发日 {next_date}",
        data,
    )


def get_sku_context(ctx, sku: str, delivery_date: str = ""):
    """补货上下文聚合查询：一次返回决策所需的全部只读数据。

    为什么需要这个工具（真实模型暴露的问题，不是设计洁癖）：
      GLM-4-Flash 实测每轮只发**一个** tool_call，即使提示词明确要求并行合并。
      原本「库存 + 促销 + 销量 + 制度参数 + 配送日历」要占 5 轮，
      再加四项校验和建草稿，必然撞上 5 轮上限——真实对话里补货链路走不完。
      业务上这五项本来就该同时看（缺任一项都不能算量），
      合并成一个工具后：第 2 轮就能拿到全部上下文，预算留给校验与建单。

    与细粒度工具的分工：
      细粒度工具保留，用于「只问某一项」的追问（如「这个品促销到几号」）。
      本工具只读、幂等，不写入任何业务状态。

    停止条件透传：
      任一子查询返回 blocking（账实不一致、销量依据缺失、促销版本待确认），
      立即原样返回该结果，绝不把半套数据交给模型——否则模型会拿残缺数据硬算。
    """
    raw = sku
    sku = _resolve_sku(raw)
    if not sku:
        return _need_sku(raw)

    subs = [
        ("inventory", "get_sellable_inventory", {"sku": sku}),
        ("promotion", "check_promotion_status", {"sku": sku}),
        ("sales", "get_sales_context", {"sku": sku, "days": 7}),
        ("policy", "get_replenishment_policy", {"sku": sku}),
        ("delivery", "get_delivery_schedule",
         {"delivery_date": delivery_date} if delivery_date else {}),
    ]

    data, failed = {}, []
    for key, name, args in subs:
        r = call_tool(name, args, ctx)
        if r.get("blocking"):
            return r          # 停止条件原样上抛（含 blocking_reason 与已取到的数据）
        if not r.get("ok"):
            failed.append(f"{TOOL_LABELS.get(name, name)}：{r.get('message', '')}")
        data[key] = r.get("data") or {}

    if failed:
        # 可恢复失败：模型可以针对缺的那一项单独重查，不必整段重来
        return _fail("CONTEXT_PARTIAL", "上下文未取全：" + "；".join(failed),
                     blocking=False, data=data)

    inv, pol = data["inventory"], data["policy"]
    promo, sale, dlv = data["promotion"], data["sales"], data["delivery"]

    try:
        sellable = int(inv.get("sellable_qty") or 0)
    except (TypeError, ValueError):
        sellable = 0
    try:
        avg = float(sale.get("avg_daily") or 0)
    except (TypeError, ValueError):
        avg = 0.0

    # 库存状态：缺货 / 偏低（不足 2 天销量）/ 正常
    if sellable <= 0:
        stock_status = "缺货"
    elif avg > 0 and sellable <= avg * 2:
        stock_status = "库存偏低（不足 2 天销量）"
    else:
        stock_status = "正常"

    data.update({
        "sku": sku,
        "name": pol.get("name") or inv.get("name"),
        "unit": pol.get("unit") or inv.get("unit"),
        "box_size": pol.get("box_size"),
        "moq": pol.get("moq"),
        "alloc_cap": pol.get("alloc_cap"),
        "stock_status": stock_status,
    })

    summary = (
        f"{data['name']}（{sku}）：可售 {sellable} {data['unit']}，{stock_status}；"
        f"近 7 天日均 {avg}；"
        + ("促销中，历史销量不可直接作为需求依据；" if promo.get("in_promo") else "非促销期；")
        + f"起订量 {pol.get('moq')}、整箱 {pol.get('box_size')}、分配上限 {pol.get('alloc_cap')}；"
        f"最近可发日 {dlv.get('next_delivery_date')}，线路余量 {dlv.get('remaining')} 件"
    )

    # ---- 店长已明确指定数量：把换算结果一并给出，堵住「用建议量覆盖客户需求」这个坑 ----
    # 真实模型实测：不去调 suggest_quantity 的指令它不一定听，但如果第 2 轮就拿到
    # 「20 箱 = 240 盒，且超上限，最多 120 盒」，它会直接按这个数去校验。
    req = ctx.get("requested_qty")
    if req:
        qty, note = _qty_from_request(req, pol)
        if qty:
            bs = int(pol.get("box_size") or 1) or 1
            cap = int(pol.get("alloc_cap") or 0)
            data["requested"] = {
                "text": req["text"], "qty": qty, "unit": data["unit"],
                "boxes": -(-qty // bs), "note": note, "source": "店长原话",
            }
            summary += f"；店长已指定 {req['text']}（{note}）"
            if cap and qty > cap:
                max_boxes = cap // bs
                data["requested"].update({
                    "exceeds_cap": True, "max_qty": max_boxes * bs, "max_boxes": max_boxes,
                })
                summary += (f"，但超出分配上限 {cap} {data['unit']}，最多可申请 "
                            f"{max_boxes * bs} {data['unit']}（{max_boxes} 箱）；"
                            f"校验时直接用 {max_boxes * bs}，并在回答中说明为何没按原数量申请")
            else:
                summary += "，请直接用该数量校验，不要调用 suggest_quantity"

    return _ok(summary, data)


def search_policy_docs(ctx, query: str, top_k: int = 3):
    """制度条文混合检索（复用核心模块②）。"""
    kb = ctx.get("kb")
    if kb is None or kb.is_empty():
        return _fail("KB_EMPTY", "制度知识库为空，请联系管理员导入文档")
    hits = kb.search((query or "").strip(), top_k=int(top_k or 3))

    # 检索为空 = 制度里确实没有这个话题。必须显式告知模型，否则它会拿
    # 首页的 top_k 条硬编答案（历史缺陷：3 条陷阱题全部编出条款）。
    if not hits:
        ctx["last_hits"] = []
        reason = getattr(kb, "last_miss_reason", "") or "无相关条款"
        return _fail(
            "NO_HIT",
            f"制度知识库中未检索到与该问题相关的条款（{reason}）。"
            "请直接回答「制度中未找到相关规定」，不要引用其他章节充当依据，也不要编造条款编号。",
            blocking=False)

    out = [
        {"rank": h["rank"], "doc": h["doc_title"], "section": h["section"],
         "snippet": h["chunk"][:120].replace("\n", " "), "score": h["score"]}
        for h in hits
    ]
    # 把原始 hits 暂存到 ctx，服务层据此生成前端引用卡片与审计标记
    ctx["last_hits"] = hits
    summary = "；".join(f"《{h['doc']}》·{h['section']}" for h in out)
    mode = getattr(kb, "last_mode", "hybrid")
    note = "（纯词法检索）" if mode == "lexical" else ""
    return _ok(f"检索到 {len(out)} 条相关条款{note}：{summary}", {"hits": out})


# ============================================================
# 校验工具（只读，但签发令牌）
# ============================================================

def _require_steps(session_id: str, sku: str, steps) -> list:
    """检查前置步骤是否完成，返回缺失的中文名称列表。"""
    st = _steps(session_id, sku)
    names = {"inventory": "库存核对", "promo": "促销状态确认", "sales": "销量依据核对"}
    return [names[k] for k in steps if not st.get(k)]


def suggest_quantity(ctx, sku: str, coverage_days: int = 3):
    """确定性计算建议补货量（LLM 不参与算术，避免算错）。"""
    # 店长已明确指定数量 → 拒绝用系统建议量覆盖客户需求。
    # 真实故障：模型调了本工具，把「20 箱」算成建议量 12 盒，草稿变成「1 箱」。
    # 提示词约束不住的行为，用确定性门禁兜住。
    req = ctx.get("requested_qty")
    if req:
        return _fail(
            "QTY_SPECIFIED",
            f"店长已明确要求补 {req['text']}，本工具的建议量不得覆盖该要求。"
            f"请按整箱规格换算成件数后直接调用 validate_replenishment（数量见 get_sku_context 返回的 requested）。"
            f"只有店长问「该补多少」时才使用建议量。",
            blocking=False)

    raw = sku
    sku = _resolve_sku(raw)
    if not sku:
        return _need_sku(raw)
    sid = ctx["session_id"]
    st = _steps(sid, sku)

    # 前置：库存、促销、销量必须已核对（促销会改变需求口径，不能跳过）
    step_tool = {"inventory": "get_sellable_inventory",
                 "promo": "check_promotion_status",
                 "sales": "get_sales_context"}
    missing = [k for k in ("inventory", "promo", "sales") if not st.get(k)]
    if missing:
        names = {"inventory": "库存核对", "promo": "促销状态确认", "sales": "销量依据核对"}
        return {
            "ok": False, "blocking": False, "code": "STEP_VIOLATION",
            "message": "计算建议量前需先完成 " + "、".join(names[k] for k in missing),
            "next_action": {"tool": step_tool[missing[0]], "args": {"sku": sku}},
        }

    with db.get_db() as conn:
        sku_row = conn.execute("SELECT * FROM skus WHERE sku = ?", (sku,)).fetchone()
        inv = conn.execute("SELECT * FROM store_inventory WHERE store_id = ? AND sku = ?",
                           (ctx["store_id"], sku)).fetchone()
    if not sku_row or not inv:
        return _fail("SKU_NOT_FOUND", f"商品 {sku} 或其库存数据不存在")

    coverage = max(1, int(coverage_days or 3))
    avg = float(st.get("avg_daily") or 0)
    sellable = int(inv["sellable_qty"] or 0)
    box = int(sku_row["box_size"] or 1)
    moq = int(sku_row["moq"] or 0)
    cap = int(sku_row["alloc_cap"] or 0)

    raw_need = avg * coverage - sellable          # 覆盖 N 天所需 − 可售库存
    if raw_need <= 0:
        return _ok(
            f"可售库存 {sellable} 件已可覆盖 {coverage} 天需求（日均 {avg}），建议无需补货",
            {"suggested_qty": 0, "avg_daily": avg, "sellable_qty": sellable,
             "coverage_days": coverage, "raw_need": round(raw_need, 1),
             "formula": f"{avg}×{coverage}−{sellable} ≤ 0"},
        )

    # 向上取整到整箱，再抬到 MOQ 之上，最后压到分配上限之内
    boxes = int(-(-raw_need // box))             # 向上取整
    qty = boxes * box
    if qty < moq:
        boxes = int(-(-moq // box))
        qty = boxes * box
    if cap and qty > cap:
        boxes = cap // box
        qty = boxes * box

    if qty <= 0:
        return _ok("按制度参数计算后无可补数量（受分配上限约束）",
                   {"suggested_qty": 0, "avg_daily": avg, "sellable_qty": sellable},
                   blocking=True, blocking_reason="分配上限内无可补数量",
                   code="NO_QTY_WITHIN_CAP",
                   manual_path=("可售库存已接近上限，本周期无法再补：需要走跨店调拨时由区域经理批准，"
                                "或等下一个周期额度释放（f22）"))

    return _ok(
        f"建议补货量 {qty} {sku_row['unit']}（{boxes} 箱）：日均 {avg} × 覆盖 {coverage} 天 "
        f"− 可售 {sellable} = {round(raw_need, 1)}，向上取整到整箱（{sku_row['spec']}）",
        {"suggested_qty": qty, "boxes": boxes, "avg_daily": avg, "sellable_qty": sellable,
         "coverage_days": coverage, "raw_need": round(raw_need, 1),
         "formula": f"({avg}×{coverage}−{sellable}) ↑ 整箱{box} = {qty}"},
    )


def validate_replenishment(ctx, sku: str, qty, delivery_date: str):
    """四项硬校验 + 签发令牌。任何一项不通过都不签发。"""
    raw = sku
    sku = _resolve_sku(raw)
    if not sku:
        return _need_sku(raw)
    # ---- 前置步骤门禁（代码强制，模型跳步直接拒绝）----
    missing = _require_steps(ctx["session_id"], sku, ("inventory", "promo", "sales"))
    if missing:
        # 注意：这是「可纠正错误」而非停止条件——同时给出下一步该调什么，避免模型无从下手
        step_tool = {
            "inventory": ("get_sellable_inventory", {"sku": sku}),
            "promo": ("check_promotion_status", {"sku": sku}),
            "sales": ("get_sales_context", {"sku": sku}),
        }
        st = _steps(ctx["session_id"], sku)
        next_key = next((k for k in ("inventory", "promo", "sales") if not st.get(k)), None)
        tool_name, tool_args = step_tool.get(next_key, (None, {}))
        return {
            "ok": False,
            "blocking": False,
            "code": "STEP_VIOLATION",
            "message": "校验前置条件未满足：请先完成 " + "、".join(missing)
                       + "，补齐后再次调用 validate_replenishment",
            "missing_steps": missing,
            "next_action": {"tool": tool_name, "args": tool_args},
        }

    try:
        qty = int(qty)
    except (TypeError, ValueError):
        return _fail("PARAM_INVALID", "申请数量必须为整数（件）")

    with db.get_db() as conn:
        sku_row = conn.execute("SELECT * FROM skus WHERE sku = ?", (sku,)).fetchone()
        line = conn.execute("SELECT * FROM delivery_lines WHERE store_id = ?",
                            (ctx["store_id"],)).fetchone()
        inv = conn.execute("SELECT * FROM store_inventory WHERE store_id = ? AND sku = ?",
                           (ctx["store_id"], sku)).fetchone()
    if not sku_row:
        return _fail("SKU_NOT_FOUND", f"商品 {sku} 不存在")

    violations = []

    # ① 最小起订量
    if qty < int(sku_row["moq"]):
        violations.append(f"申请量 {qty} 低于最小起订量 {sku_row['moq']} {sku_row['unit']}")

    # ② 整箱规格
    box = int(sku_row["box_size"] or 0)
    if box > 0 and qty % box != 0:
        ceil_box = (qty + box - 1) // box
        violations.append(
            f"申请量 {qty} 不符合整箱规格（{sku_row['spec']}），应调整为 {ceil_box * box}"
            f"（{ceil_box} 箱）"
        )

    # ③ 分配上限（超过时同时给出「按整箱压到上限内」的建议值，避免模型反复试数）
    cap = int(sku_row["alloc_cap"] or 0)
    if cap and qty > cap:
        cap_boxes = cap // box if box > 0 else 0
        cap_qty = cap_boxes * box
        if cap_qty > 0:
            violations.append(
                f"申请量 {qty} 超过分配上限 {cap} {sku_row['unit']}，"
                f"按整箱（{sku_row['spec']}）最多可申请 {cap_qty} {sku_row['unit']}"
                f"（{cap_boxes} 箱）"
            )
        else:
            violations.append(f"申请量 {qty} 超过分配上限 {cap} {sku_row['unit']}")

    # ④ 配送日 + 线路容量
    day_map = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
    allowed = {day_map[d.strip()] for d in (line["delivery_days"] or "").split(",") if d.strip()}
    try:
        want = datetime.strptime(delivery_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return _fail("DATE_INVALID", "配送日格式应为 YYYY-MM-DD")
    if want.weekday() not in allowed:
        violations.append(f"{delivery_date} 不在配送日历（{line['delivery_days']} 发车）")
    if want < date.today():
        violations.append(f"{delivery_date} 早于今天，无法排入配送")
    remaining = int(line["capacity"]) - int(line["used"])
    if qty > remaining:
        violations.append(f"申请量 {qty} 超过线路剩余容量 {remaining} 件")

    result = {
        "sku": sku, "sku_name": sku_row["name"], "qty": qty,
        "delivery_date": delivery_date, "passed": not violations,
        "violations": violations,
        "checks": {
            "moq": int(sku_row["moq"]), "box_size": box,
            "alloc_cap": int(sku_row["alloc_cap"]),
            "delivery_days": line["delivery_days"], "line_remaining": remaining,
        },
    }

    if violations:
        # 记录这次被拒的数量，供后续「预览确认卡」展示「原申请 X → 调整为 Y」
        ctx["last_rejected_qty"] = qty
        ctx["last_rejected_violations"] = list(violations)
        return _ok("四项校验未通过：" + "；".join(violations), result,
                   blocking=True, blocking_reason="校验未通过，禁止创建补货草稿",
                   code="VALIDATION_FAILED",
                   violations=violations,
                   manual_path=("按提示调整数量或配送日即可重新校验；"
                                "如果确实需要超出上限，走计划员人工审批，不通过本系统建单（f22）"))

    # ---- 全部通过：签发令牌（绑定门店 + SKU + 数量 + 配送日，15 分钟有效）----
    token = uuid.uuid4().hex
    _sess(ctx["session_id"])["tokens"][token] = {
        "store_id": ctx["store_id"], "sku": sku, "qty": qty,
        "delivery_date": delivery_date, "issued_at": time.time(), "used": False,
    }
    _mark(ctx["session_id"], sku, "validated", True)

    return _ok(
        f"四项校验全部通过（起订量/整箱/上限/配送日），已签发校验令牌，有效期 15 分钟",
        result, validation_token=token,
    )


# ============================================================
# 写操作工具（唯一）：创建补货草稿
# ============================================================

def preview_replenishment_draft(ctx, sku: str, qty, delivery_date: str,
                                validation_token: str = "", **_ignored):
    """【执行框架专用，不是模型可调用的工具】建草稿前的「干跑」预览。

    为什么需要它：
      执行框架在 create_replenishment_draft 前必须中断，把「将要写入的内容」
      先给店长看。但如果直接执行工具，写就发生了——所以需要一个只读的试算函数，
      用完全相同的入参算出：SKU、数量、整箱换算、预计配送日、四项校验状态。

    与 create_replenishment_draft 的关系：
      同一个 sku/qty/delivery_date，预览通过 → 确认后执行必然成功（除非令牌过期）。
      预览本身不写库、不消耗令牌。
    """
    raw = sku
    sku = _resolve_sku(raw)
    if not sku:
        return _need_sku(raw)

    try:
        qty = int(qty)
    except (TypeError, ValueError):
        return _fail("PARAM_INVALID", "申请数量必须为整数（件）")

    with db.get_db() as conn:
        sku_row = conn.execute("SELECT * FROM skus WHERE sku = ?", (sku,)).fetchone()
        inv = conn.execute("SELECT * FROM store_inventory WHERE store_id = ? AND sku = ?",
                           (ctx["store_id"], sku)).fetchone()
        line = conn.execute("SELECT * FROM delivery_lines WHERE store_id = ?",
                            (ctx["store_id"],)).fetchone()
    if not sku_row:
        return _fail("SKU_NOT_FOUND", f"商品 {sku} 不存在")
    if not line:
        return _fail("SCHEDULE_NOT_FOUND", f"未配置门店 {ctx['store_id']} 的配送线路")

    box = int(sku_row["box_size"] or 1)
    box = box if box > 0 else 1
    # 整箱换算：向上取整得到箱数；exact=False 说明模型给的数量不是整箱（会被门禁拒绝）
    boxes = -(-qty // box)
    exact = (qty % box == 0)
    unit = sku_row["unit"] or "件"

    # ---- 预计配送日：校验是否落在配送日历内 ----
    day_names = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    day_map = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
    allowed = {day_map[d.strip()] for d in (line["delivery_days"] or "").split(",") if d.strip()}
    try:
        want = datetime.strptime(delivery_date, "%Y-%m-%d").date()
        weekday = day_names[want.weekday()]
        date_ok = want.weekday() in allowed and want >= date.today()
    except (TypeError, ValueError):
        want, weekday, date_ok = None, "", False

    remaining = int(line["capacity"]) - int(line["used"])
    moq = int(sku_row["moq"] or 0)
    cap = int(sku_row["alloc_cap"] or 0)

    # ---- 四项校验（只用于展示，真正的门禁仍在 validate / create 里）----
    checks = [
        {"name": "最小起订量", "ok": qty >= moq,
         "detail": f"申请 {qty} {unit} ≥ 起订量 {moq} {unit}"},
        {"name": "整箱规格", "ok": exact,
         "detail": f"{qty} ÷ {box} {unit}/箱 = {qty / box:g} 箱"
                   + ("" if exact else f"，不是整箱（应取整为 {boxes * box} {unit}）")},
        {"name": "分配上限", "ok": (not cap) or qty <= cap,
         "detail": f"申请 {qty} {unit} ≤ 上限 {cap} {unit}" if cap else "未设置上限"},
        {"name": "配送日历", "ok": date_ok,
         "detail": f"{delivery_date}（{weekday}）"
                   + ("在配送日历内" if date_ok else f"不可发，发车日 {line['delivery_days']}")},
        {"name": "线路容量", "ok": qty <= remaining,
         "detail": f"申请 {qty} {unit} ≤ 线路余量 {remaining} 件"},
    ]

    # ---- 令牌状态：确认卡上要能看出「校验是否已完成」----
    token_rec = _sess(ctx["session_id"])["tokens"].get(validation_token or "")
    token_state = "missing"
    if token_rec:
        if token_rec["used"]:
            token_state = "used"
        elif time.time() - token_rec["issued_at"] > TOKEN_TTL:
            token_state = "expired"
        elif (token_rec["sku"] != sku or token_rec["qty"] != qty
              or token_rec["delivery_date"] != delivery_date):
            token_state = "mismatch"
        else:
            token_state = "valid"

    # ---- 是否属于「数量被调整过」：让店长看到改了什么 ----
    adjust = None
    prev_qty = ctx.get("last_rejected_qty")
    if prev_qty and int(prev_qty) != qty:
        adjust = {
            "from_qty": int(prev_qty),
            "from_boxes": -(-int(prev_qty) // box),
            "to_qty": qty,
            "to_boxes": boxes,
            "reason": "；".join(ctx.get("last_rejected_violations") or []) or "按制度参数调整",
        }
    elif ctx.get("requested_qty"):
        # 模型看到上限提示后直接按上限取值（没有先被拒再重试），此时没有
        # last_rejected_qty，但店长仍需知道「我要 20 箱，实际报了 10 箱」的原因。
        # 不写清楚这一条，确认卡上只有 10 箱，店长会以为是系统按销量算的。
        req_qty, _note = _qty_from_request(ctx["requested_qty"], sku_row)
        if req_qty and req_qty != qty:
            adjust = {
                "from_qty": req_qty,
                "from_boxes": -(-req_qty // box),
                "to_qty": qty,
                "to_boxes": boxes,
                "reason": (f"店长原要求 {ctx['requested_qty']['text']}（{req_qty} {unit}），"
                           f"超出分配上限 {cap} {unit}，已按整箱压到 {qty} {unit}"),
            }

    # ---- 未满足数量：店长原要求 − 实际申请，且必须带原因（f52）----
    # 访谈 f52 原话：「需要给门店看预计到货与本次未满足数量」。
    # 只给「实际申请 120」而不说「你要 240、少了 120、因为上限」，店长无法向店员解释。
    shortfall = None
    desired = adjust["from_qty"] if adjust else None
    if not desired and ctx.get("requested_qty"):
        desired, _n = _qty_from_request(ctx["requested_qty"], sku_row)
    if desired and int(desired) > qty:
        limit_type = "分配上限" if (cap and int(desired) > cap) else "整箱规格/线路容量"
        shortfall = {
            "requested_qty": int(desired),
            "granted_qty": qty,
            "unmet_qty": int(desired) - qty,
            "limit_type": limit_type,
            "reason": (adjust or {}).get("reason") or f"受{limit_type}约束",
            "text": f"原要求 {desired} {unit}，实际可申请 {qty} {unit}，"
                    f"未满足 {int(desired) - qty} {unit}（{limit_type}）",
        }

    data = {
        "action": "create_replenishment_draft",
        "tool": "create_replenishment_draft",
        "store_id": ctx["store_id"],
        "sku": sku,
        "sku_name": sku_row["name"],
        "spec": sku_row["spec"],
        "unit": unit,
        "box_size": box,
        "qty": qty,
        "boxes": boxes,
        # 供前端直接展示的一句话换算，避免各端各写一套算法
        "box_text": f"{boxes} 箱 × {box} {unit}/箱 = {qty} {unit}",
        "qty_text": f"{qty} {unit}（{boxes} 箱）",
        "moq": moq,
        "alloc_cap": cap,
        "delivery_date": delivery_date,
        "delivery_weekday": weekday,
        "line_name": line["line_name"],
        "line_remaining": remaining,
        "inventory": {
            "sys_qty": inv["sys_qty"] if inv else None,
            "sellable_qty": inv["sellable_qty"] if inv else None,
            "backroom_qty": inv["backroom_qty"] if inv else None,
            "shelf_qty": inv["shelf_qty"] if inv else None,
        },
        "token_state": token_state,
        "checks": checks,
        "all_checks_ok": all(c["ok"] for c in checks),
        "adjust": adjust,
        "shortfall": shortfall,
    }

    # 预览不通过就不该弹确认卡（框架会把它当成一次普通的工具失败回填给模型）
    # 注意：blocking 保持 False —— 这是「可纠正错误」，模型应调整数量后重试，而不是停下
    if not data["all_checks_ok"]:
        bad = [c["name"] for c in checks if not c["ok"]]
        return {"ok": False, "blocking": False, "code": "PREVIEW_CHECK_FAILED",
                "message": "预览未通过：" + "、".join(bad) + "，请先调整数量或配送日",
                "data": {}, "preview": data}
    if token_state != "valid":
        return {"ok": False, "blocking": False, "code": "TOKEN_INVALID",
                "message": "校验令牌无效或与本次申请不一致，请重新执行 validate_replenishment",
                "data": {}, "preview": data}

    return _ok(
        f"预览：{sku_row['name']} {qty} {unit}（{boxes} 箱 × {box} {unit}/箱），"
        f"配送日 {delivery_date}（{weekday}），四项校验全部通过",
        data,
    )


def create_replenishment_draft(ctx, sku: str, qty, delivery_date: str,
                               validation_token: str,
                               stock_verified_by_human: bool = False,
                               promotion_version_confirmed: str = ""):
    """创建草稿：令牌 + 人工确认 + 促销版本三重硬门禁，缺一拒绝。"""
    raw = sku
    sku = _resolve_sku(raw)
    if not sku:
        return _need_sku(raw)
    sid = ctx["session_id"]
    st = _sess(sid)

    # ---- 门禁 1：令牌存在、未使用、未过期 ----
    rec = st["tokens"].get(validation_token or "")
    if not rec:
        return _fail("TOKEN_INVALID",
                     "校验令牌无效：必须先调用 validate_replenishment 通过校验后取得令牌")
    if rec["used"]:
        return _fail("TOKEN_USED", "该校验令牌已使用过，请重新校验")
    if time.time() - rec["issued_at"] > TOKEN_TTL:
        return _fail("TOKEN_EXPIRED", "校验令牌已过期（超过 15 分钟），请重新校验")

    # ---- 门禁 2：令牌内容必须与本次申请一致（防张冠李戴/篡改）----
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        return _fail("PARAM_INVALID", "申请数量必须为整数（件）")
    if (rec["sku"] != sku or rec["qty"] != qty
            or rec["delivery_date"] != delivery_date
            or rec["store_id"] != ctx["store_id"]):
        return _fail("TOKEN_MISMATCH",
                     "令牌与本次申请不一致（商品/数量/配送日被改动），请重新校验")

    # ---- 门禁 3：账实一致性人工确认 ----
    steps = _steps(sid, sku)
    if not steps.get("inventory_confirmed") and not stock_verified_by_human:
        return _fail("STOCK_NOT_VERIFIED",
                     "未经店长人工确认账实一致，不得创建补货草稿")

    # ---- 门禁 4：促销版本确认（参数优先，其次采用会话内已确认的版本）----
    today = date.today()
    with db.get_db() as conn:
        promo = conn.execute(
            "SELECT * FROM promotions WHERE sku = ? AND (store_id IS NULL OR store_id = ?) "
            "AND start_date <= ? AND end_date >= ? ORDER BY id DESC LIMIT 1",
            (sku, ctx["store_id"], str(today), str(today)),
        ).fetchone()
        sku_row = conn.execute("SELECT * FROM skus WHERE sku = ?", (sku,)).fetchone()
        inv = conn.execute("SELECT * FROM store_inventory WHERE store_id = ? AND sku = ?",
                           (ctx["store_id"], sku)).fetchone()
        line = conn.execute("SELECT * FROM delivery_lines WHERE store_id = ?",
                            (ctx["store_id"],)).fetchone()
        sales = conn.execute(
            "SELECT SUM(qty) AS total FROM sales_daily WHERE store_id = ? AND sku = ?",
            (ctx["store_id"], sku)).fetchone()

    if promo:
        confirmed = (promotion_version_confirmed or "").strip() or (steps.get("promo_version") or "")
        if confirmed != promo["version"]:
            return _fail("PROMO_VERSION_REQUIRED",
                         f"该商品处于促销期，必须确认促销版本 {promo['version']} 后才能创建草稿")

    # ---- 组装分配依据（可解释性：店长能看懂为什么是这个量）----
    avg_daily = steps.get("avg_daily") or 0
    basis = {
        "inventory": {
            "sys_qty": inv["sys_qty"], "sellable_qty": inv["sellable_qty"],
            "backroom_qty": inv["backroom_qty"], "shelf_qty": inv["shelf_qty"],
            "snapshot_date": inv["snapshot_date"],
        },
        "sales": {"total": int(sales["total"] or 0), "avg_daily": avg_daily},
        "policy": {"moq": sku_row["moq"], "box_size": sku_row["box_size"],
                   "alloc_cap": sku_row["alloc_cap"], "spec": sku_row["spec"]},
        "promotion": ({"version": promo["version"], "start": promo["start_date"],
                       "end": promo["end_date"], "confirmed": True} if promo else None),
        "delivery": {"date": delivery_date, "line": line["line_name"],
                     "remaining_capacity": int(line["capacity"]) - int(line["used"])},
        # 未满足数量在下面按「店长原要求 − 实际申请」补算（f52）：
        # 旧实现用「申请量 − 线路剩余容量」，而校验通过后这一项恒为 0，
        # 等于把门店最关心的「少给了多少」永远写成 0。
        "unsatisfied": 0,
        "reason": "按制度参数与配送日历校验通过后生成的补货建议",
    }
    validation = {
        "checks": {"moq": sku_row["moq"], "box_size": sku_row["box_size"],
                   "alloc_cap": sku_row["alloc_cap"], "passed": True},
        "token_issued_at": datetime.fromtimestamp(rec["issued_at"]).isoformat(timespec="seconds"),
    }

    # ---- 数量换算与调整说明（必须在 INSERT 之前算好，qty_requested 要用）----
    bs = int(sku_row["box_size"] or 1) or 1
    # 店长原话里的数量（换算成件）；解析不出则为 None。
    # qty_requested 落这个值、qty_suggested 落实际申请量，两者不同才看得出「被调整过」。
    req = ctx.get("requested_qty")
    req_qty, _req_note = _qty_from_request(req, sku_row) if req else (None, "")

    # 数量调整说明：店长原要求 ≠ 实际申请时必须显式记录。
    # 收尾文案与确认卡都依赖它——否则店长只看到「10 箱」，不知道为何不是自己要的「20 箱」
    adjust = None
    if req_qty and req_qty != qty:
        adjust = {
            "from_qty": req_qty, "from_boxes": -(-req_qty // bs),
            "to_qty": qty, "to_boxes": -(-qty // bs),
            "reason": (f"店长原要求 {req['text']}（{req_qty} {sku_row['unit']}），"
                       f"受起订量/整箱/分配上限/线路容量约束，调整为 {qty} {sku_row['unit']}"),
        }

    # ---- 未满足数量与原因（f52）----
    unmet_qty = max(0, (req_qty or qty) - qty)
    unit_txt = sku_row["unit"] or "件"
    shortfall = None
    if unmet_qty > 0:
        cap_qty = (int(sku_row["alloc_cap"] or 0) // bs) * bs if bs else 0
        limit_type = ("分配上限" if (cap_qty and req_qty > cap_qty) else "整箱规格/线路容量")
        shortfall = {
            "requested_qty": int(req_qty), "granted_qty": qty,
            "unmet_qty": unmet_qty, "limit_type": limit_type,
            "reason": (adjust or {}).get("reason") or f"受{limit_type}约束",
            "text": f"原要求 {req_qty} {unit_txt}，本次可申请 {qty} {unit_txt}，"
                    f"未满足 {unmet_qty} {unit_txt}（{limit_type}）",
        }
    basis["unsatisfied"] = unmet_qty
    basis["unsatisfied_reason"] = shortfall["reason"] if shortfall else ""

    with db.get_db() as conn:
        cur = conn.execute(
            "INSERT INTO replenishment_drafts (session_id, store_id, sku, sku_name, "
            "qty_suggested, qty_requested, delivery_date, basis, validation, status, "
            "created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            # qty_suggested = 实际写入的申请量；qty_requested = 店长原话要求量。
            # 两者不一致时，说明被制度参数压过（adjust 字段记录了原因）。
            (sid, ctx["store_id"], sku, sku_row["name"], qty,
             req_qty or qty, delivery_date,
             json.dumps(basis, ensure_ascii=False), json.dumps(validation, ensure_ascii=False),
             "draft", ctx.get("username") or ctx.get("user", {}).get("sub") or "", db.now()),
        )
        draft_id = cur.lastrowid

    rec["used"] = True                      # 令牌一次性
    st["drafts"].append(draft_id)

    draft = {
        "id": draft_id, "sku": sku, "sku_name": sku_row["name"], "qty": qty,
        "unit": sku_row["unit"] or "件",
        "boxes": -(-qty // bs),
        "box_text": (f"{-(-qty // bs)} 箱 × {bs} {sku_row['unit'] or '件'}/箱 = "
                     f"{qty} {sku_row['unit'] or '件'}"),
        "delivery_date": delivery_date, "store_id": ctx["store_id"],
        "status": "draft", "basis": basis, "adjust": adjust,
        "shortfall": shortfall,
    }
    return _ok(
        f"已创建补货草稿 #{draft_id}：{sku_row['name']} {qty} {sku_row['unit']}，"
        f"配送日 {delivery_date}。草稿需店长在界面确认后才会提交。",
        draft, draft=draft,
    )


# ============================================================
# 分发器
# ============================================================

_DISPATCH = {
    "search_sku": search_sku,
    "get_sku_context": get_sku_context,
    "get_sellable_inventory": get_sellable_inventory,
    "check_promotion_status": check_promotion_status,
    "get_sales_context": get_sales_context,
    "get_replenishment_policy": get_replenishment_policy,
    "get_delivery_schedule": get_delivery_schedule,
    "search_policy_docs": search_policy_docs,
    "suggest_quantity": suggest_quantity,
    "validate_replenishment": validate_replenishment,
    # 预览仅由执行框架调用（不在 TOOLS_SPEC 里，模型无法直接触发）
    "preview_replenishment_draft": preview_replenishment_draft,
    "create_replenishment_draft": create_replenishment_draft,
}


def call_tool(name: str, args: dict, ctx: dict):
    """执行一次工具调用，任何异常都转成结构化结果（不抛给上层）。"""
    fn = _DISPATCH.get(name)
    if fn is None:
        return _fail("TOOL_NOT_FOUND", f"未知工具：{name}")
    try:
        return fn(ctx, **(args or {}))
    except TypeError as e:
        return _fail("ARGUMENT_ERROR", f"工具参数不合法：{e}")
    except Exception as e:
        return _fail("TOOL_ERROR", f"工具执行异常：{e}")


def list_drafts(session_id: str = None, store_id: str = None, limit: int = 20):
    """查询草稿（服务层接口用）。"""
    sql = ("SELECT id, session_id, store_id, sku, sku_name, qty_suggested, qty_requested, "
           "delivery_date, status, created_at, submitted_at FROM replenishment_drafts WHERE 1=1")
    params = []
    if session_id:
        sql += " AND session_id = ?"
        params.append(session_id)
    if store_id:
        sql += " AND store_id = ?"
        params.append(store_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with db.get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
