# 1-问答助手归档

对应交付清单 **src 第 1 项「问答助手归档」**。

## 内容

| 文件 | 说明 |
|---|---|
| `replenishment_proto.py` | 阶段 1 最小原型：单脚本制度问答助手（无 Web）。链路：读取补货制度 Markdown → 500 字切片向量化存入 FAISS → 命令行提问 → BM25 + 向量混合检索 top3 → 拼入提示词 → 调用 LLM 生成回答（供应商可配置） |
| `补货制度.md` | 归档自带的种子知识库文档，脚本默认读取同目录下此文件 |

## 归档说明

- 此原型用于验证「制度问答」检索链路可行性，其能力已并入 `../2-MVP项目/` 的完整 Web 版（`kb.py` / `kbstore.py` 制度知识库 + `server.py` 流式问答），保留在此作为演进过程归档。
- 运行方式与依赖见 `replenishment_proto.py` 文件头注释（faiss-cpu / rank-bm25 / jieba / numpy / requests，LLM 配置走环境变量）。
