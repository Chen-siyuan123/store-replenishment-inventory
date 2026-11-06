#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
知识库存储层 kbstore.py（管理后台批次 2）
================================================================
核心变化：知识库从「单文件内存」迁移为数据库存储——
  kb_documents  文档表（元信息）
  kb_chunks     切片表（doc_id / section / 原文 / embedding 二进制）
  FAISS + BM25  内存索引，由库中切片构建；增删文档后自动重建

embedding 落库的好处：重建索引直接读二进制向量，不重复调用供应商接口。

支持的文档格式：
  .md / .markdown / .txt —— 按 Markdown 标题切章节，再 500 字切片
  .pdf                    —— pypdf 提取文字层后整体切片（扫描件不支持）

复用阶段 1 核心模块①②（kb.py）：切片 / 向量化 / 分词 / 混合检索逻辑
阶段 2 迁移目标：app/rag/ + app/tools/kb_retriever.py
"""

import os
import re

import numpy as np
import faiss
from rank_bm25 import BM25Okapi

import db
import kb as kbcore          # 复用：切片、向量化、分词、归一化
from kb import EMBED_DIM

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(WEB_DIR, "uploads")     # 上传原件留存目录
SEED_KB_PATH = os.path.join(os.path.dirname(WEB_DIR), "data", "补货制度.md")  # 种子文档

ALLOWED_EXT = {".md", ".markdown", ".txt", ".pdf"}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

# ============================================================
# 检索相关度门限（可按真实语料用环境变量调参）
# ------------------------------------------------------------
# 三个门限解决两个具体问题：
#   ① 排序错位：语义路不可用时（如未配置 embedding 接口，降级为哈希伪向量，
#      余弦分只有 0.0x），若仍按 0.6 权重参与 min-max 融合，噪声会被放大到 0~1，
#      把 BM25 正确命中的条款顶掉。故先判断语义路是否「可信」，不可信就退化为纯 BM25。
#   ② 无答案硬答：检索永远返回 top_k 条，陷阱问题也会拿到高分片段，模型据此编造。
#      故在排序前加一道「知识库里到底有没有这个话题」的门禁，判不出来就返回空。
# ============================================================
VEC_MIN_INFO = float(os.environ.get("KB_VEC_MIN_INFO", "0.30"))    # 语义路可信下限（离线伪向量实测 ≤0.06）
VEC_MIN_HIT = float(os.environ.get("KB_VEC_MIN_HIT", "0.55"))      # 语义中等命中（需配合关键词密度）
VEC_STRONG_HIT = float(os.environ.get("KB_VEC_STRONG_HIT", "0.68"))  # 语义强命中（可单独放行，应对口语化提问）
BM25_MIN_DENSITY = float(os.environ.get("KB_BM25_MIN_DENSITY", "0.20"))  # 纯词法模式下每词平均得分下限
MIN_EXPAND_JACCARD = float(os.environ.get("KB_MIN_EXPAND_JACCARD", "0.60"))  # 词形归一的字符集重合率下限

# ---- 向量来源指纹：解决「库里存的是旧向量」这一类静默故障 ----
# 切片向量是建库时算好落库的（重建索引零 API 成本）。但从哈希伪向量切到真实
# embedding、或换了向量模型之后，库里旧向量与新查询向量不在同一语义空间，
# 相似度完全失去意义，而检索本身不会报错——只会悄悄给出错误答案。
# 因此把「谁算的、多少维」记进 kb_meta，rebuild 时比对，不一致就整库重算。
META_EMBED_ID = "embed_provider_id"
REEMBED_BATCH = 16          # 单次 /embeddings 请求的切片数（越小越稳，避免超单请求上限）


class KBParseError(Exception):
    """文档解析失败（含中文原因，可直接返回前端）。"""


# ============================================================
# kb_meta 读写（键值表，目前只存向量来源指纹）
# ============================================================

def _get_meta(key: str, default=None):
    with db.get_db() as conn:
        row = conn.execute("SELECT value FROM kb_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _set_meta(key: str, value: str):
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO kb_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ============================================================
# 文档解析
# ============================================================

def _sections_from_text(text: str):
    """按 Markdown 标题把文本切为 [(章节标题, 正文)]（无标题则归入「正文」）。"""
    sections, current, buf = [], "正文", []
    for line in text.splitlines():
        m = re.match(r"^#{1,6}\s*(.+?)\s*$", line)
        if m:
            if buf:
                sections.append((current, "\n".join(buf)))
                buf = []
            current = m.group(1)
        else:
            buf.append(line)
    if buf:
        sections.append((current, "\n".join(buf)))
    return sections


def extract_text(path: str, ext: str) -> str:
    """从文件提取纯文本。md 直读；pdf 用 pypdf 提取文字层。"""
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(path)
            pages = [(page.extract_text() or "") for page in reader.pages]
        except Exception as e:
            raise KBParseError(f"PDF 解析失败：{e}")
        text = "\n".join(pages).strip()
        if len(text) < 20:
            raise KBParseError("该 PDF 没有可提取的文字层（可能是扫描件），暂不支持")
        return text
    # md / txt：直接读
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


# ============================================================
# KB 存储与检索
# ============================================================

class KBStore:
    """知识库容器：从数据库加载切片构建内存索引，供问答检索。"""

    def __init__(self):
        self.index = None    # FAISS 内积索引
        self.bm25 = None     # BM25 索引
        self.chunks = []     # [{"id","section","content","doc_title"}]
        self.vectors = None
        self.last_mode = "hybrid"        # 本次检索实际走的模式：hybrid / lexical / lexical-hard
        self.last_miss_reason = ""       # 返回空结果时的原因（供工具层与日志展示）
        self.vocab_df = {}               # 语料词 -> 文档频次（词形归一用）
        self.embed_stale = False         # True = 库内向量与当前配置不同源且重算失败，禁用语义路
        self.last_embed_error = ""       # 最近一次向量化失败原因（检索降级时记录）
        self.last_reembed = None         # 最近一次重算记录 {from,to,chunks}（启动日志用）

    # ---------- 向量来源一致性 ----------

    def _reembed_chunks(self, batch: int = REEMBED_BATCH) -> int:
        """用当前向量模型重算全部切片向量并回写，返回切片数。

        两阶段写入：先把所有批次算完再统一 UPDATE。
        这样中途失败时库内仍是整库旧向量（同源，至少语义自洽），
        不会出现「一半新向量一半旧向量」的混合状态。
        """
        with db.get_db() as conn:
            rows = conn.execute("SELECT id, content FROM kb_chunks ORDER BY id").fetchall()
        if not rows:
            return 0

        new_vecs = []
        for i in range(0, len(rows), batch):
            part = rows[i:i + batch]
            new_vecs.append(kbcore.embed_texts([r["content"] for r in part]))
        all_vecs = np.vstack(new_vecs)

        with db.get_db() as conn:
            conn.executemany(
                "UPDATE kb_chunks SET embedding = ? WHERE id = ?",
                [(vec.tobytes(), row["id"]) for row, vec in zip(rows, all_vecs)],
            )
        return len(rows)

    def _ensure_embed_provider(self) -> bool:
        """确保库内向量与当前配置同源；不一致则整库重算。返回是否发生重算。

        失败不抛异常，而是标记 embed_stale：服务照常启动，检索退化为纯词法，
        并在启动日志里明确告警——比起直接起不来，让店长先能用关键词查制度更实际。
        """
        current = kbcore.embed_provider_id()
        stored = _get_meta(META_EMBED_ID)
        if stored == current:
            return False

        try:
            n = self._reembed_chunks()
        except kbcore.EmbedError as e:
            self.embed_stale = True
            self.last_embed_error = e.message
            print(f"[知识库][WARN] 向量重算失败，语义检索已禁用（本次仅用关键词）：{e.message}")
            return False
        except Exception as e:                      # 网络/DB 意外
            self.embed_stale = True
            self.last_embed_error = f"{type(e).__name__}: {e}"
            print(f"[知识库][WARN] 向量重算异常，语义检索已禁用（本次仅用关键词）：{e}")
            return False

        _set_meta(META_EMBED_ID, current)
        self.embed_stale = False
        self.last_reembed = {"from": stored or "(未记录)", "to": current, "chunks": n}
        print(f"[知识库] 向量来源已变更：{stored or '(未记录)'} → {current}，重算 {n} 个切片")
        return True

    # ---------- 索引构建 ----------

    def rebuild(self) -> int:
        """从数据库全量重建内存索引（上传/删除后调用）。返回切片数。

        重建前先校验向量来源：切换 embedding 供应商或换模型后，
        库里旧向量的语义空间已经不同，必须先重算否则检索结果会静默错乱。
        """
        self._ensure_embed_provider()

        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT c.id, c.section, c.content, c.embedding, d.title AS doc_title "
                "FROM kb_chunks c JOIN kb_documents d ON c.doc_id = d.id ORDER BY c.id"
            ).fetchall()

        if not rows:
            self.index, self.bm25, self.chunks, self.vectors = None, None, [], None
            self.vocab_df = {}
            return 0

        self.chunks = [
            {"id": r["id"], "section": r["section"],
             "content": r["content"], "doc_title": r["doc_title"]}
            for r in rows
        ]
        # embedding 是建库时算好存库的，重建索引零成本（前提是来源已校验一致）
        self.vectors = np.vstack([
            np.frombuffer(r["embedding"], dtype="float32") for r in rows
        ])
        self.index = faiss.IndexFlatIP(EMBED_DIM)
        self.index.add(self.vectors)
        self.bm25 = BM25Okapi([kbcore._tokenize(c["content"]) for c in self.chunks])

        # 语料词表 + 文档频次：用于把被分词切碎的口语词映射回语料里真实存在的词
        self.vocab_df = {}
        for freqs in self.bm25.doc_freqs:
            for term in freqs:
                self.vocab_df[term] = self.vocab_df.get(term, 0) + 1
        return len(rows)

    def _expand_tokens(self, tokens):
        """词形归一：只处理语料里不存在的查询词（df==0），把它们替换成语料中字面最接近的词。

        例：jieba 把「起订量」切成「订量」（起 被单独切走），语料里只有「起订量」，
        于是「订量」永远匹配不到 MOQ 条款；本函数按字符集重合率把它归一回「起订量」。

        只为 df==0 的词找候选，正常词原样保留，因此不会影响已能命中的查询，
        也不会把「冷链/资质」这类语料中确实没有的话题硬凑成命中（无候选就保持原词）。
        """
        out = []
        for t in tokens:
            if self.vocab_df.get(t, 0) > 0:
                out.append(t)
                continue
            chars_t = set(t)
            best = None
            for w in self.vocab_df:
                if abs(len(w) - len(t)) > 1:
                    continue
                chars_w = set(w)
                union = len(chars_t | chars_w)
                if union and len(chars_t & chars_w) / union >= MIN_EXPAND_JACCARD:
                    best = w
                    break
            out.append(best or t)
        return out


    def is_empty(self) -> bool:
        return self.index is None

    # ---------- 文档管理 ----------

    def add_document(self, filename: str, data: bytes,
                     doc_type: str, uploaded_by: str) -> int:
        """保存上传文件 → 解析 → 切片 → 向量化 → 入库 → 重建索引。返回切片数。"""
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_EXT:
            raise KBParseError(f"不支持的文件格式 {ext}，仅支持：md / txt / pdf")
        if len(data) > MAX_FILE_SIZE:
            raise KBParseError("文件超过 5MB 上限")

        # 新切片会用「当前」向量模型计算。若库内还是旧来源，先整库重算，
        # 否则新旧切片不同源，检索时混算相似度会失真。
        self._ensure_embed_provider()

        # 原件留存（审计/追溯用）
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        safe_name = f"{db.now().replace(':', '')}_{filename}"
        disk_path = os.path.join(UPLOAD_DIR, safe_name)
        with open(disk_path, "wb") as f:
            f.write(data)

        # 提取文本 → 按章节切片（复用核心模块①的切片逻辑）
        text = extract_text(disk_path, ext)
        if len(text.strip()) < 20:
            raise KBParseError("文档内容过短或为空")
        sections = _sections_from_text(text) if ext != ".pdf" else [("正文", text)]

        chunk_rows = []  # (section, content)
        for title, body in sections:
            body = kbcore.clean_text(body)
            if not body:
                continue
            for piece in kbcore.split_chunks(body):
                chunk_rows.append((title, piece))
        if not chunk_rows:
            raise KBParseError("文档解析后无有效内容")

        # 批量向量化（复用核心模块①的 embedding 逻辑）
        vectors = kbcore.embed_texts([c[1] for c in chunk_rows])

        # 入库：文档 + 切片（embedding 存二进制）
        title = os.path.splitext(filename)[0]
        with db.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO kb_documents (title, filename, doc_type, chunk_count, "
                "uploaded_by, created_at) VALUES (?,?,?,?,?,?)",
                (title, filename, doc_type, len(chunk_rows), uploaded_by, db.now()),
            )
            doc_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO kb_chunks (doc_id, section, content, embedding) "
                "VALUES (?,?,?,?)",
                [(doc_id, sec, content, vec.tobytes())
                 for (sec, content), vec in zip(chunk_rows, vectors)],
            )

        self.rebuild()
        return len(chunk_rows)

    def delete_document(self, doc_id: int):
        """删除文档及全部切片，并重建索引。文档不存在抛 KeyError。"""
        with db.get_db() as conn:
            row = conn.execute("SELECT title FROM kb_documents WHERE id = ?", (doc_id,)).fetchone()
            if not row:
                raise KeyError(doc_id)
            conn.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM kb_documents WHERE id = ?", (doc_id,))
        self.rebuild()

    def list_documents(self):
        """文档列表（含切片数）。"""
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT id, title, filename, doc_type, chunk_count, uploaded_by, created_at "
                "FROM kb_documents ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def seed_default(self, uploaded_by: str = "system") -> bool:
        """导入种子文档（src/data/补货制度.md）。已存在同名文档或导入失败返回 False。"""
        if not os.path.exists(SEED_KB_PATH):
            return False
        with open(SEED_KB_PATH, "rb") as f:
            data = f.read()
        try:
            self.add_document(os.path.basename(SEED_KB_PATH), data, "补货制度", uploaded_by)
            return True
        except KBParseError:
            return False

    # ---------- 检索（复用核心模块②：BM25 + 向量混合） ----------

    def build_prompt(self, question: str, hits) -> str:
        """把检索到的条款拼入提示词（带编号引用）。"""
        context = "\n\n".join(f"[制度条款 {h['rank']}]\n{h['chunk']}" for h in hits)
        return (
            f"以下是补货制度文档中与问题最相关的条款：\n\n{context}\n\n"
            f"---\n\n请基于以上条款回答用户问题：{question}"
        )

    def search(self, query: str, top_k: int = 3):
        """混合检索 top_k：BM25 + 向量加权融合，带相关度门禁。空库抛 ValueError。

        返回 [] 表示「知识库中确实没有相关条款」——调用方据此回答「制度中未找到」，
        而不是拿 top1 硬编答案。判定理由写在 self.last_miss_reason，检索模式写在
        self.last_mode，供工具层与日志展示。
        """
        if self.is_empty():
            raise ValueError("知识库为空")

        self.last_miss_reason = ""
        self.last_mode = "hybrid"
        q = (query or "").strip()
        tokens = kbcore._tokenize(q)
        if not q or not tokens:
            self.last_miss_reason = "检索词为空或分词后无有效词"
            return []
        # 词形归一（订量→起订量），避免分词粒度导致整条条款召回不到
        tokens = self._expand_tokens(tokens)

        # 路 1：BM25 关键词得分
        bm25_scores = np.array(self.bm25.get_scores(tokens), dtype="float32")

        # 路 2：向量语义得分（FAISS 返回按相似度降序的 (得分, 下标) 对，
        # 必须用返回的下标回填，不能用 argsort 反推——否则得分会映射到错误切片）
        vec_full = np.zeros(len(self.chunks), dtype="float32")
        self.last_embed_error = ""

        if self.embed_stale:
            # 库内向量与当前配置不同源（重算失败过）：绝不能混用两种向量算相似度
            self.last_embed_error = "库内向量与当前向量模型不同源，语义检索已禁用"
        else:
            try:
                q_vec = kbcore.embed_query(q)   # 带缓存：重复提问不重复烧额度
                vec_scores, vec_idx = self.index.search(q_vec, len(self.chunks))
                for score, idx in zip(vec_scores[0], vec_idx[0]):
                    if idx >= 0:                       # 过滤 FAISS 的占位 -1
                        vec_full[idx] = score
            except kbcore.EmbedError as e:
                # 在线检索时向量化失败（限流/网络/额度）：本次降级为纯词法，不打断问答。
                # 若让异常冒出去，一次接口抖动就会让整个问答不可用。
                self.last_embed_error = e.message

        vec_max = float(vec_full.max())
        bm25_max = float(bm25_scores.max())
        # 语义路是否「可信」：离线降级时是哈希伪向量，余弦分只有 0.0x，
        # 此时它不含任何语义信息，不能参与融合（否则 0.6 权重会把噪声放大成排序依据）
        vec_ok = (not self.last_embed_error) and vec_max >= VEC_MIN_INFO

        # ---- 相关度门禁：先判断知识库里有没有这个话题 ----
        # 词法密度用「每词平均得分」而不是绝对分，避免语料规模变化导致阈值漂移
        density = bm25_max / max(len(tokens), 1)

        if vec_ok:
            # 两路证据的取舍依据（embedding-3 + 现行制度的实测标定，可用 eval/probe_retrieval.py 复现）：
            #   正常制度题   vec 0.553~0.727   density 0.276~0.989
            #   无答案陷阱题  vec 0.491~0.582   density 0.060~0.104
            # 单看语义分两段有重叠（0.553 vs 0.582），单看关键词则会漏掉口语化改写，
            # 因此取「双条件」：语义强命中可单独放行；否则要求语义中等 + 关键词有密度。
            strong_hit = vec_max >= VEC_STRONG_HIT
            medium_hit = vec_max >= VEC_MIN_HIT and density >= BM25_MIN_DENSITY
            if not (strong_hit or medium_hit):
                self.last_miss_reason = (
                    f"证据不足：语义最高 {vec_max:.3f}（强命中需 ≥{VEC_STRONG_HIT:.2f}），"
                    f"关键词密度 {density:.3f}（需 ≥{BM25_MIN_DENSITY:.2f}）")
                return []
            candidates = np.flatnonzero((vec_full >= VEC_MIN_HIT) | (bm25_scores > 0))
        else:
            if density < BM25_MIN_DENSITY:
                self.last_miss_reason = (
                    f"关键词匹配密度 {density:.3f} 低于门限 {BM25_MIN_DENSITY}"
                    f"（向量路不可用，已降级为纯词法检索），知识库内无相关条款")
                return []
            candidates = np.flatnonzero(bm25_scores > 0)

        if candidates.size == 0:
            self.last_miss_reason = "没有切片提供词法或语义证据"
            return []

        # ---- 只在候选集内排序与归一化（0 分切片不参与竞争）----
        if vec_ok:
            fused = (0.4 * kbcore._min_max(bm25_scores[candidates])
                     + 0.6 * kbcore._min_max(vec_full[candidates]))
        else:
            # 语义路不可信 → 纯 BM25 排序，等权归一化到 [0,1]
            # lexical-hard 表示「向量路本次不可用」（库内不同源或接口失败），日志里要能区分
            self.last_mode = "lexical-hard" if self.last_embed_error else "lexical"
            fused = kbcore._min_max(bm25_scores[candidates])

        score_map = {int(i): float(s) for i, s in zip(candidates, fused)}
        order = sorted(score_map, key=score_map.get, reverse=True)[:top_k]

        return [
            {"rank": rank,
             "score": round(score_map[i], 4),
             "vec_score": round(float(vec_full[i]), 4),  # 原始余弦分（审计 R3 用）
             "section": self.chunks[i]["section"],
             "doc_title": self.chunks[i]["doc_title"],
             "chunk": self.chunks[i]["content"]}
            for rank, i in enumerate(order, 1)
        ]
