#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据层 db.py（管理后台批次 1：完整建表 + 种子数据）
================================================================
表清单：
  users             用户（店长/管理员，PBKDF2 密码哈希）
  prompt_templates  提示词模板（版本化，is_active 标记生效版本）
  kb_documents      制度知识库文档（批次 2 使用）
  kb_chunks         知识库切片（含 doc_id / section / embedding 二进制）
  sessions          会话（批次 1 起记录 user_id / store_id，行级权限锚点）
  messages          消息（批次 4 起写入 audit 异常标记）

兼容旧库：chat.db 已存在时自动 ALTER TABLE 补列，不丢数据。
"""

import os
import sqlite3
import hashlib
import secrets
from datetime import datetime

from auth import hash_password  # 密码哈希复用认证模块的实现

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat.db")

DEFAULT_PROMPT = (
    "你是门店补货与库存协同制度助手，只根据提供的制度条文回答，"
    "涉及起订量、整箱规格、分配上限、配送日历、缺货口径时必须引用具体条文数值，"
    "不确定时明确说'制度中未找到'，回答末尾附引用来源，"
    "不得编造补货标准或替店长决定是否补货。"
)


def get_db():
    """获取 SQLite 连接（行结果可按列名取值）。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _add_column(conn, table: str, column: str, decl: str):
    """安全加列：检查列是否存在，不存在才 ALTER（兼容旧 chat.db）。"""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db():
    """建表（幂等）+ 旧库迁移 + 种子数据。"""
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL,        -- store_manager / admin
            store_id      TEXT,                 -- 店长归属门店，admin 为空
            store_name    TEXT,
            status        TEXT DEFAULT 'active',
            created_at    TEXT
        );
        CREATE TABLE IF NOT EXISTS prompt_templates (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            content    TEXT NOT NULL,
            is_active  INTEGER DEFAULT 0,       -- 1 = 当前生效版本
            updated_by TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS kb_documents (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            filename    TEXT,
            doc_type    TEXT,                    -- 补货制度/配送日历/促销规则/审批流程等
            chunk_count INTEGER DEFAULT 0,
            uploaded_by TEXT,
            created_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS kb_chunks (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id    INTEGER NOT NULL,          -- 归属文档（删除文档级联删切片）
            section   TEXT,                      -- 所属章节标题（引用展示用）
            content   TEXT NOT NULL,             -- 切片原文
            embedding BLOB                       -- float32 向量二进制（重建索引免重算）
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id         TEXT PRIMARY KEY,
            user_id    INTEGER,                  -- 归属用户（行级权限）
            store_id   TEXT,
            title      TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            sources    TEXT,
            audit      TEXT,                     -- 异常标记 JSON（批次 4）
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_chunks_doc ON kb_chunks(doc_id);
        """)

        # ---- 旧库迁移：早期建表可能缺列 ----
        _add_column(conn, "sessions", "user_id", "INTEGER")
        _add_column(conn, "sessions", "store_id", "TEXT")
        _add_column(conn, "messages", "audit", "TEXT")

        _seed(conn)


def _seed(conn):
    """种子数据：预置账号 + 默认提示词模板（幂等）。"""
    now = datetime.now().isoformat(timespec="seconds")

    # 预置账号（原型阶段；正式版应首次登录强制改密）
    if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        conn.executemany(
            "INSERT INTO users (username, password_hash, role, store_id, store_name, created_at) "
            "VALUES (?,?,?,?,?,?)",
            [
                ("admin",   hash_password("admin@123"),  "admin",         None, "总部", now),
                ("store01", hash_password("store@123"), "store_manager", "S001", "朝阳路店", now),
            ],
        )

    # 默认提示词模板（用户提供），设为生效
    if not conn.execute("SELECT 1 FROM prompt_templates LIMIT 1").fetchone():
        conn.execute(
            "INSERT INTO prompt_templates (name, content, is_active, updated_by, updated_at) "
            "VALUES (?,?,?,?,?)",
            ("默认模板", DEFAULT_PROMPT, 1, "system", now),
        )


def get_active_prompt() -> str:
    """读取当前生效的系统提示词（每次问答实时读取，改完即生效）。"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT content FROM prompt_templates WHERE is_active = 1 "
            "ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    # 无生效模板时兜底用默认文案（不应发生，seed 已保证有）
    return row["content"] if row else DEFAULT_PROMPT


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
