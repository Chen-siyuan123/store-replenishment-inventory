#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知识库模块 kb.py（复用阶段 1 的两处核心模块，供 Web 服务调用）
================================================================
【核心模块①】切片向量化：Markdown → 500 字切片 → 向量化 → FAISS
【核心模块②】混合检索：BM25 关键词 + 向量语义，加权融合取 top3

相比阶段 1 脚本的改进：
  - 切片时记录「所属章节标题」，检索结果可标注来源章节，
    前端即可展示「《补货制度.md》·「最小起订量与整箱规格」」这样的引用；
  - 封装为 KnowledgeBase 类，FastAPI 启动时构建一次、常驻内存。

阶段 2 迁移目标：app/rag/（splitter + embedder）+ app/tools/kb_retriever.py
"""

import os
import re
import hashlib

import numpy as np
import requests
import jieba
import faiss
from rank_bm25 import BM25Okapi

# ============================================================
# 配置（阶段 2 迁移时统一到 app/core/config.py）
# ============================================================

KB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "补货制度.md")  # 知识库文档路径（prototype/补货制度.md）

CHUNK_SIZE = 500    # 每片最大 500 字
CHUNK_OVERLAP = 50  # 相邻片重叠 50 字，防止条款被切断后丢失上下文
W_VECTOR = 0.6      # 混合检索权重：向量语义 0.6
W_BM25 = 0.4        #              BM25 关键词 0.4
TOP_K = 3           # 检索返回条数
EMBED_DIM = 2048    # 与智谱 embedding-3 输出维度一致（不同供应商自动截断/补齐）


# ============================================================
# 【核心模块①】切片向量化
# ============================================================

def load_sections(path: str):
    """按 Markdown 标题把文档切成「章节」，返回 [(章节标题, 章节正文), ...]。

    目的：让每个切片都能知道自己属于哪一节，
    这样检索命中的条款可以标出「《文档名》·「章节名」」的引用来源。
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    sections = []       # 结果列表
    current_title = "总则"  # 标题出现前的内容归入「总则」
    buf = []

    for line in raw.splitlines():
        m = re.match(r"^#{1,6}\s*(.+?)\s*$", line)  # 匹配标题行（# ~ ######）
        if m:
            # 遇到新标题：先把上一节的缓冲落袋
            if buf:
                sections.append((current_title, "\n".join(buf)))
                buf = []
            current_title = m.group(1)
        else:
            buf.append(line)
    if buf:
        sections.append((current_title, "\n".join(buf)))
    return sections


def clean_text(text: str) -> str:
    """轻量清洗：去 Markdown 标记、压缩空白，保留条款文字。"""
    text = re.sub(r"[*_`>]+", "", text)          # 去强调/引用符号
    text = re.sub(r"[-]{3,}", "", text)          # 去分隔线
    text = re.sub(r"\n{2,}", "\n", text)         # 压缩连续空行
    return text.strip()


def split_chunks(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP):
    """按 500 字滑动窗口切片（步长 450），过滤过短碎片。与阶段 1 逻辑一致。"""
    chunks = []
    step = size - overlap
    for i in range(0, len(text), step):
        piece = text[i:i + size].strip()
        if len(piece) > 20:
            chunks.append(piece)
        if i + size >= len(text):
            break
    return chunks


def _hash_bag_vector(text: str) -> np.ndarray:
    """演示用词袋哈希向量（未配置 embedding 供应商时的降级方案）。"""
    vec = np.zeros(EMBED_DIM, dtype="float32")
    for token in jieba.lcut(text):
        if len(token) < 2:
            continue
        bucket = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % EMBED_DIM
        vec[bucket] += 1.0
    return vec


def _normalize(vecs: np.ndarray) -> np.ndarray:
    """L2 归一化：归一化后 FAISS 内积 = 余弦相似度。"""
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


def embed_texts(texts):
    """批量向量化：优先走 OpenAI 兼容 /embeddings 接口，未配置则降级哈希向量。"""
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
        vecs = np.array([d["embedding"] for d in resp.json()["data"]], dtype="float32")
        # 供应商维度可能 ≠ EMBED_DIM，截断或补齐到统一维度
        if vecs.shape[1] != EMBED_DIM:
            if vecs.shape[1] > EMBED_DIM:
                vecs = vecs[:, :EMBED_DIM]
            else:
                pad = np.zeros((vecs.shape[0], EMBED_DIM - vecs.shape[1]), dtype="float32")
                vecs = np.hstack([vecs, pad])
        return _normalize(vecs)

    # ---- 演示路径：词袋哈希伪向量 ----
    return _normalize(np.array([_hash_bag_vector(t) for t in texts], dtype="float32"))


# ============================================================
# 【核心模块②】混合检索
# ============================================================

def _tokenize(text: str):
    """中文分词（jieba），BM25 与哈希向量共用，保证口径一致。"""
    return [t for t in jieba.lcut(text) if len(t) >= 2]


def _min_max(scores: np.ndarray) -> np.ndarray:
    """得分归一化到 [0,1]，全相等时返回全 0.5。"""
    lo, hi = scores.min(), scores.max()
    if hi - lo < 1e-9:
        return np.full_like(scores, 0.5)
    return (scores - lo) / (hi - lo)


class KnowledgeBase:
    """知识库容器：启动时构建一次，常驻内存供所有请求共用。"""

    def __init__(self, kb_path: str = KB_PATH):
        if not os.path.exists(kb_path):
            raise FileNotFoundError(f"未找到制度文档：{kb_path}")

        self.doc_name = os.path.basename(kb_path)  # 文档名，用于前端引用展示
        self.chunks = []   # [{"text":..., "section":...}, ...]

        # ---- 按章节切片，切片时携带章节标题 ----
        for title, body in load_sections(kb_path):
            body = clean_text(body)
            if not body:
                continue
            for piece in split_chunks(body):
                self.chunks.append({"text": piece, "section": title})

        if not self.chunks:
            raise ValueError("制度文档内容为空，无法构建知识库")

        # ---- 向量化 + FAISS 索引（IndexFlatIP + 归一化 = 余弦相似度）----
        self.vectors = embed_texts([c["text"] for c in self.chunks])
        self.index = faiss.IndexFlatIP(EMBED_DIM)
        self.index.add(self.vectors)

        # ---- BM25 索引：对全部切片建一次 ----
        self.bm25 = BM25Okapi([_tokenize(c["text"]) for c in self.chunks])

    def search(self, query: str, top_k: int = TOP_K):
        """混合检索：BM25 得分 + 向量得分各自归一化后加权融合，取 top_k。"""
        # 路 1：BM25 关键词得分
        bm25_scores = np.array(self.bm25.get_scores(_tokenize(query)), dtype="float32")

        # 路 2：向量语义得分（查询向量化后与 FAISS 全量内积）
        q_vec = embed_texts([query])
        vec_scores, _ = self.index.search(q_vec, len(self.chunks))
        vec_full = np.zeros(len(self.chunks), dtype="float32")
        order = np.argsort(vec_scores[0])[::-1]   # 恢复到原始片序
        vec_full[order] = vec_scores[0]

        # 加权融合
        fused = W_BM25 * _min_max(bm25_scores) + W_VECTOR * _min_max(vec_full)
        top_idx = np.argsort(fused)[::-1][:top_k]

        results = []
        for rank, idx in enumerate(top_idx, 1):
            results.append({
                "rank": rank,
                "score": round(float(fused[idx]), 4),
                "section": self.chunks[idx]["section"],  # 章节名，用于引用展示
                "chunk": self.chunks[idx]["text"],
            })
        return results

    def build_prompt(self, question: str, hits) -> str:
        """把检索到的条款拼入提示词（带编号引用，便于 LLM 引据作答）。"""
        context = "\n\n".join(f"[制度条款 {h['rank']}]\n{h['chunk']}" for h in hits)
        return (
            f"以下是补货制度文档中与问题最相关的条款：\n\n{context}\n\n"
            f"---\n\n请基于以上条款回答用户问题：{question}"
        )
