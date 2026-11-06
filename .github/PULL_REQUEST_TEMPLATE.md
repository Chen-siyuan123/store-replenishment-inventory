## 这个 PR 做了什么

<!-- 一句话说明动机；如果修的是缺陷，写清触发条件 -->

## 影响范围

- [ ] 仅文档 / 注释
- [ ] 业务工具（`prototype/web/tools.py`）
- [ ] Agent 执行循环 / 门禁（`agent/harness.py`、`tools.py` 写操作链路）
- [ ] 检索（`kb.py` / `kbstore.py`）
- [ ] 前端 / 管理后台（`templates/`）
- [ ] 测试 / 评测（`tests/`、`eval/`）

## 是否触碰安全边界

- [ ] 新增了写操作工具（**默认不允许**，必须走 `validate_replenishment` + 一次性令牌 + HITL）
- [ ] 改了提示词或工具 Schema（需要在 PR 里贴出评测前后对比）
- [ ] 涉及密钥 / 会话数据（确认未入库、日志已脱敏）
- [ ] 以上都不涉及

## 验证方式

```bash
python -m compileall -q prototype eval tests scripts
python -m pytest tests/ -v
python eval/run_eval.py
```

- [ ] 三条命令本地已通过
- [ ] CI 已绿
- [ ] 有新增用例（改工具必填）

## 文档同步

- [ ] `docs/README.md` 的 REQ → 代码追溯表已更新
- [ ] `docs/验证与测试报告.md` 的结论已更新（或说明了未覆盖）
- [ ] `CHANGELOG.md` 已补条目
