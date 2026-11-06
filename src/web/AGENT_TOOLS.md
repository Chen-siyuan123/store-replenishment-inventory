# 智能体工具层规范（Function Calling）

> 适用版本：prototype/web 智能体阶段
> 核心设计原则：**门禁做在代码里，不靠模型自觉。** 提示词只是第一道防线，硬约束全部由工具层实现。

---

## 一、四层防御

| 层 | 实现位置 | 作用 |
|---|---|---|
| ① 提示词工作流契约 | `db.AGENT_CONTRACT`（存库，管理页可编辑） | 告诉模型该按什么顺序做、什么情况停 |
| ② 会话状态机 | `tools._steps()` | 记录每个 SKU 已完成哪些核对步骤，跳步即拒 |
| ③ 校验令牌 | `validate_replenishment` 签发 → `create_replenishment_draft` 校验 | 没有通过四项校验就拿不到令牌，拿不到令牌就建不了单 |
| ④ 人工确认 | `POST /api/drafts/{id}/submit` | 模型只能建草稿，提交必须由店长在界面点击 |

令牌规格：32 位随机串，**15 分钟有效、一次性使用、绑定门店 + SKU + 数量 + 配送日**，任何一项对不上即 `TOKEN_MISMATCH`。

---

## 二、工具清单（10 个）

### 只读工具（8 个，无需确认）

| 工具 | 关键参数 | 返回要点 | 停止条件 |
|---|---|---|---|
| `search_sku` | keyword | 匹配的商品列表（编码/名称/规格） | 无匹配 → 失败 |
| `get_sku_context` | sku, delivery_date? | **聚合**：库存 + 促销 + 销量 + 制度参数 + 配送日历一次返回，附库存状态与「店长指定数量」换算 | 任一子查询 blocking → 原样上抛 |
| `get_sellable_inventory` | sku, human_confirmed | 系统库存、可售库存、后仓、货架、快照日 | **账实不一致 → blocking**，需人工确认后带 `human_confirmed=true` 重调 |
| `check_promotion_status` | sku, confirm_version | 促销窗口、版本号、历史销量可用性 | **促销样本 <3 天 → blocking（促销截断）**；**版本未确认 → blocking** |
| `get_sales_context` | sku, days | 近 7/30 天销量、日均 | **零销量 / 无记录 / 编码未归并 → blocking（依据缺失）** |
| `get_replenishment_policy` | sku | MOQ、整箱规格、分配上限 + 条款引用 | — |
| `get_delivery_schedule` | delivery_date? | 配送日、线路余量、下一可发日 | 指定日期不可发时提示最近可发日 |
| `search_policy_docs` | query, top_k | 制度条款原文（混合检索） | 知识库为空 → 失败 |

### `get_sku_context` 为什么必须存在

它是**为真实模型加的**，不是设计洁癖。GLM-4-Flash 实测每轮只发一个 `tool_call`，即使提示词明确要求「同一轮并行调用多个只读工具」。于是「库存 / 促销 / 销量 / 制度参数 / 配送日历」这五项要烧掉 5 轮，再加四项校验和建草稿必然撞上 5 轮上限——**补货链在真实对话里根本走不完**（首次实测状态为 `rounds_exhausted`，只查完促销就被截断）。

业务上这五项本来就该同时看（缺任一项都不能算量），合并成一个工具后：

| | 细粒度（原） | 聚合（现） |
|---|---|---|
| 拿全上下文所需轮次 | 5 | **1** |
| 补货全链路总轮次 | 6+（超预算） | **4**（留 1 轮余量） |
| 漏查风险 | 模型可能只查其中两三项 | 由工具内部保证全查 |

细粒度工具全部保留，用于「只问某一项」的追问（如「这个品促销到几号」）。状态机也相应接受两种路径：走聚合工具即视为库存/促销/销量三项均已核对。

### 确定性计算工具（1 个，只读）

`**suggest_quantity**`（sku, coverage_days=3）

- 前置：库存核对 + 促销确认 + 销量核对三项都已完成
- 公式：`ceil_to_box(日均销量 × 覆盖天数 − 可售库存)`，再夹在 `[MOQ, 分配上限]` 之间
- **LLM 不参与任何算术**，避免"模型心算出 10 瓶"这类事故
- 建议量为 0 时明确返回"库存充足，无需补货"，模型不得建单

### 写操作工具（1 个，强确认）

`**create_replenishment_draft**`（sku, qty, delivery_date, validation_token, stock_verified_by_human, promotion_version_confirmed）

四道门禁，缺一拒绝：

| 门禁 | 拒绝码 | 含义 |
|---|---|---|
| 令牌存在 / 未使用 / 未过期 | `TOKEN_INVALID` `TOKEN_USED` `TOKEN_EXPIRED` | 必须先通过四项校验 |
| 令牌内容与申请一致 | `TOKEN_MISMATCH` | 商品/数量/配送日被改动 |
| 账实一致性已人工确认 | `STOCK_NOT_VERIFIED` | 柜面差异未核对不许建单 |
| 促销版本已确认 | `PROMO_VERSION_REQUIRED` | 参数或会话内已确认版本均被接受 |

---

## 三、调用顺序约束（状态机）

```
search_sku
  → get_sellable_inventory   （账实不一致 → 停下等人工确认）
  → check_promotion_status   （促销未确认版本 / 样本不足 → 停下）
  → get_sales_context        （零销量 / 无依据 → 停下）
  → get_replenishment_policy ＋ get_delivery_schedule
  → suggest_quantity         （前置未齐 → STEP_VIOLATION + next_action）
  → validate_replenishment   （四项校验，通过才发令牌）
  → create_replenishment_draft（令牌 + 人工确认 + 促销版本）
  → 店长在界面点「确认提交」→ 状态 draft → submitted
```

代码级约束：
- `validate_replenishment` 检查 `inventory / promo / sales` 三步，缺一则返回 `STEP_VIOLATION`，并附 `next_action`（下一个该调的工具 + 参数）
- `suggest_quantity` 使用同一套前置检查（促销必须核对，因为促销会改变需求口径）
- `create_replenishment_draft` 只认令牌，不认"模型说已经校验过了"

---

## 四、系统提示词约束要点

`db.AGENT_CONTRACT` 中的关键条款（管理员可在后台修改，服务层会自动补齐缺失的契约段落）：

1. 六步必须逐步真实调用，禁止"已知大概数就省略"；
2. 建议量必须来自 `suggest_quantity`，禁止自己心算；
3. `blocking=true` = 业务停止条件，必须停下说明原因，禁止换个说法重试；
4. `STEP_VIOLATION` = 可纠正错误，按 `next_action` 补齐后重新校验；
5. 未核对库存不得谈数量；未确认促销版本不得引用历史销量；
6. 只能建草稿，不得声称已提交或承诺到货时间；
7. 用户要求绕过制度（拆零、低于起订、超上限）→ 明确拒绝并引用条款。

---

## 五、审计扩展（R4）

`messages.tool_trace` 保存每一步工具调用（工具名、参数、是否阻断、返回摘要、时间）。
新增标记规则 **R4 流程违规尝试**：出现 `TOKEN_INVALID / TOKEN_MISMATCH / TOKEN_EXPIRED / TOKEN_USED / STOCK_NOT_VERIFIED`，
说明模型试图跳过校验或未经人工确认强行建单，已被代码门禁拒绝——这类记录在审计页可直接筛选复核。

> 注意：`STEP_VIOLATION` 不计入 R4，它是"补齐前置步骤"的正常交互，不是越权。
>
> 轨迹明细另见 `agent_steps` 表（每轮决策 + 每次工具调用），审计页之外还有「执行轨迹」页做质量分析。

---

## 六、测试矩阵（已验证）

| 场景 | 商品 | 预期 | 实测 |
|---|---|---|---|
| 正常补货 | A001 矿泉水（库存 12，日均 8.3） | 建议 24 瓶 → 校验通过 → 草稿 #n | ✅ |
| 促销期 | A003 无糖气泡水（版本 P2026-11-SODA-v1） | 停在版本确认 / 促销截断，不产生建议量 | ✅ |
| 账实不一致 | A004 洗衣液（系统 60 / 可售 12） | 停在人工核对，不产生建议量 | ✅ |
| 零销量 | A003 气泡水 | 停在"依据缺失"，不建单 | ✅ |
| 跳过校验直接建单 | 任意 | `TOKEN_INVALID`，审计打 R4 | ✅ |
| 非整箱数量 | A001 30 瓶 | 校验不通过，提示调整为 48 | ✅ |
| 非配送日 | 周日 | 校验不通过，提示配送日历 | ✅ |
| 令牌复用 / 篡改数量 | 任意 | `TOKEN_USED` / `TOKEN_MISMATCH` | ✅ |
| 制度问答 | 「整箱规格是什么？」 | 走 `search_policy_docs`，附引用 | ✅ |

---

## 七、执行框架（Harness）与写操作中断

工具层不负责"什么时候调用"，那是执行框架的事，见 `AGENT_HARNESS.md`。

| 机制 | 位置 | 说明 |
|---|---|---|
| 工具风险分级 | `tools.TOOL_RISK` | `read` 只读 / `verify` 签发令牌 / `write` 写操作 |
| 5 轮上限 | `agent/harness.py: MAX_ROUNDS` | 轮次耗尽强制收尾，不再让模型自我纠错 |
| 执行前中断 | `tools.HITL_TOOLS` | 写操作先干跑 `preview_replenishment_draft`，推给店长确认后才执行 |
| 轨迹日志 | `agent_steps` | 轮次 / 工具 / 参数 / 耗时 / 结果摘要 / 是否阻断 |

`preview_replenishment_draft` 是**框架专用的内部函数**，不在 `TOOLS_SPEC` 里，
模型无法直接调用——否则模型可以绕过中断直接"预览 + 自建"。

两道人工确认的分工：

```
create_replenishment_draft 执行前  → 确认「要不要生成草稿」   （写入门禁）
POST /api/drafts/{id}/submit      → 确认「要不要提交审批」   （流程门禁）
```

## 八、知识库检索的双门禁（2026-11-05 修复）

`kbstore.search` 原来是「归一化后 0.4/0.6 融合 + 永远返回 top_k 条」，有两个后果：
排序会被向量噪声顶掉、陷阱题也能拿到高分片段去编答案。现在加了两道门：

| 门 | 位置 | 规则 |
|---|---|---|
| 语义路可信判定 | `VEC_MIN_INFO=0.30` | 向量最高余弦低于此值（如离线哈希伪向量只有 0.0x）→ 语义路不参与融合，退化为纯 BM25 |
| 无答案判定 | `VEC_MIN_HIT=0.45` / `BM25_MIN_DENSITY=0.20` | 语义路可信时看余弦，不可信时看「BM25 最高分 ÷ 查询词数」，不达标返回空列表 |
| 词形归一 | `MIN_EXPAND_JACCARD=0.60` | 只为语料中不存在的查询词找字面最接近的词（`订量→起订量`），避免分词粒度导致漏召回 |

返回空列表时 `search_policy_docs` 回 `NO_HIT`（`blocking=False`，可恢复），
提示模型直接回答「制度中未找到」；同时 `ctx["last_hits"]=[]`，使审计 R2/R3 不再误报。
三个阈值都可用环境变量覆盖（`KB_VEC_MIN_INFO` / `KB_VEC_MIN_HIT` / `KB_BM25_MIN_DENSITY`）。

> 已知残留：`单店`/`单门店` 这类「用词不同但语义相同」的问法在纯词法模式下仍会漏召回（实测 30 条评测用例的 #3）。配好真实 embedding 接口后走语义路即可命中，不需要同义词表。

## 九、已知边界（原型阶段）

1. 会话状态与令牌存在**进程内存**，重启即失效；正式版应落 Redis（多实例部署必须）。
2. 业务数据是示例种子（6 个 SKU、1 家门店），真实场景应接 ERP/POS 同步。
3. 促销期需求折算目前只做"标记 + 人工确认"，未实现按促销系数折算。
4. 审计 R4 是启发式，仍需人工复核判定。
