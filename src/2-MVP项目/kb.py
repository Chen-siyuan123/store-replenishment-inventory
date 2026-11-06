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
import sys
import time
import hashlib

import numpy as np
import requests
import jieba
import faiss
from rank_bm25 import BM25Okapi

# 触发 .env 加载（幂等）。放在第三方库之后、模块级常量之前，
# 保证 EMBED 配置与 kbstore 的门限在读取时 .env 已经生效。
import config

# ============================================================
# 配置（阶段 2 迁移时统一到 app/core/config.py）
# ============================================================

KB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "补货制度.md")  # 知识库文档路径（src/2-MVP项目/补货制度.md）

CHUNK_SIZE = 500    # 每片最大 500 字
CHUNK_OVERLAP = 50  # 相邻片重叠 50 字，防止条款被切断后丢失上下文
W_VECTOR = 0.6      # 混合检索权重：向量语义 0.6
W_BM25 = 0.4        #              BM25 关键词 0.4
TOP_K = 3           # 检索返回条数

# 向量维度上限：智谱 embedding-3 输出 2048 维。
# 换用维度更低的模型时右侧补零（补零不改变余弦相似度），
# 维度更高则截断（会轻微损失语义，但保持索引维度统一）。
EMBED_DIM = 2048

# 向量模型配置键。刻意**不**回退到 LLM_MODEL：
# 对话模型调 /embeddings 会直接 400（智谱错误码 1213），
# 之前正是这个回退让「只配三个变量」的启动方式必然失败。
EMBED_CONFIG_KEY = "LLM_EMBED_MODEL"


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


class EmbedError(Exception):
    """向量化失败。message 已是中文，可直接返回前端或写入日志。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


_degraded_warned = False   # 降级告警只在进程内打印一次，避免刷屏


def _embed_conf():
    """读取向量化配置，返回 (base_url, api_key, model)。

    注意 model 只取 LLM_EMBED_MODEL，不再拿 LLM_MODEL 兜底：
    对话模型名传给 /embeddings 会被供应商拒绝，报错信息还很难懂。
    """
    return (config.get("LLM_BASE_URL"),
            config.get("LLM_API_KEY"),
            config.get(EMBED_CONFIG_KEY))


def embed_provider_id() -> str:
    """当前向量化实现的指纹，用于判断库里存的向量是否过期。

    业务背景：切片向量是建库时算好落库的（重建索引零 API 成本）。
    一旦从「哈希伪向量」切到「真实 embedding」，库里的旧向量与新查询向量
    不在同一语义空间，混用会让相似度完全没有意义。
    因此把指纹存进 kb_meta，rebuild 时不一致就自动重算。
    """
    base_url, api_key, model = _embed_conf()
    if base_url and api_key and model:
        return f"api:{model}:{EMBED_DIM}"
    return f"hash-bag:{EMBED_DIM}"


def _explain_embed_http_error(resp, model: str) -> str:
    """把供应商的英文/晦涩报错翻译成可执行的中文提示。"""
    raw = (resp.text or "")[:200]
    code, msg = "", ""
    try:
        j = resp.json()
        err = j.get("error") or {}
        code = str(err.get("code") or j.get("code") or "")
        msg = str(err.get("message") or j.get("message") or "")
    except ValueError:
        pass

    if resp.status_code == 401:
        return "向量化鉴权失败（401）：LLM_API_KEY 无效或已过期，请到控制台确认"
    if resp.status_code == 429:
        return "向量化被限流（429）：调用过于频繁，请稍后重试"
    if resp.status_code == 404 or code in ("1211", "1212"):
        return f"向量化模型不存在（{model}）：请确认 LLM_EMBED_MODEL 填的是 embedding-3"
    if code == "1213" or resp.status_code == 400:
        # 智谱 1213：「未正常接收到 prompt 参数」——实际最常见的原因是模型名填错
        return (f"向量化请求被拒绝（模型 {model}）。最常见原因：把对话模型填进了 "
                f"LLM_EMBED_MODEL，该位置必须填向量模型 embedding-3。"
                f"供应商原始信息：{msg or raw}")
    return f"向量化接口返回 HTTP {resp.status_code}：{msg or raw}"


def _warn_degraded_once(base_url: str, api_key: str):
    """未配置向量模型时打印一次醒目告警（走 stderr，不干扰 SSE 输出）。"""
    global _degraded_warned
    if _degraded_warned:
        return
    _degraded_warned = True
    reason = ("未配置 LLM_BASE_URL / LLM_API_KEY" if not (base_url and api_key)
              else f"未配置 {EMBED_CONFIG_KEY}")
    sys.stderr.write(
        f"\n[向量化][WARN] {reason}，已降级为哈希伪向量。\n"
        f"               后果：语义检索不参与排序，自动退化为纯 BM25 关键词检索"
        f"（关键词能命中时结果仍可用，但同义改写、口语化提问会查不到）。\n"
        f"               修复：在 .env 中设置 {EMBED_CONFIG_KEY}=embedding-3 后重启。\n\n"
    )
    sys.stderr.flush()


def embed_texts(texts):
    """批量向量化，返回已 L2 归一化的矩阵（配合 IndexFlatIP 即余弦相似度）。

    三条路径：
      ① 已配置向量模型 → 调 /embeddings；任何失败都抛 EmbedError（中文可读）
      ② 未配置向量模型 → 降级哈希伪向量（仅词袋重合），并打印一次告警
      ③ 空输入       → 返回 (0, EMBED_DIM) 空矩阵

    调用约定：
      - 建库/重算向量必须让它抛错（宁可失败，也不能把伪向量写进库）
      - 在线检索请用 kbstore.search，那里会把 EmbedError 降级为纯词法检索，
        保证 embedding 接口临时故障时问答不整体不可用
    """
    texts = list(texts)
    if not texts:
        return np.zeros((0, EMBED_DIM), dtype="float32")

    base_url, api_key, model = _embed_conf()

    # ---- 路径②：未配置 → 降级 ----
    if not (base_url and api_key and model):
        _warn_degraded_once(base_url, api_key)
        return _normalize(np.array([_hash_bag_vector(t) for t in texts], dtype="float32"))

    # ---- 路径①：真实接口 ----
    # 对 429（限流）与 5xx 重试一次：免费版有并发/频率上限，批量检索时偶发的
    # 429 不该直接让检索退化为关键词模式。
    resp = None
    for attempt in range(2):
        try:
            resp = requests.post(
                url=f"{base_url.rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "input": texts},
                timeout=60,
            )
        except requests.Timeout:
            raise EmbedError(f"向量化超时（{model}，60 秒）：请稍后重试，或检查网络")
        except requests.RequestException as e:
            raise EmbedError(f"向量化请求失败（{model}）：{e}")
        if resp.status_code in (429, 500, 502, 503) and attempt == 0:
            time.sleep(1.2)
            continue
        break

    if resp.status_code != 200:
        raise EmbedError(_explain_embed_http_error(resp, model))

    try:
        vecs = np.array([d["embedding"] for d in resp.json()["data"]], dtype="float32")
    except (KeyError, TypeError, ValueError):
        raise EmbedError(f"向量化返回格式异常（{model}）：{(resp.text or '')[:120]}")
    if vecs.ndim != 2 or vecs.shape[0] != len(texts):
        raise EmbedError(f"向量化返回条数与请求不一致：请求 {len(texts)} 条，"
                         f"返回 {vecs.shape[0] if vecs.ndim else 0} 条")

    # 供应商维度可能 ≠ EMBED_DIM，补齐或截断到统一维度
    if vecs.shape[1] != EMBED_DIM:
        if vecs.shape[1] > EMBED_DIM:
            vecs = vecs[:, :EMBED_DIM]
        else:
            pad = np.zeros((vecs.shape[0], EMBED_DIM - vecs.shape[1]), dtype="float32")
            vecs = np.hstack([vecs, pad])
    return _normalize(vecs)


# ============================================================
# 查询向量缓存
# ============================================================

_QUERY_CACHE = {}          # (向量来源指纹, 查询文本) -> 归一化向量矩阵
_QUERY_CACHE_MAX = 256     # 上限：约 256 × 2048 × 4B ≈ 2MB，可忽略


def embed_query(text: str):
    """单条查询向量化，带进程内缓存。

    为什么只缓存查询、不缓存建库切片：
      切片内容可能被上传/删除改变，必须每次按原文重算；
      而查询文本高度重复（同一问题反复问、多轮追问同一主题），
      缓存既省 embedding 额度，也能显著降低触发限流的概率。

    键里带向量来源指纹：换了向量模型后旧缓存自动失效，不会串味。
    """
    key = (embed_provider_id(), text)
    hit = _QUERY_CACHE.get(key)
    if hit is not None:
        return hit

    vec = embed_texts([text])
    if len(_QUERY_CACHE) >= _QUERY_CACHE_MAX:
        _QUERY_CACHE.clear()   # 简单策略：满了整体清空，不值得为此引入 LRU
    _QUERY_CACHE[key] = vec
    return vec


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
