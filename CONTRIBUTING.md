# 开发与协作规范

面向后续维护者（也包括三周后的自己）。项目按「需求 → 设计 → 交付」三段归档，所有改动都应留下可追溯的痕迹。

## 一、分支与提交

- 主干为 `main`，始终保持可运行：推送前本地跑通 `python -m compileall` + 单测 + 离线评测。
- 一个改动一个提交，**不要**把「改功能 + 改文档 + 顺手重构」压成一次提交。
- 提交信息格式：`type(scope): 中文摘要`，正文用短横线列出改动点与原因。

```
fix(eval): 无向量模型时不误判失败

- CI 无 LLM_API_KEY，检索降级为纯 BM25，同义改写题会漏检
- 降级时如实记录命中率，但不计入失败
```

`type` 取值：`feat` / `fix` / `docs` / `test` / `refactor` / `chore`。
提交信息写「为什么」比写「改了什么」更有价值——半年后能看懂的是动机，不是 diff。

## 二、代码风格

- 行宽 110，4 空格缩进，UTF-8 + LF（见 `.editorconfig`）。
- **新增函数必须写类型标注**（现状：返回值标注 31%，新代码不再降）；docstring 讲清「为什么这么做」，不复述函数名。
- 不写裸 `except:`；捕获 `Exception` 时必须记录或转成业务错误码（现状 14 处宽泛捕获，均在适配外部输入/第三方库边界，新增不允许扩散）。
- 日志：原型阶段仍用 `print`（31 处），正式部署前统一改 `logging`，按 `INFO` 业务事件 / `WARNING` 可恢复异常 / `ERROR` 需人工介入分级。
- 动态 SQL 只允许拼接**白名单常量**（如 `retention.PURGE_TABLES`），任何来自请求的参数一律走 `?` 占位符。

```bash
pip install -r requirements-dev.txt
python -m ruff check .            # 仅确定性错误规则（E9/F63/F7/F82）
python -m mypy prototype/web      # 可选，不设门禁
```

## 三、新增业务工具的固定四步

工具是模型的唯一手脚，加一个工具要同步四处，少一处就会出现「模型能调、但拦不住」的漏洞：

1. `prototype/web/tools.py` 里实现函数，返回统一结构 `{ok, blocking, message, data}`；阻塞类错误必须带 `code` 与 `blocking_reason`；
2. 在 `TOOLS_SPEC` 注册 JSON Schema，同时在 `TOOL_RISK` 声明风险等级（`read` / `verify` / `write`）；
3. 只读工具补进 `tests/test_framework_basics.py` 的风险分区断言——**写操作全系统只能有 `create_replenishment_draft` 一个**，新增写工具等于改动防线，需要先讨论；
4. `eval/cases_v1.json` 至少加一条用例，覆盖「该调用」与「不该调用」两个方向。

## 四、提交前自检

```bash
python -m compileall -q prototype eval tests scripts   # 语法
python -m pytest tests/ -v                             # L1 单测
python eval/run_eval.py                                # L2 离线评测（不调 API）
```

CI（`.github/workflows/ci.yml`）会跑同样三步，绿了再合。三条命令都不需要 API 密钥，clone 即可复现。

## 五、安全基线（不可协商）

- `.env`、`*.db`、`uploads/` 已在 `.gitignore`，**密钥与真实会话数据绝不入库**；日志与截图里的密钥必须脱敏（`config.mask()`）。
- 任何写操作都必须走「`validate_replenishment` 签发一次性令牌 → 建单校验参数一致 → HITL 中断等店长确认」，不允许为演示方便开后门。
- 模型输出的数量一律以框架层正则提取结果为准，不接受模型自行改写店长明确要求的数量。

## 六、文档同步

改代码时顺手确认这三处是否还成立，否则下一位读者会被误导：

- `docs/README.md` 的 REQ → 代码追溯表；
- `docs/delivery/验证与测试报告.md` 的验证结论（有新验证就补，未验证就写清未覆盖）；
- `CHANGELOG.md` 的对应版本条目。
