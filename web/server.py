#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
门店补货与库存协同智能体系统 · Web 服务（FastAPI 后端）
================================================================
批次 1（认证体系）：
  POST /api/login          登录（JWT）
  GET  /api/me             当前用户信息
  GET  /login              登录页（SSR）
既有接口（已接入鉴权）：
  POST /chat               问答主接口（SSE 流式，需登录）
  GET  /sessions           会话列表（只看本人）
  GET  /sessions/{id}/messages / DELETE /sessions/{id}
  GET  /                   问答页

错误约定：
  - 校验类错误以 JSON 返回 {code, message}（中文）；
    401 = 未登录/过期（UNAUTHORIZED / TOKEN_EXPIRED）
    403 = 权限不足（PERMISSION_DENIED）
  - /chat 建流后发生的错误通过 SSE error 事件推送。

启动方式：
  set LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
  set LLM_API_KEY=<你的key>
  set LLM_MODEL=glm-4-flash
  set LLM_EMBED_MODEL=embedding-3
  python -m uvicorn server:app --port 8765
"""

import json
import os
import re
import uuid

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional

import db       # 数据层（建表/种子/连接）
import kbstore  # 【核心模块①②】知识库存取 + 混合检索（批次 2 数据库化）
import llm      # 【核心模块③】LLM 流式封装
from auth import (create_token, verify_password,
                  get_current_user, require_admin)
from kbstore import KBParseError

# ============================================================
# 全局配置
# ============================================================

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(WEB_DIR, "index.html")
templates = Jinja2Templates(directory=os.path.join(WEB_DIR, "templates"))

MAX_QUESTION_LEN = 500
HISTORY_ROUNDS = 6  # 每次请求携带最近 6 轮历史（12 条消息）

app = FastAPI(title="门店补货与库存协同 · 店长问答与管理后台")


# ============================================================
# 统一错误响应
# ============================================================

def err(code: str, message: str, status: int):
    return JSONResponse(status_code=status, content={"code": code, "message": message})


def sse_event(event: str, data: dict) -> str:
    """构造一条 SSE 事件文本。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ============================================================
# 启动事件：初始化数据库 + 构建知识库
# ============================================================

@app.on_event("startup")
def startup():
    db.init_db()  # 建表 + 种子账号 + 默认提示词模板
    try:
        app.state.kb = kbstore.KBStore()
        if app.state.kb.rebuild() == 0:
            # 知识库为空时自动导入种子文档（补货制度.md）
            if app.state.kb.seed_default():
                print("[知识库] 已自动导入种子文档：补货制度.md")
        print(f"[知识库] 构建完成：{len(app.state.kb.chunks)} 个切片")
    except Exception as e:
        app.state.kb = None
        print(f"[知识库] 构建失败：{e}")


# ============================================================
# 页面路由
# ============================================================

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    """登录页（SSR）。"""
    return templates.TemplateResponse(request, "login.html")


@app.get("/", response_class=HTMLResponse)
def index():
    """店长问答页。"""
    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ============================================================
# 认证接口
# ============================================================

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(req: LoginRequest):
    """登录：校验密码 → 签发 JWT（payload 含角色与门店）。"""
    with db.get_db() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE username = ? AND status = 'active'",
            (req.username.strip(),),
        ).fetchone()

    if not user or not verify_password(req.password, user["password_hash"]):
        return err("LOGIN_FAILED", "用户名或密码错误", 401)

    return {
        "token": create_token(user),
        "username": user["username"],
        "role": user["role"],
        "store_id": user["store_id"],
        "store_name": user["store_name"],
    }


@app.get("/api/me")
def me(user: dict = Depends(get_current_user)):
    """当前用户信息（前端页面挂载时校验登录态用）。"""
    return {"user": user}


# ============================================================
# 问答接口（需登录）
# ============================================================

class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    question: str


@app.post("/chat")
async def chat(req: ChatRequest, user: dict = Depends(get_current_user)):
    """问答主接口：登录后可用；会话归属当前用户。"""
    # ---- 建流前参数校验 ----
    question = (req.question or "").strip()
    if not question:
        return err("PARAM_INVALID", "请求参数不完整：question 不能为空", 400)
    if len(question) > MAX_QUESTION_LEN:
        return err("QUESTION_TOO_LONG",
                   f"问题长度超过 {MAX_QUESTION_LEN} 字，请精简后重试", 400)

    # ---- 会话处理：无 session_id 则新建（记录归属用户/门店）----
    session_id = req.session_id
    with db.get_db() as conn:
        if session_id:
            row = conn.execute(
                "SELECT id FROM sessions WHERE id = ? AND user_id = ?",
                (session_id, user["uid"]),
            ).fetchone()
            if not row:
                return err("SESSION_NOT_FOUND", "会话不存在或已被删除", 404)
        else:
            session_id = uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO sessions (id, user_id, store_id, title, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (session_id, user["uid"], user.get("store_id"), question[:20],
                 db.now(), db.now()),
            )

    return StreamingResponse(
        event_generator(session_id, question, user),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============================================================
# 审计异常标记（批次 4）
# 说明：启发式规则，用于人工复核排序，不作为处罚依据
# ============================================================

R1_KEYWORDS = [
    "绕过", "低于起订", "低于最小", "拆零", "不按整箱", "超过上限", "超上限",
    "多要", "特批", "通融", "加急", "先发货", "私下", "走后门",
]


def detect_audit_flags(question: str, answer: str, hits):
    """对一问一答执行三项异常检测，返回标记列表（无异常返回空列表）。

    R1 疑似越权要求：问题命中绕过制度类关键词（拆零/低于起订/超上限…）
    R2 模糊回答：回答以「未找到/无法」兜底，却没有任何条文引用
    R3 低质量检索：top1 原始余弦得分 < 0.3（引用条文大概率与问题无关）
    """
    flags = []

    # ---- R1：问题关键词 ----
    hit_kw = [k for k in R1_KEYWORDS if k in question]
    if hit_kw:
        flags.append({"rule": "R1", "label": "疑似越权要求",
                      "detail": "命中关键词：" + "、".join(hit_kw)})

    # ---- R2：模糊兜底回答（无引用的「未找到/无法」式回答）----
    evasive = ("未找到" in answer) or ("无法" in answer) or ("无法回答" in answer)
    has_citation = ("制度条款" in answer) or ("引用来源" in answer)
    if evasive and not has_citation:
        flags.append({"rule": "R2", "label": "模糊回答",
                      "detail": "回答以「未找到/无法」兜底且未引用任何制度条文"})

    # ---- R3：低质量检索（top1 原始余弦分；注意不能用 min-max 融合分，
    #      融合分经过归一化后 top1 恒接近 1，无法区分好坏）----
    if hits and hits[0].get("vec_score", 1.0) < 0.3:
        flags.append({"rule": "R3", "label": "低质量检索",
                      "detail": f"top1 余弦得分 {hits[0]['vec_score']} < 0.3，引用条文可能与问题无关"})

    return flags


def event_generator(session_id: str, question: str, user: dict):
    """SSE 事件生成器：status → delta* → sources → done（或 error）。"""
    yield sse_event("status", {"message": "正在检索补货制度…"})

    if app.state.kb is None or app.state.kb.is_empty():
        yield sse_event("error", {"code": "KB_EMPTY",
                                  "message": "制度知识库为空，请联系管理员导入文档"})
        return

    # ---- 混合检索 top3 ----
    try:
        hits = app.state.kb.search(question)
    except Exception as e:
        yield sse_event("error", {"code": "INTERNAL_ERROR", "message": f"检索服务异常：{e}"})
        return

    # 保存用户消息 + 更新会话活跃时间
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, sources, audit, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (session_id, "user", question, None, None, db.now()),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?",
                     (db.now(), session_id))

    # ---- 最近 6 轮历史作为上下文（不含本轮）----
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id = ? "
            "AND role IN ('user','assistant') ORDER BY id DESC LIMIT ?",
            (session_id, HISTORY_ROUNDS * 2),
        ).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)][:-1]

    # ---- 组装消息：系统提示词实时读库（管理页改完即生效）----
    yield sse_event("status", {"message": "正在生成回答…"})
    system_prompt = db.get_active_prompt()
    messages = llm.build_messages(history, system_prompt,
                                  app.state.kb.build_prompt(question, hits))

    answer_parts = []
    try:
        for piece in llm.stream_chat(messages):
            answer_parts.append(piece)
            yield sse_event("delta", {"content": piece})
    except llm.LLMError as e:
        yield sse_event("error", {"code": e.code, "message": e.message})
        return
    except Exception as e:
        yield sse_event("error", {"code": "INTERNAL_ERROR", "message": f"系统内部错误：{e}"})
        return

    answer = "".join(answer_parts)

    # ---- 引用来源 ----
    sources = [
        {"doc": h["doc_title"], "section": h["section"],
         "snippet": h["chunk"][:80].replace("\n", " ") + "…", "score": h["score"]}
        for h in hits
    ]

    # ---- 审计异常标记（批次 4）：R1 越权 / R2 模糊 / R3 低质量检索 ----
    flags = detect_audit_flags(question, answer, hits)
    audit_json = json.dumps({"flags": flags}, ensure_ascii=False) if flags else None

    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, sources, audit, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (session_id, "assistant", answer,
             json.dumps(sources, ensure_ascii=False), audit_json, db.now()),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?",
                     (db.now(), session_id))

    yield sse_event("sources", {"sources": sources})
    yield sse_event("done", {"session_id": session_id})


# ============================================================
# 会话接口（行级权限：只操作本人的会话）
# ============================================================

@app.get("/sessions")
def list_sessions(user: dict = Depends(get_current_user)):
    """会话列表：仅返回当前用户的会话。"""
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT id, title, updated_at FROM sessions WHERE user_id = ? "
            "ORDER BY updated_at DESC LIMIT 50",
            (user["uid"],),
        ).fetchall()
    return {"sessions": [dict(r) for r in rows]}


@app.get("/sessions/{session_id}/messages")
def get_messages(session_id: str, user: dict = Depends(get_current_user)):
    """加载会话历史：校验归属。"""
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM sessions WHERE id = ? AND user_id = ?",
            (session_id, user["uid"]),
        ).fetchone()
        if not row:
            return err("SESSION_NOT_FOUND", "会话不存在或已被删除", 404)
        rows = conn.execute(
            "SELECT role, content, sources, created_at FROM messages "
            "WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    msgs = []
    for r in rows:
        m = {"role": r["role"], "content": r["content"], "created_at": r["created_at"]}
        if r["sources"]:
            m["sources"] = json.loads(r["sources"])
        msgs.append(m)
    return {"messages": msgs}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str, user: dict = Depends(get_current_user)):
    """删除会话：校验归属，级联删消息。"""
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM sessions WHERE id = ? AND user_id = ?",
            (session_id, user["uid"]),
        ).fetchone()
        if not row:
            return err("SESSION_NOT_FOUND", "会话不存在或已被删除", 404)
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return {"ok": True}


# ============================================================
# 管理后台：知识库管理（批次 2，仅管理员）
# ============================================================

@app.get("/admin/kb", response_class=HTMLResponse)
def admin_kb_page(request: Request, user: dict = Depends(get_current_user)):
    """知识库管理页（页面内 JS 再校验 admin 角色，未授权跳登录页）。"""
    return templates.TemplateResponse(request, "kb.html")


@app.post("/api/kb/documents")
async def upload_document(
    request: Request,
    user: dict = Depends(require_admin),
):
    """上传制度文档（multipart：file + doc_type）→ 解析切片向量化入库。"""
    try:
        form = await request.form()
        upload = form.get("file")
        doc_type = (form.get("doc_type") or "其他").strip()

        if upload is None or not getattr(upload, "filename", ""):
            return err("PARAM_INVALID", "请求参数不完整：请选择要上传的文件", 400)

        data = await upload.read()
        chunk_count = app.state.kb.add_document(
            upload.filename, data, doc_type, user["sub"])
        return {"ok": True, "chunk_count": chunk_count,
                "message": f"上传成功，已切片 {chunk_count} 段并入库"}
    except KBParseError as e:
        return err("KB_PARSE_FAILED", str(e), 400)
    except Exception as e:
        return err("INTERNAL_ERROR", f"上传处理异常：{e}", 500)


@app.get("/api/kb/documents")
def list_documents(user: dict = Depends(require_admin)):
    """文档列表（含切片数、上传人、时间）。"""
    return {"documents": app.state.kb.list_documents()}


@app.delete("/api/kb/documents/{doc_id}")
def delete_document(doc_id: int, user: dict = Depends(require_admin)):
    """删除文档及切片，重建索引。"""
    try:
        app.state.kb.delete_document(doc_id)
        return {"ok": True, "message": "文档已删除，索引已重建"}
    except KeyError:
        return err("DOC_NOT_FOUND", "文档不存在或已被删除", 404)


@app.post("/api/kb/seed")
def seed_kb(user: dict = Depends(require_admin)):
    """手动导入种子文档（补货制度.md）。"""
    if app.state.kb.seed_default(user["sub"]):
        return {"ok": True, "message": "种子文档已导入"}
    return err("SEED_FAILED", "种子文档不存在或导入失败（可能已导入过）", 400)


# ============================================================
# 管理后台：提示词模板管理（批次 3，仅管理员）
# ============================================================

class PromptRequest(BaseModel):
    name: str
    content: str


@app.get("/admin/prompts", response_class=HTMLResponse)
def admin_prompts_page(request: Request, user: dict = Depends(get_current_user)):
    """提示词模板管理页（页面内 JS 再校验 admin 角色）。"""
    return templates.TemplateResponse(request, "prompts.html")


@app.get("/api/prompts")
def list_prompts(user: dict = Depends(require_admin)):
    """模板列表（含生效标记），按更新时间倒序。"""
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, content, is_active, updated_by, updated_at "
            "FROM prompt_templates ORDER BY is_active DESC, updated_at DESC"
        ).fetchall()
    return {"prompts": [dict(r) for r in rows]}


@app.post("/api/prompts")
def create_prompt(req: PromptRequest, user: dict = Depends(require_admin)):
    """新建模板版本（存库，不覆盖旧版，可追溯）。"""
    name = (req.name or "").strip()
    content = (req.content or "").strip()
    if not content:
        return err("PARAM_INVALID", "模板内容不能为空", 400)
    if len(content) > 2000:
        return err("PROMPT_TOO_LONG", "模板内容超过 2000 字上限", 400)
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO prompt_templates (name, content, is_active, updated_by, updated_at) "
            "VALUES (?,?,0,?,?)",
            (name or f"模板 {db.now()}", content, user["sub"], db.now()),
        )
    return {"ok": True, "message": "模板已保存（未生效）"}


@app.put("/api/prompts/{prompt_id}/activate")
def activate_prompt(prompt_id: int, user: dict = Depends(require_admin)):
    """设为生效版本：先全部置 0 再置 1，保证全局唯一生效；问答实时读库立即生效。"""
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT id FROM prompt_templates WHERE id = ?", (prompt_id,)).fetchone()
        if not row:
            return err("PROMPT_NOT_FOUND", "模板不存在或已被删除", 404)
        conn.execute("UPDATE prompt_templates SET is_active = 0")
        conn.execute(
            "UPDATE prompt_templates SET is_active = 1, updated_by = ?, updated_at = ? "
            "WHERE id = ?",
            (user["sub"], db.now(), prompt_id),
        )
    return {"ok": True, "message": "模板已生效，问答立即使用新版本"}


# ============================================================
# 管理后台：对话审计（批次 4，仅管理员）
# ============================================================

@app.get("/admin/audit", response_class=HTMLResponse)
def admin_audit_page(request: Request, user: dict = Depends(get_current_user)):
    """对话审计页（页面内 JS 再校验 admin 角色）。"""
    return templates.TemplateResponse(request, "audit.html")


@app.get("/api/audit/meta")
def audit_meta(user: dict = Depends(require_admin)):
    """筛选项元数据：门店下拉 + 店长下拉。"""
    with db.get_db() as conn:
        stores = conn.execute(
            "SELECT DISTINCT store_id, store_name FROM users "
            "WHERE store_id IS NOT NULL ORDER BY store_id"
        ).fetchall()
        users = conn.execute(
            "SELECT id, username, store_name, role FROM users ORDER BY id"
        ).fetchall()
    return {
        "stores": [dict(r) for r in stores],
        "users": [dict(r) for r in users],
    }


@app.get("/api/audit")
def audit_records(
    store_id: Optional[str] = None,
    user_id: Optional[int] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    flagged_only: bool = False,
    user: dict = Depends(require_admin),
):
    """问答审计记录：按门店/店长/时间筛选，可只看异常。

    每条记录 = 一次问答对（assistant 消息 + 其同会话上一条 user 消息）。
    """
    sql = (
        "SELECT a.id AS message_id, a.session_id, a.content AS answer, "
        "       a.sources, a.audit, a.created_at, "
        "       s.user_id, u.username, u.store_name, "
        "       (SELECT q.content FROM messages q "
        "         WHERE q.session_id = a.session_id AND q.role = 'user' "
        "           AND q.id < a.id ORDER BY q.id DESC LIMIT 1) AS question "
        "FROM messages a "
        "JOIN sessions s ON a.session_id = s.id "
        "JOIN users u ON s.user_id = u.id "
        "WHERE a.role = 'assistant'"
    )
    params = []
    if store_id:
        sql += " AND u.store_id = ?"
        params.append(store_id)
    if user_id:
        sql += " AND s.user_id = ?"
        params.append(user_id)
    if start:
        sql += " AND a.created_at >= ?"
        params.append(start)
    if end:
        sql += " AND a.created_at <= ?"     # 日期闭区间：补齐当天末尾
        params.append(end + "T23:59:59" if len(end) == 10 else end)
    if flagged_only:
        sql += " AND a.audit IS NOT NULL"
    sql += " ORDER BY a.id DESC LIMIT 200"

    with db.get_db() as conn:
        rows = conn.execute(sql, params).fetchall()

    records = []
    for r in rows:
        rec = dict(r)
        rec["flags"] = json.loads(r["audit"])["flags"] if r["audit"] else []
        rec["sources"] = json.loads(r["sources"]) if r["sources"] else []
        # 脱敏：审计列表不返回全文，详情前端按需展示（记录里保留全文，量小）
        records.append(rec)
    return {"records": records}
