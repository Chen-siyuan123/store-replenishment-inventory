---
name: 缺陷报告
about: 记录一次可复现的异常（问答答错、门禁未拦住、页面报错等）
title: "[bug] "
labels: bug
---

## 现象

<!-- 实际发生了什么，一句话 -->

## 复现步骤

1.
2.
3.

## 期望结果

<!-- 按补货制度 / 设计文档，应该是什么 -->

## 是否涉及写操作

- [ ] 是（涉及补货草稿创建 / 确认，请附草稿 id 与执行轨迹 run_id）
- [ ] 否

## 执行轨迹

```
# 管理后台 /admin/trace 可查；或用脚本导出成 Markdown
python scripts/export_run_trace.py --run-id <run_id>
```

## 环境

- 运行方式：`run_local.bat` / 手动 `uvicorn`
- Python 版本：
- 模型与地址（`LLM_MODEL` / `LLM_BASE_URL`，**密钥请脱敏**）：
- 提交版本（`git rev-parse --short HEAD`）：
