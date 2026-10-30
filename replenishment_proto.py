#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
门店补货与库存协同智能体系统 · 阶段 1 最小原型（单脚本，无 Web）
====================================================================
功能链路（与架构方案第 2.1 节「路径 A：制度问答」对应）：

    读取补货制度 Markdown
        ↓ 【核心模块①】500 字切片 → 向量化 → 存入 FAISS
    命令行输入问题
        ↓ 【核心模块②】BM25 + 向量混合检索 top3
        ↓ 拼入提示词
        ↓ 【核心模块③】调用 LLM 生成回答（供应商可配置）

运行前安装依赖：
    pip install faiss-cpu rank-bm25 jieba numpy requests

配置 LLM（环境变量，均可选）：
    LLM_BASE_URL   OpenAI 兼容接口地址，如 https://api.openai.com/v1
    LLM_API_KEY    对应供应商的 API Key
    LLM_MODEL      模型名，如 gpt-4o-mini / qwen-plus / deepseek-chat
    未配置时自动进入「仅检索模式」：只输出命中的制度条款，不调用 LLM，
    便于在无 Key 环境下先验证检索链路。

后续阶段复用说明（三处核心模块在代码中用【核心模块N】显式标注）：
    ① 切片向量化  → 阶段 2 迁入 app/rag/（splitter + embedder + retriever）
    ② 混合检索    → 阶段 2 迁入 app/tools/kb_retriever.py
    ③ LLM 封装    → 阶段 2 迁入 app/llm/gateway.py + providers/
"""

import os
import re
import sys
import math
import hashlib

import numpy as np
import requests
import jieba
import faiss
from rank_bm25 import BM25Okapi

# ============================================================
# 全局配置（阶段 2 迁移时改为 app/core/config.py 统一管理）
# ============================================================

KB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "补货制度.md")

# 切片参数：500 字一片，相邻片重叠 50 字（重叠防止条款被切断后检索丢失上下文）
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50

# 混合检索权重：向量相似度 0.6 + BM25 关键词 0.4
W_VECTOR = 0.6
W_BM25 = 0.4
TOP_K = 3

# 向量维度：真实 embedding 与演示用哈希向量均归一化到该维度
# （2048 与智谱 embedding-3 输出维度一致；换其他供应商若维度不同，
#   embed_texts 会自动截断/补齐到该维度）
EMBED_DIM = 2048

# 演示模式提示只打印一次（避免每次查询重复输出）
_DEMO_MODE_NOTIFIED = False


# ============================================================
# 【核心模块③】LLM 调用封装（供应商可配置）
# ------------------------------------------------------------
# 设计要点（对应架构方案 2.2 与 app/llm/ 目录）：
#   1. 统一走 OpenAI 兼容协议（/chat/completions），换供应商只改
#      LLM_BASE_URL / LLM_MODEL / LLM_API_KEY 三个环境变量，代码不动；
#   2. 阶段 2 迁移时本函数拆为 gateway.py（重试/超时/降级）+ providers/
#      （每供应商一个 adapter 文件）；
#   3. 未配置 Key 时返回 None，由上层决定降级为「仅检索模式」。
# ============================================================

def call_llm(prompt: str, system_prompt: str, timeout: int = 60):
    """调用 LLM 生成回答。

    参数:
        prompt:        拼好检索上下文的用户提示词
        system_prompt: 系统提示词（约束 LLM 只依据制度条款回答）
        timeout:       请求超时秒数
    返回:
        str  -> 成功返回模型回答文本
        None -> 未配置供应商 / 调用失败（上层降级为仅检索模式）
    """
    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()

    # 三要素缺一不可，否则视为「未配置 LLM」
    if not (base_url and api_key and model):
        return None

    try:
        # OpenAI 兼容协议：几乎所有主流供应商（通义/DeepSeek/月之暗面/OpenAI）
        # 都提供该协议，因此这里是唯一需要写的 HTTP 形态
        resp = requests.post(
            url=f"{base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,  # 制度问答要求低随机性
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:  # 网络/鉴权/限流等异常统一降级，不让程序崩溃
        print(f"[LLM 调用失败，已降级为仅检索模式] 原因: {e}", file=sys.stderr)
        return None


# ============================================================
# 【核心模块①】切片向量化（Markdown → 500 字切片 → 向量 → FAISS）
# ------------------------------------------------------------
# 设计要点：
#   1. 切片前先剥离 Markdown 标记、按条款断行拼接，保证语义连续；
#   2. 真实环境用 API embedding；未配置时用「词袋哈希」演示向量——
#      零依赖可跑通链路，但语义能力弱，仅用于结构验证；
#   3. FAISS 用 IndexFlatIP（内积）+ 归一化向量 = 余弦相似度；
#   4. 阶段 2 迁移时拆为 app/rag/splitter.py + embedder.py，
#      FAISS 换 pgvector（架构方案数据层既定选型）。
# ============================================================

def load_and_clean_markdown(path: str) -> str:
    """读取 Markdown 并做轻量清洗：去标记符号、压缩空白，保留条款文字。"""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)  # 去标题井号
    text = re.sub(r"[*_`>]+", "", text)                 # 去强调/引用符号
    text = re.sub(r"\n{2,}", "\n", text).strip()        # 压缩连续空行
    return text


def split_chunks(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP):
    """按 500 字滑动窗口切片，相邻片重叠 50 字。

    重叠的目的：某条制度条款恰好横跨两个切片边界时，
    至少有一个切片能保留完整上下文，避免检索命中「半句话」。
    """
    chunks = []
    step = size - overlap  # 滑动步长
    for i in range(0, len(text), step):
        piece = text[i:i + size].strip()
        if len(piece) > 20:  # 过滤过短的尾部碎片
            chunks.append(piece)
        if i + size >= len(text):  # 已覆盖全文则停止
            break
    return chunks


def embed_texts(texts):
    """批量向量化。

    优先走 OpenAI 兼容 /embeddings 接口（配置了 LLM 三要素即启用）；
    否则降级为「词袋哈希」演示向量：把分词后的每个词哈希到固定维度，
    归一化后作为伪语义向量——能跑通 FAISS 链路，不具备真实语义能力。
    """
    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    model = os.environ.get("LLM_EMBED_MODEL", os.environ.get("LLM_MODEL", "")).strip()

    if base_url and api_key and model:
        # ---- 真实路径：供应商 embedding 接口 ----
        resp = requests.post(
            url=f"{base_url.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "input": texts},
            timeout=60,
        )
        resp.raise_for_status()
        vecs = np.array([d["embedding"] for d in resp.json()["data"]],
                        dtype="float32")
        # 供应商返回维度可能 ≠ EMBED_DIM，投影/截断到统一维度
        if vecs.shape[1] != EMBED_DIM:
            if vecs.shape[1] > EMBED_DIM:
                vecs = vecs[:, :EMBED_DIM]
            else:
                pad = np.zeros((vecs.shape[0], EMBED_DIM - vecs.shape[1]),
                               dtype="float32")
                vecs = np.hstack([vecs, pad])
        return _normalize(vecs)

    # ---- 演示路径：词袋哈希伪向量（无 Key 环境验证链路用） ----
    global _DEMO_MODE_NOTIFIED
    if not _DEMO_MODE_NOTIFIED:
        print("[提示] 未配置 LLM 环境变量，embedding 使用演示用哈希向量（非真实语义）",
              file=sys.stderr)
        _DEMO_MODE_NOTIFIED = True
    return _normalize(np.array([_hash_bag_vector(t) for t in texts],
                               dtype="float32"))


def _hash_bag_vector(text: str) -> np.ndarray:
    """演示用词袋哈希向量：词 → 哈希落桶 → 频次累加。"""
    vec = np.zeros(EMBED_DIM, dtype="float32")
    for token in jieba.lcut(text):
        if len(token) < 2:  # 过滤单字符噪声
            continue
        bucket = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % EMBED_DIM
        vec[bucket] += 1.0
    return vec


def _normalize(vecs: np.ndarray) -> np.ndarray:
    """L2 归一化：归一化后内积即余弦相似度。"""
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0  # 防止全零向量除零
    return vecs / norms


def build_vector_index(chunks):
    """【核心模块①】入口：切片 → 向量化 → 建 FAISS 索引。

    返回 (faiss_index, chunk_list, chunk_vectors)，检索阶段复用。
    """
    print(f"[知识库] 切片完成：共 {len(chunks)} 片（每片 ≤{CHUNK_SIZE} 字，重叠 {CHUNK_OVERLAP} 字）")
    vectors = embed_texts(chunks)
    index = faiss.IndexFlatIP(EMBED_DIM)  # 内积索引（配合归一化 = 余弦）
    index.add(vectors)
    print(f"[知识库] FAISS 索引建立完成：dim={EMBED_DIM}, 共 {index.ntotal} 条")
    return index, chunks, vectors


# ============================================================
# 【核心模块②】混合检索（BM25 关键词 + 向量语义，加权融合取 top3）
# ------------------------------------------------------------
# 设计要点：
#   1. BM25 擅长精确术语命中（如「MOQ」「整箱」），向量擅长同义改写
#      （如「一箱多少个」命中「整箱规格」），两者互补；
#   2. 两路得分各自做 min-max 归一化到 [0,1] 再加权，消除量纲差异；
#   3. 阶段 2 迁移到 app/tools/kb_retriever.py，接口签名保持不变。
# ============================================================

def _tokenize(text: str):
    """中文分词（jieba），BM25 与哈希向量共用同一分词器保证口径一致。"""
    return [t for t in jieba.lcut(text) if len(t) >= 2]


def _min_max(scores: np.ndarray) -> np.ndarray:
    """把一组得分归一化到 [0,1]，全相等时返回全 0.5。"""
    lo, hi = scores.min(), scores.max()
    if hi - lo < 1e-9:
        return np.full_like(scores, 0.5)
    return (scores - lo) / (hi - lo)


def hybrid_search(query: str, index, chunks, vectors, bm25, top_k: int = TOP_K):
    """【核心模块②】入口：混合检索返回 top_k 条制度条款。"""
    # ---- 路 1：BM25 关键词得分 ----
    bm25_scores = np.array(bm25.get_scores(_tokenize(query)), dtype="float32")

    # ---- 路 2：向量语义得分（查询向量化后与 FAISS 全量内积）----
    q_vec = embed_texts([query])
    vec_scores, _ = index.search(q_vec, len(chunks))  # 返回按内积降序的全量得分
    vec_full = np.zeros(len(chunks), dtype="float32")
    order = np.argsort(vec_scores[0])[::-1]           # 恢复到原始片序
    vec_full[order] = vec_scores[0]

    # ---- 加权融合（各自归一化后线性加权）----
    fused = W_BM25 * _min_max(bm25_scores) + W_VECTOR * _min_max(vec_full)
    top_idx = np.argsort(fused)[::-1][:top_k]

    results = []
    for rank, idx in enumerate(top_idx, 1):
        results.append({
            "rank": rank,
            "score": float(fused[idx]),
            "chunk": chunks[idx],
        })
    return results


# ============================================================
# 提示词组装 + 主流程（阶段 2 迁移到 agents/orchestrator.py + prompts/）
# ============================================================

SYSTEM_PROMPT = (
    "你是连锁超市的补货制度助手。请严格依据下方提供的制度条款回答问题，"
    "不要编造条款里没有的内容；若条款不足以回答，请明确说「制度文档中未找到相关规定」。"
    "回答使用简体中文，条理清晰。"
)


def build_prompt(question: str, hits) -> str:
    """把检索到的 top3 条款拼入提示词（带编号引用，便于 LLM 引据作答）。"""
    context = "\n\n".join(
        f"[制度条款 {h['rank']}]\n{h['chunk']}" for h in hits
    )
    return (
        f"以下是补货制度文档中与问题最相关的条款：\n\n{context}\n\n"
        f"---\n\n请基于以上条款回答用户问题：{question}"
    )


def main():
    # ---- 步骤 1：加载知识库（启动时一次性构建，常驻内存）----
    if not os.path.exists(KB_PATH):
        print(f"[错误] 未找到制度文档：{KB_PATH}")
        sys.exit(1)
    text = load_and_clean_markdown(KB_PATH)
    chunks = split_chunks(text)
    index, chunks, vectors = build_vector_index(chunks)

    # ---- BM25 索引：对全部切片建一次即可 ----
    bm25 = BM25Okapi([_tokenize(c) for c in chunks])
    print("[知识库] BM25 索引建立完成\n")

    llm_ready = all(os.environ.get(k, "").strip()
                    for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"))
    if llm_ready:
        print(f"[模式] LLM 已配置（model={os.environ['LLM_MODEL']}），进入问答模式")
    else:
        print("[模式] 未配置 LLM，进入仅检索模式（输出 top3 命中条款）")
    print("=" * 60)
    print("输入补货制度相关问题（示例：整箱规格是什么？/ 促销期间怎么报数？")
    print(" / 超过分配上限怎么办？），输入 q 退出")
    print("=" * 60)

    # ---- 步骤 2：命令行问答循环 ----
    while True:
        try:
            question = input("\n你的问题 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见")
            break
        if not question:
            continue
        if question.lower() in ("q", "quit", "exit"):
            print("再见")
            break

        # 混合检索 top3（【核心模块②】）
        hits = hybrid_search(question, index, chunks, vectors, bm25)

        print(f"\n── 检索命中 top{len(hits)}（BM25 {W_BM25} + 向量 {W_VECTOR} 融合）──")
        for h in hits:
            preview = h["chunk"][:60].replace("\n", " ")
            print(f"  #{h['rank']} score={h['score']:.3f} | {preview}...")

        # LLM 生成回答（【核心模块③】）；未配置时降级为仅检索
        answer = call_llm(build_prompt(question, hits), SYSTEM_PROMPT)
        if answer:
            print("\n── 助手回答 ──")
            print(answer)
        else:
            print("\n── 仅检索模式：请参考上方命中的制度条款原文 ──")


if __name__ == "__main__":
    main()
