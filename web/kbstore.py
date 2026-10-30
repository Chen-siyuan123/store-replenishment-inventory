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
SEED_KB_PATH = os.path.join(os.path.dirname(WEB_DIR), "补货制度.md")  # 种子文档

ALLOWED_EXT = {".md", ".markdown", ".txt", ".pdf"}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB


class KBParseError(Exception):
    """文档解析失败（含中文原因，可直接返回前端）。"""


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

    # ---------- 索引构建 ----------

    def rebuild(self) -> int:
        """从数据库全量重建内存索引（上传/删除后调用）。返回切片数。"""
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT c.id, c.section, c.content, c.embedding, d.title AS doc_title "
                "FROM kb_chunks c JOIN kb_documents d ON c.doc_id = d.id ORDER BY c.id"
            ).fetchall()

        if not rows:
            self.index, self.bm25, self.chunks, self.vectors = None, None, [], None
            return 0

        self.chunks = [
            {"id": r["id"], "section": r["section"],
             "content": r["content"], "doc_title": r["doc_title"]}
            for r in rows
        ]
        # embedding 是建库时算好存库的，重建零成本
        self.vectors = np.vstack([
            np.frombuffer(r["embedding"], dtype="float32") for r in rows
        ])
        self.index = faiss.IndexFlatIP(EMBED_DIM)
        self.index.add(self.vectors)
        self.bm25 = BM25Okapi([kbcore._tokenize(c["content"]) for c in self.chunks])
        return len(rows)

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
        """导入种子文档（prototype/补货制度.md）。已存在同名文档或导入失败返回 False。"""
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
        """混合检索 top_k：BM25 + 向量加权融合。空库抛 ValueError。"""
        if self.is_empty():
            raise ValueError("知识库为空")

        # 路 1：BM25 关键词得分
        bm25_scores = np.array(self.bm25.get_scores(kbcore._tokenize(query)), dtype="float32")

        # 路 2：向量语义得分（FAISS 返回按相似度降序的 (得分, 下标) 对，
        # 必须用返回的下标回填，不能用 argsort 反推——否则得分会映射到错误切片）
        q_vec = kbcore.embed_texts([query])
        vec_scores, vec_idx = self.index.search(q_vec, len(self.chunks))
        vec_full = np.zeros(len(self.chunks), dtype="float32")
        for score, idx in zip(vec_scores[0], vec_idx[0]):
            if idx >= 0:                       # 过滤 FAISS 的占位 -1
                vec_full[idx] = score

        # 加权融合（各自 min-max 归一化）
        fused = 0.4 * kbcore._min_max(bm25_scores) + 0.6 * kbcore._min_max(vec_full)
        top_idx = np.argsort(fused)[::-1][:top_k]

        return [
            {"rank": rank, "score": round(float(fused[i]), 4),
             "vec_score": round(float(vec_full[i]), 4),  # 原始余弦分（审计 R3 用）
             "section": self.chunks[i]["section"],
             "doc_title": self.chunks[i]["doc_title"],
             "chunk": self.chunks[i]["content"]}
            for rank, i in enumerate(top_idx, 1)
        ]
