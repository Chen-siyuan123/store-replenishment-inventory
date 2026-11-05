#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据层 db.py（管理后台批次 1：完整建表 + 种子数据）
================================================================
表清单：
  users             用户（店长/管理员，PBKDF2 密码哈希）
  prompt_templates  提示词模板（版本化，is_active 标记生效版本）
  kb_documents      制度知识库文档（批次 2 使用）
  kb_chunks         知识库切片（含 doc_id / section / embedding 二进制）
  sessions          会话（批次 1 起记录 user_id / store_id，行级权限锚点）
  messages          消息（批次 4 起写入 audit 异常标记）

兼容旧库：chat.db 已存在时自动 ALTER TABLE 补列，不丢数据。
"""

import os
import sqlite3
import hashlib
import secrets
from datetime import datetime, timedelta

from auth import hash_password  # 密码哈希复用认证模块的实现

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat.db")

DEFAULT_PROMPT = (
    "你是门店补货与库存协同制度助手，只根据提供的制度条文回答，"
    "涉及起订量、整箱规格、分配上限、配送日历、缺货口径时必须引用具体条文数值，"
    "不确定时明确说'制度中未找到'，回答末尾附引用来源，"
    "不得编造补货标准或替店长决定是否补货。"
)

# 智能体版系统提示词 = 原有制度助手约束 + 工作流契约（工具调用纪律）
# 契约单独抽成常量：即使管理员在后台切回基础模板，服务层也会自动补上，门禁语义不丢
AGENT_CONTRACT = """【补货工作流契约】

你可以调用工具查询制度、库存、配送日历、促销状态，并在校验通过后创建补货申请草稿。

【执行预算：最多 5 轮】
- 你最多有 5 轮决策机会（每轮 = 一次模型调用）。实测你每轮只会发出一个工具调用，所以**必须靠聚合工具省轮次**，不要指望一轮内并发多个工具。
- 轮次用尽后系统会强制结束，你将失去给出结论的机会，所以不要把轮次浪费在重复查询上。
- create_replenishment_draft 属于写操作：系统会在执行前强制暂停，把草稿内容（商品、数量、整箱换算、预计配送日）推给店长确认，确认后才真正写入。你不需要重复调用它，也不要在暂停前声称草稿已创建。

【补货申请标准路径（4 次工具调用，务必按此走）】
1. search_sku —— 确认商品编码（用户说商品名时必须先查编码）
2. get_sku_context —— 一次取回库存、促销、销量、制度参数、配送日历。
   这是聚合工具，等价于同时调用 get_sellable_inventory / check_promotion_status /
   get_sales_context / get_replenishment_policy / get_delivery_schedule。
   逐个调用会耗尽轮次导致任务失败；只有店长单独追问某一项时才用细粒度工具。
   返回 blocking 时按 blocking_reason 处理，不要继续下一步。
3. 数量处理：
   - 店长明确给了数量（如「补 20 箱」）→ 按整箱规格换算成件数（20 箱 × 12 盒/箱 = 240 盒），直接进入第 4 步，严禁调用 suggest_quantity
   - 店长问「该补多少」→ 调用 suggest_quantity 由系统计算，禁止自己心算
4. validate_replenishment —— 四项校验（起订量、整箱、分配上限、配送日与线路容量）。不通过时按返回的建议值调整后重新校验，不要用同一组参数重复提交
5. create_replenishment_draft —— 仅在拿到校验令牌后调用（系统会先中断等店长确认）

【硬性纪律】
- 工具返回 blocking=true 时，属于业务停止条件，必须立即停止后续步骤并向用户说明原因，禁止换个说法重试或自行给出建议量。
- 工具返回 ok=false 且 code=STEP_VIOLATION 时，不属于停止条件：说明前置步骤尚未完成，应按返回的 next_action 立即调用指定工具，然后重新调用 validate_replenishment。
- 工具返回 TOKEN_INVALID / TOKEN_MISMATCH / TOKEN_EXPIRED 时，必须重新完整校验，禁止猜测或复用旧令牌。
- 未经 get_sku_context（或 get_sellable_inventory）核对库存，不得谈论任何建议量。
- 未经 get_sku_context（或 check_promotion_status）确认促销版本，不得引用历史销量推算需求。
- 账实不一致（系统库存≠可售库存）时，必须请店长人工核对后仓与货架，得到明确确认后才可继续。
- validate_replenishment 未通过时，禁止调用 create_replenishment_draft；被拒绝后不得重复尝试绕过。
- 只能创建草稿，不得声称已提交、已下单或承诺到货时间；提交动作必须由店长在界面上确认。
- 用户要求绕过起订量、拆零、超上限等违规操作时，明确拒绝并引用制度条款。
- 制度类问题必须调用 search_policy_docs 检索，回答末尾标注引用来源；未检索时不得标注任何来源，禁止虚构文档名或章节名。
- search_policy_docs 返回 NO_HIT（知识库中无相关条款）时，必须直接回答「制度中未找到关于…的规定」，可以建议联系总部确认；禁止把不相关章节当依据，禁止编造条款编号、审批角色或时限。此时不标注任何引用来源，这是正确行为而非回答失败。"""

AGENT_PROMPT = DEFAULT_PROMPT + "\n\n" + AGENT_CONTRACT



def _migrate_prompt_contract(conn):
    """把库里生效模板的「同一轮并行」路径迁移为聚合工具路径（幂等）。

    为什么必须动库里的数据、而不只是改 db.py 的常量：
      生效提示词存在 prompt_templates 表里，管理员在后台改过模板后，
      改 AGENT_CONTRACT 常量不会影响已入库的那一份，
      模型会继续按「一轮并发 5 个工具」的旧指令执行——而真实模型根本做不到，
      结果就是每次补货都耗尽 5 轮预算。

    只替换「补货申请标准路径」整段，模板其余内容（角色设定、口径等）原样保留。
    """
    row = conn.execute(
        "SELECT id, content FROM prompt_templates WHERE is_active = 1 "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row or not row["content"]:
        return
    content = row["content"]
    if "get_sku_context" in content:
        return                                  # 已迁移过

    start = content.find("【补货申请标准路径")
    if start < 0:
        return
    # 段落终点：下一个【标题】或文本结尾
    nxt = content.find("\n【", start + 1)
    end = nxt if nxt > 0 else len(content)
    new_block = AGENT_CONTRACT[AGENT_CONTRACT.index("【补货申请标准路径"):
                               AGENT_CONTRACT.index("\n【硬性纪律】")].rstrip() + "\n"
    content = content[:start] + new_block + content[end:]

    # 旧模板里的「执行预算」首条要求一轮并发，真实模型做不到，一并改写
    content = content.replace(
        "- 你最多有 5 轮决策机会（每轮 = 一次模型调用）。同一轮可以并行发起多个互不依赖的只读工具调用，请合并调用，不要一次只查一项。",
        "- 你最多有 5 轮决策机会（每轮 = 一次模型调用）。实测你每轮只会发出一个工具调用，"
        "所以必须靠 get_sku_context 这类聚合工具省轮次，不要指望一轮内并发多个工具。")
    content = content.replace(
        "- 未经 get_sellable_inventory 核对库存，不得谈论任何建议量。",
        "- 未经 get_sku_context（或 get_sellable_inventory）核对库存，不得谈论任何建议量。")

    conn.execute("UPDATE prompt_templates SET content = ?, updated_at = ? WHERE id = ?",
                 (content, now(), row["id"]))


def get_db():
    """获取 SQLite 连接（行结果可按列名取值）。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _add_column(conn, table: str, column: str, decl: str):
    """安全加列：检查列是否存在，不存在才 ALTER（兼容旧 chat.db）。"""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db():
    """建表（幂等）+ 旧库迁移 + 种子数据。"""
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL,        -- store_manager / admin
            store_id      TEXT,                 -- 店长归属门店，admin 为空
            store_name    TEXT,
            status        TEXT DEFAULT 'active',
            created_at    TEXT
        );
        CREATE TABLE IF NOT EXISTS prompt_templates (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            content    TEXT NOT NULL,
            is_active  INTEGER DEFAULT 0,       -- 1 = 当前生效版本
            updated_by TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS kb_documents (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            filename    TEXT,
            doc_type    TEXT,                    -- 补货制度/配送日历/促销规则/审批流程等
            chunk_count INTEGER DEFAULT 0,
            uploaded_by TEXT,
            created_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS kb_chunks (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id    INTEGER NOT NULL,          -- 归属文档（删除文档级联删切片）
            section   TEXT,                      -- 所属章节标题（引用展示用）
            content   TEXT NOT NULL,             -- 切片原文
            embedding BLOB                       -- float32 向量二进制（重建索引免重算）
        );
        CREATE TABLE IF NOT EXISTS kb_meta (
            key   TEXT PRIMARY KEY,              -- 如 embed_provider_id
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id         TEXT PRIMARY KEY,
            user_id    INTEGER,                  -- 归属用户（行级权限）
            store_id   TEXT,
            title      TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            sources    TEXT,
            audit      TEXT,                     -- 异常标记 JSON（批次 4）
            tool_trace TEXT,                     -- 工具调用链 JSON（智能体阶段）
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_chunks_doc ON kb_chunks(doc_id);

        -- ========== 智能体阶段：业务主数据（原型用示例数据） ==========
        CREATE TABLE IF NOT EXISTS skus (
            sku           TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            spec          TEXT,                  -- 箱规描述（如「24 瓶/箱」）
            moq           INTEGER,               -- 最小起订量（件）
            box_size      INTEGER,               -- 整箱规格（每箱件数）
            alloc_cap     INTEGER,               -- 分配上限（件/周期）
            unit          TEXT DEFAULT '件',
            category      TEXT,
            alias         TEXT,                  -- 商品别名/俗称（店长口头叫法：矿泉水、泡面…）
            legacy_code   TEXT,                  -- 旧编码（未归并时不可直接算需求）
            code_merged   INTEGER DEFAULT 1      -- 1 = 新旧编码已归并
        );
        CREATE TABLE IF NOT EXISTS store_inventory (
            store_id     TEXT NOT NULL,
            sku          TEXT NOT NULL,
            sys_qty      INTEGER,                -- 系统库存
            sellable_qty INTEGER,                -- 可售库存（扣除锁定/残次）
            backroom_qty INTEGER,                -- 后仓数量
            shelf_qty    INTEGER,                -- 货架数量
            snapshot_date TEXT,                  -- 数据快照日期（T+1）
            PRIMARY KEY (store_id, sku)
        );
        CREATE TABLE IF NOT EXISTS sales_daily (
            store_id  TEXT NOT NULL,
            sku       TEXT NOT NULL,
            sale_date TEXT NOT NULL,
            qty       INTEGER,
            PRIMARY KEY (store_id, sku, sale_date)
        );
        -- 编码映射表：旧码/套装码 → 主编号（数据工程师归并后写入）
        -- 有映射即可按主编号算销量口径，不必等主表 code_merged 改完
        CREATE TABLE IF NOT EXISTS sku_code_map (
            alias_code TEXT PRIMARY KEY,          -- 旧编码 / 套装码
            main_code  TEXT NOT NULL,             -- 主编号（skus.sku）
            note       TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS promotions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            sku        TEXT NOT NULL,
            store_id   TEXT,                     -- NULL = 全部门店
            start_date TEXT,
            end_date   TEXT,
            version    TEXT,                     -- 促销版本号（需人工确认）
            kind       TEXT,                     -- 促销 / 变价
            note       TEXT
        );
        CREATE TABLE IF NOT EXISTS delivery_lines (
            store_id      TEXT PRIMARY KEY,
            delivery_days TEXT,                  -- 逗号分隔：MON,WED,FRI
            line_name     TEXT,
            capacity      INTEGER,               -- 线路容量（件）
            used          INTEGER                -- 已占用
        );
        -- 补货申请单草稿（智能体唯一写操作产物，必须人工确认后才提交）
        CREATE TABLE IF NOT EXISTS replenishment_drafts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    TEXT,
            store_id      TEXT,
            sku           TEXT,
            sku_name      TEXT,
            qty_suggested INTEGER,
            qty_requested INTEGER,
            delivery_date TEXT,
            basis         TEXT,                  -- 分配依据 JSON（库存/销量/条款/到货日/未满足）
            validation    TEXT,                  -- 四项校验结果 JSON
            status        TEXT DEFAULT 'draft',  -- draft / submitted / allocated / rejected
            created_by    TEXT,
            created_at    TEXT,
            submitted_at  TEXT
        );
        """)

        # ---- 旧库迁移：早期建表可能缺列 ----
        _add_column(conn, "sessions", "user_id", "INTEGER")
        _add_column(conn, "sessions", "store_id", "TEXT")
        _add_column(conn, "messages", "audit", "TEXT")
        _add_column(conn, "messages", "tool_trace", "TEXT")
        _add_column(conn, "skus", "alias", "TEXT")

        _seed(conn)
        _seed_business(conn)
        _patch_aliases(conn)
        _migrate_demo_promo(conn)
        _migrate_prompt_contract(conn)
        _patch_sku_code_map(conn)

    # 智能体执行轨迹表（agent_runs / agent_steps / agent_actions）
    # 放在函数内 import：agent.store 依赖本模块的 get_db，模块级 import 会循环引用
    from agent import store as agent_store
    agent_store.init_tables()


def _seed(conn):
    """种子数据：预置账号 + 默认提示词模板（幂等）。"""
    now = datetime.now().isoformat(timespec="seconds")

    # 预置账号（原型阶段；正式版应首次登录强制改密）
    if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        conn.executemany(
            "INSERT INTO users (username, password_hash, role, store_id, store_name, created_at) "
            "VALUES (?,?,?,?,?,?)",
            [
                ("admin",   hash_password("admin@123"),  "admin",         None, "总部", now),
                ("store01", hash_password("store@123"), "store_manager", "S001", "朝阳路店", now),
            ],
        )

    # 默认提示词模板（用户提供），设为生效
    if not conn.execute("SELECT 1 FROM prompt_templates LIMIT 1").fetchone():
        conn.execute(
            "INSERT INTO prompt_templates (name, content, is_active, updated_by, updated_at) "
            "VALUES (?,?,?,?,?)",
            ("默认模板", DEFAULT_PROMPT, 1, "system", now),
        )

    # 智能体工作流模板（制度助手约束 + 工具调用纪律），首次启动时写入并生效
    row = conn.execute(
        "SELECT id, content, updated_by FROM prompt_templates WHERE name = ?",
        ("智能体工作流模板",)).fetchone()
    if not row:
        conn.execute("UPDATE prompt_templates SET is_active = 0")
        conn.execute(
            "INSERT INTO prompt_templates (name, content, is_active, updated_by, updated_at) "
            "VALUES (?,?,?,?,?)",
            ("智能体工作流模板", AGENT_PROMPT, 1, "system", now),
        )
    elif row["updated_by"] == "system" and row["content"] != AGENT_PROMPT:
        # 系统维护的模板随版本升级自动同步（管理员改过的模板不动）
        conn.execute(
            "UPDATE prompt_templates SET content = ?, updated_at = ? WHERE id = ?",
            (AGENT_PROMPT, now, row["id"]),
        )


def _seed_business(conn):
    """业务主数据种子（原型演示用）：商品、库存、销量、促销、配送线路。

    特意设计了四种典型场景，用于验证停止条件：
      A001 正常              —— 库存一致、无促销、销量正常
      A002 促销期            —— 促销进行中，需先确认促销版本
      A003 零销量            —— 近 30 天销量为 0，触发「依据缺失」
      A004 账实不一致        —— 系统库存 60 / 可售 12，需人工核对后仓
      A005 新品无历史        —— 无销量记录，触发「依据缺失」
      A006 正常              —— 用于对照
    """
    if conn.execute("SELECT 1 FROM skus LIMIT 1").fetchone():
        return

    now_dt = datetime.now()
    today = now_dt.date()

    conn.executemany(
        "INSERT INTO skus (sku, name, spec, moq, box_size, alloc_cap, unit, category, "
        "legacy_code, code_merged) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("A001", "饮用天然水 550ml", "24 瓶/箱", 10, 24, 240, "瓶", "饮用水", None, 1),
            ("A002", "鲜牛奶 1L", "12 盒/箱", 5, 12, 120, "盒", "乳制品", "OLD-A002", 1),
            ("A003", "无糖气泡水 480ml", "15 瓶/箱", 8, 15, 150, "瓶", "饮料", None, 1),
            ("A004", "洗衣液 2kg", "6 瓶/箱", 4, 6, 60, "瓶", "日化", None, 1),
            ("A005", "红烧牛肉面 整箱", "30 包/箱", 5, 30, 200, "包", "方便食品", "OLD-A005", 1),
            ("A006", "抽纸 3 层", "20 包/箱", 6, 20, 160, "包", "纸品", None, 1),
        ],
    )

    # 库存快照（T+1）：A004 系统库存 60 但可售仅 12（后仓 48 未上架）
    conn.executemany(
        "INSERT INTO store_inventory (store_id, sku, sys_qty, sellable_qty, backroom_qty, "
        "shelf_qty, snapshot_date) VALUES (?,?,?,?,?,?,?)",
        [
            ("S001", "A001", 12, 12, 0, 12, str(today)),
            ("S001", "A002", 12, 12, 0, 12, str(today)),
            ("S001", "A003", 0, 0, 0, 0, str(today)),
            ("S001", "A004", 60, 12, 48, 12, str(today)),
            ("S001", "A005", 45, 45, 15, 30, str(today)),
            ("S001", "A006", 8, 8, 0, 8, str(today)),
        ],
    )

    # 近 30 天销量：A001/A002/A004/A006 有数据，A003 全 0，A005 无记录
    sales = []
    base = {"A001": 8, "A002": 5, "A003": 0, "A004": 2, "A006": 3}
    for i in range(30):
        d = today - timedelta(days=i)
        for sku, b in base.items():
            if sku == "A005":
                continue
            qty = 0 if b == 0 else max(1, b + (i % 4) - 1)
            sales.append(("S001", sku, str(d), qty))
    conn.executemany(
        "INSERT OR IGNORE INTO sales_daily (store_id, sku, sale_date, qty) VALUES (?,?,?,?)",
        sales,
    )

    # 促销：A003 当前处于促销窗口（4 天前开始，2 天后结束）
    # 注：演示数据里「促销门禁」示例商品从 A002（鲜牛奶）移到 A003（气泡水），
    #     让牛奶保持干净，用于演示「查库存 → 换算整箱 → 校验 → 建草稿」的主链路；
    #     促销版本确认、促销截断这两条停止条件仍由 A003 演示（它同时零销量，叠加两个停止条件）。
    conn.execute(
        "INSERT INTO promotions (sku, store_id, start_date, end_date, version, kind, note) "
        "VALUES (?,?,?,?,?,?,?)",
        ("A003", None, str(today - timedelta(days=4)), str(today + timedelta(days=2)),
         "P2026-11-SODA-v1", "促销", "第二件半价，历史销量不可直接作为需求依据"),
    )

    # 配送线路：周一/三/五发车，容量 500 件，已占用 320 件
    conn.execute(
        "INSERT INTO delivery_lines (store_id, delivery_days, line_name, capacity, used) "
        "VALUES (?,?,?,?,?)",
        ("S001", "MON,WED,FRI", "东区 1 号线", 500, 320),
    )


def _patch_aliases(conn):
    """补商品别名（旧库升级用；店长常用口头叫法，搜索时一并匹配）。"""
    aliases = {
        "A001": "矿泉水,饮用水,水,550",
        "A002": "鲜奶,牛奶,纯牛奶,1L",
        "A003": "气泡水,苏打水,无糖饮料",
        "A004": "洗衣液,洗涤用品",
        "A005": "方便面,泡面,牛肉面",
        "A006": "抽纸,纸巾,面巾纸",
    }
    for sku, alias in aliases.items():
        conn.execute(
            "UPDATE skus SET alias = ? WHERE sku = ? AND (alias IS NULL OR alias = '')",
            (alias, sku),
        )


def _patch_sku_code_map(conn):
    """预置新旧编码映射（REQ-1 的最小切片）。

    为什么要有这张表：
      访谈 f30 说新旧码与套装码并存，f33 要求「缺关键字段进人工修复队列，不用猜测值补齐」。
      「改主表 code_merged」要求先做完整个编码治理工程，试点跑不起来；
      改成「插一行映射」后，数据侧可以边跑边归并，命中映射的商品立刻可以正常算销量。
      没有映射的商品保持阻断（不猜），并生成一条转交单给数据工程师。
    """
    now = datetime.now().isoformat(timespec="seconds")
    rows = [
        ("OLD-A002", "A002", "鲜牛奶旧编码，已按主编号 A002 归并（f30）"),
        ("OLD-A005", "A005", "红烧牛肉面旧编码，已按主编号 A005 归并（f30）"),
    ]
    for alias, main, note in rows:
        conn.execute(
            "INSERT OR IGNORE INTO sku_code_map (alias_code, main_code, note, created_at) "
            "VALUES (?,?,?,?)", (alias, main, note, now))


def _migrate_demo_promo(conn):
    """旧库迁移：把演示用的「鲜牛奶促销」从 A002 移到 A003。

    背景：A002（鲜牛奶）是「申请补 20 箱」主链路的演示商品。
    如果它同时处于促销期，流程会在「确认促销版本」处停下，
    主链路就没法演示到「校验 → 建草稿 → 人工确认」。
    因此把促销示例挪到 A003（气泡水），停止条件照样保留。

    只迁移版本号为 P2026-11-MILK-v2 的那条种子数据，
    不会影响管理员自己录入的其他促销。
    """
    try:
        conn.execute(
            "UPDATE promotions SET sku = 'A003', version = 'P2026-11-SODA-v1' "
            "WHERE sku = 'A002' AND version = 'P2026-11-MILK-v2'"
        )
    except sqlite3.Error:
        pass    # 表结构异常时不阻断启动，促销门禁会按「无促销」处理


def get_active_prompt() -> str:
    """读取当前生效的系统提示词（每次问答实时读取，改完即生效）。"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT content FROM prompt_templates WHERE is_active = 1 "
            "ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    # 无生效模板时兜底用默认文案（不应发生，seed 已保证有）
    return row["content"] if row else DEFAULT_PROMPT


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
