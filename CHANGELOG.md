# 更新日志

本文件记录仓库的关键变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号按原型里程碑递增（尚未发布正式版本，统一用 `0.x`）。

## [0.3.0] - 2026-11-05

### 新增

- **离线评测一键入口** `eval/run_eval.py`：用例资产盘点（30 条用例的期望工具是否都存在）、
  制度检索门禁（正常题 top1 命中、陷阱题必须判空）、三条 `ScriptedLLM` 离线轨迹回归；
  运行后自动生成 `eval/eval_report.md`。
- **框架层单元测试** `tests/test_framework_basics.py`：13 项断言，覆盖数量抽取防线
  （中文数字、整箱换算、建议量不得覆盖店长明确数量）、工具风险分区一致性
  （全系统写操作唯一、HITL 集合等于写集合、`TOOLS_SPEC` 与 `TOOL_RISK` 一一对应）。
- **CI** `.github/workflows/ci.yml`：push / PR 自动跑语法检查、单元测试、离线评测，
  评测报告作为 artifact 保留 30 天；同一分支连续推送自动取消上一轮。
- **验证文档** `docs/delivery/验证与测试报告.md`：四层验证体系（单元测试 / 离线评测 /
  端到端用例 / 上线检查），并显式列出**未覆盖的边界**（端到端回答质量待试点人工评审、
  7 项上线阻塞未清、行级安全未上库）。
- **运行实录与导出脚本**：`scripts/export_run_trace.py` 把 `chat.db` 轨迹三表导出成 Markdown，
  `docs/delivery/运行实录-20箱牛奶案例.md` 为一次真实 5 轮执行的原样记录
  （20 箱被校验拦截 → 压到 10 箱 → 建草稿 → 店长确认）。
- **工程规范文件**：`LICENSE`（MIT）、`.editorconfig`、`.gitattributes`、`pyproject.toml`
  （pytest 与 ruff 口径，ruff 暂不作为门禁）。
- **协作规范**：`CONTRIBUTING.md`（提交格式、代码风格、新增业务工具的固定四步、安全基线、
  文档同步清单）、`requirements-dev.txt`、PR / 缺陷报告模板；`pyproject.toml` 补 `[project]`
  元数据，版本号与 git tag `v0.3.0` 对齐。

### 修复

- 评测脚本在**无向量模型**环境下不再误判失败：`LLM_API_KEY` 缺失时检索降级为纯 BM25，
  同义改写题会漏检，现在标记为 `degraded`，如实记录命中率但不计入失败。
- 评测脚本在**全新 clone**（数据库为空）时不再抛「知识库为空」：预检前自动导入种子制度文档。

### 变更

- README 增加验证与测试小节、CI 徽章、项目结构更新；文档索引同步登记验证报告。

## [0.2.0] - 2026-11-05

### 新增

- **写操作防线**：`validate_replenishment` 签发一次性令牌 → `create_replenishment_draft`
  校验参数一致 → 执行前 HITL 中断等店长确认；数量由框架层正则独立提取，提示词注入无法覆盖。
- **业务停止条件与转交单**：六类阻塞情况（账实不一致、零销量、新旧编码未归并等）工具返回
  `blocking=true`，自动开转交单（原因 / 接手岗位 / 处理时限），后台可查待办与超时。
- **检索两级门禁**：BM25（jieba）+ FAISS 向量，相关度不达标直接回「制度中未找到」。
- **管理后台**：知识库、提示词、执行轨迹、审计与转交待办四个页面（需 admin）。
- **日志留存**：`retention.py` 到期先导出 `archive/*.jsonl` 再删除，申请单与未闭环转交单不清理。

## [0.1.0] - 2026-11-05

### 新增

- Web 原型骨架：FastAPI + SSE 流式问答，自研轻量执行循环（最多 5 轮工具调用）。
- 11 个业务工具（库存、销量、配送日历、制度检索、校验、建单等）与工具风险分级。
- SQLite 存储：会话、消息、`agent_runs / agent_steps / agent_actions` 轨迹三表。
- JWT 登录态 + PBKDF2 密码哈希；`.env` 配置加载与中文自检。
- 阶段 1 原型 `prototype/stage1/replenishment_proto.py`（单脚本 RAG，验证检索可行性，已被 `prototype/web` 取代）。
