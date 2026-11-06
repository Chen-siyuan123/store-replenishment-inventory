# 门店补货与库存协同智能体 · 本地运行指南

面向第一次在这台机器上跑起来的人。三步即可。

---

## 一、三步启动

```bat
cd src/web

copy .env.example .env      :: 1. 复制配置模板，用记事本打开填 LLM_API_KEY
python config.py            :: 2. 配置体检（不启动服务，先确认配置对不对）
run_local.bat               :: 3. 双击启动（自动查依赖、起服务、开浏览器）
```

`run_local.bat` 会依次做：检查 Python → 检查 `.env` → 装缺失依赖 → 跑配置自检 → 启动 uvicorn → 6 秒后打开 <http://127.0.0.1:8765>。

默认账号：

| 角色 | 账号 | 密码 | 能做什么 |
|---|---|---|---|
| 总部管理员 | `admin` | `admin@123` | 知识库、提示词、对话审计、执行轨迹 |
| 店长 | `store01` | `store@123` | 问答、补货申请、草稿确认 |

---

## 二、配置项

全部放在 `src/web/.env`，由 `config.py` 在进程启动时加载（**已存在的系统环境变量优先**，便于服务器覆盖而不改文件）。

| 变量 | 必填 | 示例 | 说明 |
|---|---|---|---|
| `LLM_BASE_URL` | ✅ | `https://open.bigmodel.cn/api/paas/v4` | OpenAI 兼容地址，换供应商只改这里 |
| `LLM_API_KEY` | ✅ | `xxxx.yyyy` | 智谱为 `<id>.<secret>` 格式 |
| `LLM_MODEL` | ✅ | `glm-4-flash` | 对话模型，需支持 Function Calling |
| `LLM_EMBED_MODEL` | 建议 | `embedding-3` | 向量模型。**不能填对话模型名** |
| `KB_VEC_MIN_INFO` | 可选 | `0.30` | 语义路可信下限，低于它退化为纯 BM25 |
| `KB_VEC_MIN_HIT` | 可选 | `0.55` | 语义中等命中下限（需配合关键词密度） |
| `KB_VEC_STRONG_HIT` | 可选 | `0.68` | 语义强命中下限（可单独放行） |
| `KB_BM25_MIN_DENSITY` | 可选 | `0.20` | 每词平均 BM25 得分下限 |

> 检索门限是**按当前语料 + embedding-3 实测标定**的。换 embedding 供应商或大改制度文档后，务必重跑 `python ../../eval/probe_retrieval.py` 重新标定，否则会出现「该答的说没找到」或「没答案的硬编」。

---

## 三、常见错误对照

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动即打印 `[ERR] 缺少必填配置` | `.env` 没填全 | 按提示补 `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL` |
| `向量化请求被拒绝（模型 xxx）` / HTTP 400 / 错误码 1213 | 把对话模型名填进了 `LLM_EMBED_MODEL` | 改为 `embedding-3` |
| `[向量化][WARN] ... 已降级为哈希伪向量` | 没配 `LLM_EMBED_MODEL` | 补上并重启；此时只有关键词检索生效 |
| `向量化鉴权失败（401）` | key 无效/过期 | 控制台重置后更新 `.env` |
| `向量化被限流（429）` | 免费额度频率限制 | 已内置 1 次重试；持续出现则降低并发 |
| `[知识库] 向量来源已变更：hash-bag:2048 → api:embedding-3:2048` | 首次接真实 embedding，旧库是伪向量 | **正常**，说明已自动整库重算 |
| `[知识库][WARN] 向量重算失败，语义检索已禁用` | 换模型时接口不可用 | 修好配置后重启；期间仅关键词检索 |
| 页面空白、控制台 404 | 端口被占用，服务没起来 | 换端口：`python -m uvicorn server:app --port 8799` |
| 对话回复「制度中未找到」但制度里明明有 | 检索门限偏严，或术语与制度不一致 | 先跑 `probe_retrieval.py` 看分数，再调 `KB_VEC_MIN_HIT` |

---

## 四、目录速览

```
src/web/
├── .env / .env.example     配置（.env 已被 .gitignore 忽略，不会提交）
├── config.py               配置加载 + 自检（可单独运行：python config.py）
├── run_local.bat           Windows 一键启动
├── server.py               FastAPI：问答 SSE、确认接口、后台页面
├── kb.py                   【核心模块①②】切片 / 向量化 / 分词 / 混合检索
├── kbstore.py              知识库数据库化 + 检索双门禁 + 向量来源校验
├── llm.py                  LLM 封装（流式 / Function Calling，供应商可换）
├── tools.py                10 个工具 + 状态机 + 校验令牌 + 数量意图解析
├── db.py                   建表、种子数据、提示词模板与迁移
├── agent/
│   ├── harness.py          执行循环（最多 5 轮）+ 写操作中断/恢复
│   ├── store.py            轨迹三表 + 质量指标
│   └── scenarios.py        离线回放（不消耗额度）
├── index.html              店长问答页
└── templates/              登录 / 知识库 / 提示词 / 审计 / 轨迹
```

---

## 五、验证脚本

```bash
python config.py                        # 配置体检
python -m agent.scenarios               # 三条轨迹回归（离线，不烧额度）
python ../../eval/probe_retrieval.py    # 制度类检索预检（10 条，含 3 条陷阱题）
```

三个脚本都是只读或本地写库，不依赖浏览器，适合改完代码后快速自检。
