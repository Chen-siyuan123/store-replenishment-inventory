#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
执行循环 Agent Harness（agent/harness.py）
================================================================
一句话：**把「模型想做什么」和「系统允许做什么」分开**。

循环体（最多 MAX_ROUNDS = 5 轮）：
    ┌─────────────────────────────────────────────────┐
    │ 第 N 轮                                          │
    │  1. 模型决策  llm.chat_with_tools(messages)      │
    │  2. 若返回 tool_calls → 逐个执行                 │
    │  3. 结果回填 messages（role=tool）               │
    │  4. 回到 1，直到模型给出最终回答或用完 5 轮       │
    └─────────────────────────────────────────────────┘

三处关键设计：

① **轮次是硬预算，不是建议**
   5 轮封顶，耗尽后强制收尾。目的是把「模型无限自我纠错」的成本封死；
   同时逼提示词把互不依赖的只读查询合并到同一轮（一轮可并行多个 tool_call）。

② **写操作在「执行前」中断，而不是执行后补确认**
   create_replenishment_draft 属于 TOOL_RISK=write。框架遇到它时：
     预览（干跑）→ 落 agent_actions → SSE 推确认卡 → **结束本次流**
   店长点确认后，走 /api/agent/confirm 开一条新的流，从断点继续执行。
   为什么不用阻塞等待：SSE 连接会被网关 60s 掐断，用户也可能关页面走开；
   落库 + 断点恢复才能让「确认」这件事真正跨请求存活。

③ **每一步都写日志表**
   模型决策一行（tool=__model__，含本轮决策耗时与"决定调用哪些工具"），
   每个工具一行（工具名、入参、耗时、结果摘要、是否阻断）。
   日志不是给开发看的 debug 输出，而是后续做轨迹质量分析的**数据源**
   （见 store.trace_quality）。

本模块不依赖 FastAPI，事件以 (event_name, payload) 元组产出，
由 server.py 转成 SSE；离线回放（agent/scenarios.py）也可以直接消费。
"""

import json
import time
import uuid

import db
import llm
import tools
from agent import incidents as incident_rules
from agent import store

# 单次提问的最大决策轮数（产品约束：5 轮）
MAX_ROUNDS = 5

# 最终回答按字符切片推给前端，做打字机效果（无需二次调用模型）
ANSWER_CHUNK = 6


def _chunk_text(text: str, size: int = ANSWER_CHUNK):
    for i in range(0, len(text), size):
        yield text[i:i + size]


def _parse_args(raw):
    """模型给的 arguments 是 JSON 字符串，解析失败时按空参数处理（工具会报参数错误）。"""
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw or "{}")
        return val if isinstance(val, dict) else {}
    except (TypeError, ValueError):
        return {}


class Harness:
    """一次问答的执行上下文 + 循环控制器。"""

    def __init__(self, *, session_id: str, store_id: str, user: dict, username: str,
                 kb, system_prompt: str, history: list = None, question: str = "",
                 llm_client=None, max_rounds: int = MAX_ROUNDS):
        # ---- 运行标识 ----
        self.run_id = uuid.uuid4().hex[:12]
        self.max_rounds = int(max_rounds or MAX_ROUNDS)

        # ---- 身份与数据上下文（工具层通过 ctx 取门店/知识库/会话状态）----
        self.session_id = session_id
        self.store_id = store_id
        self.user = user or {}
        self.username = username or ""
        self.kb = kb
        self.system_prompt = system_prompt
        self.history = history or []
        self.question = question
        self.llm_client = llm_client or llm      # 可注入：离线回放用假模型

        # ---- 运行状态（server 层据此落消息、算审计标记）----
        self.status = "running"
        self.answer = ""
        self.round_used = 0
        self.tool_trace = []      # 与 messages.tool_trace 同构，供审计 R4 使用
        self.drafts = []          # 本次创建成功的草稿（SSE draft 事件）
        self.sources = []         # 引用来源

        # ---- 消息链：system + 历史 + 本轮问题 ----
        self.messages = [{"role": "system", "content": system_prompt}]
        self.messages.extend(self.history)
        if question:
            self.messages.append({"role": "user", "content": question})

        # ---- 工具执行上下文 ----
        # last_hits 由 search_policy_docs 写入，用于生成引用卡片
        # last_rejected_qty 由 validate_replenishment 写入，用于确认卡展示「原申请 → 调整后」
        # requested_qty 由本处解析店长原话得到：工具层据此防止「建议量覆盖客户明确要求」
        self.ctx = {
            "session_id": session_id,
            "store_id": store_id,
            "user": self.user,
            "username": self.username,
            "kb": kb,
            "last_hits": [],
            "question": question,
            "requested_qty": tools.extract_requested_qty(question),
        }

    # ========================================================
    # 对外入口一：首次运行
    # ========================================================

    def run(self):
        """产生事件流。事件类型与 SSE 事件名一一对应：

        status / tool / confirm_required / draft / delta / error
        """
        store.create_run(self.run_id, self.session_id, self.user.get("uid"),
                         self.store_id, self.question, self.max_rounds, self.messages)

        try:
            yield from self._loop(start_round=1)
        except llm.LLMError as e:
            if e.code == "TOOLS_UNSUPPORTED":
                # 模型不支持函数调用 → 降级为「检索 + 生成」的传统 RAG
                try:
                    yield from self._fallback_rag()
                except llm.LLMError as e2:
                    self.status = "failed"
                    yield ("error", {"code": e2.code, "message": e2.message})
            else:
                self.status = "failed"
                yield ("error", {"code": e.code, "message": e.message})
        except Exception as e:                       # 兜底：任何异常都不让流裸断
            self.status = "failed"
            yield ("error", {"code": "AGENT_ERROR", "message": f"智能体执行异常：{e}"})

        # 轮次用尽：给一个明确的收尾，而不是空回答
        if self.status == "running":
            self.status = "rounds_exhausted"
            self.answer = (
                f"这次核对步骤较多，已达到单次 {self.max_rounds} 轮上限。"
                "请把问题拆小一点（例如先确认某个商品的库存与制度参数），我再继续。"
            )
            for piece in _chunk_text(self.answer):
                yield ("delta", {"content": piece})

        self._persist()
        self.sources = self._collect_sources()

    # ========================================================
    # 对外入口二：人工确认后从断点恢复
    # ========================================================

    @classmethod
    def from_run(cls, run: dict, *, kb, user: dict = None, username: str = "",
                 llm_client=None):
        """用数据库里的运行快照重建一个 Harness（不新建 run）。"""
        h = cls(session_id=run["session_id"], store_id=run["store_id"] or "S001",
                user=user or {}, username=username, kb=kb,
                system_prompt="", history=[], question="",
                llm_client=llm_client, max_rounds=run.get("max_rounds") or MAX_ROUNDS)
        h.run_id = run["id"]
        h.messages = run.get("messages") or []
        h.round_used = int(run.get("round_used") or 0)
        h.ctx["last_hits"] = []

        # 恢复执行时 question 是空的，但「店长原话里的数量」必须还原：
        # 真正的建单发生在确认之后的 resume 里，若 requested_qty 丢了，
        # create 落库的 qty_requested 会退化成实际申请量（240 → 120），
        # 「原要求 20 箱、因上限压到 10 箱」这个事实就从数据里消失了。
        try:
            with db.get_db() as conn:
                row = conn.execute(
                    "SELECT content FROM messages WHERE session_id = ? AND role = 'user' "
                    "ORDER BY id DESC LIMIT 1", (run["session_id"],)).fetchone()
            if row:
                h.ctx["question"] = row["content"]
                h.ctx["requested_qty"] = tools.extract_requested_qty(row["content"])
        except Exception:
            pass
        return h

    def resume(self, decision: str, operator: str):
        """执行挂起的写操作（confirmed）或放弃（cancelled），然后收尾。

        注意：这里**不再进入下一轮模型决策**——建草稿是链路终点，
        让它继续调工具只会制造重复建单的风险。
        """
        run = store.get_run(self.run_id)
        if not run:
            yield ("error", {"code": "RUN_NOT_FOUND", "message": "执行记录不存在"})
            return
        action = store.get_action(run.get("pending_action_id"))
        if not action or action["status"] != "pending":
            yield ("error", {"code": "ACTION_NOT_PENDING",
                             "message": "该确认已处理或已失效，请重新发起补货申请"})
            return

        args = action.get("args") or {}
        tool_call_id = action.get("tool_call_id") or action["tool"]

        # ---------------- 分支一：店长取消 ----------------
        if decision != "confirm":
            result = {"ok": False, "blocking": True, "code": "USER_CANCELLED",
                      "message": "店长已取消本次补货申请，未创建任何草稿"}
            store.decide_action(action["id"], "cancelled", operator)
            store.log_step(self.run_id, self.round_used, 90,
                           tool=action["tool"], decision="cancelled", args=args,
                           ok=False, blocking=True, code="USER_CANCELLED",
                           duration_ms=0, result_summary="店长在确认卡上点了取消")
            self.messages.append({"role": "tool", "tool_call_id": tool_call_id,
                                  "name": action["tool"],
                                  "content": json.dumps(result, ensure_ascii=False)})
            self.status = "cancelled"
            self.answer = ("好的，本次补货申请已取消，没有创建任何草稿。"
                           "如果还需要补货，告诉我商品和数量，我重新走一遍核对。")
            for piece in _chunk_text(self.answer):
                yield ("delta", {"content": piece})
            self._persist()
            return

        # ---------------- 分支二：店长确认 → 真正执行写操作 ----------------
        yield ("status", {"message": "已确认，正在创建补货草稿…"})
        t0 = time.perf_counter()
        result = tools.call_tool(action["tool"], args, self.ctx)
        elapsed = int((time.perf_counter() - t0) * 1000)

        store.decide_action(action["id"], "confirmed", operator)
        store.log_step(self.run_id, self.round_used, 91,
                       tool=action["tool"], decision="confirmed", args=args,
                       ok=result.get("ok", False),
                       blocking=result.get("blocking", False),
                       code=result.get("code"), duration_ms=elapsed,
                       result_summary=result.get("message", ""))

        self.tool_trace.append({
            "tool": action["tool"], "label": tools.TOOL_LABELS.get(action["tool"], action["tool"]),
            "args": args, "ok": result.get("ok", False),
            "blocking": result.get("blocking", False), "code": result.get("code"),
            "message": result.get("message", ""), "at": db.now(),
        })
        self.messages.append({"role": "tool", "tool_call_id": tool_call_id,
                              "name": action["tool"],
                              "content": json.dumps(result, ensure_ascii=False)})

        if result.get("draft"):
            self.drafts.append(result["draft"])
            yield ("draft", {"draft": result["draft"]})

        if result.get("ok"):
            self.status = "done"
            self.answer = self._summarize_after_draft(result.get("draft") or {})
        else:
            # 令牌过期/账实未确认等：如实告知，不假装成功
            self.status = "failed"
            self.answer = f"补货草稿创建失败：{result.get('message', '未知原因')}。请重新核对后再试。"
            # 建单失败若属于停止条件（账实未确认/促销版本缺失），同样要有人接手
            if result.get("blocking"):
                self._record_incident(tool=action["tool"], code=result.get("code"),
                                      reason=result.get("blocking_reason") or result.get("message"),
                                      args=args)
        for piece in _chunk_text(self.answer):
            yield ("delta", {"content": piece})

        self._persist()
        self.sources = self._collect_sources()

    # ========================================================
    # 核心循环
    # ========================================================

    def _loop(self, start_round: int):
        """模型 ↔ 工具 循环。每轮固定三步：决策 → 执行 → 回填。"""
        for round_no in range(start_round, self.max_rounds + 1):
            self.round_used = round_no
            # 落盘一次快照：即使进程被杀，恢复时也知道走到第几轮、消息链长什么样
            store.update_run(self.run_id, status="running",
                             round_used=round_no, messages=self.messages)

            yield ("status", {"message": ("正在理解您的问题…" if round_no == 1
                                          else f"第 {round_no}/{self.max_rounds} 轮：正在核对…")})

            # ---------- ① 模型决策 ----------
            t0 = time.perf_counter()
            resp = self.llm_client.chat_with_tools(self.messages, tools.TOOLS_SPEC)
            decision_ms = int((time.perf_counter() - t0) * 1000)
            calls = resp.get("tool_calls") or []

            # 情况 A：模型不再调工具 → 最终回答，循环结束
            if not calls:
                self.answer = (resp.get("content") or "").strip()
                store.log_step(self.run_id, round_no, 0,
                               tool=store.MODEL_STEP, decision="final_answer",
                               duration_ms=decision_ms,
                               result_summary=f"第 {round_no} 轮：不再调用工具，直接回答（{len(self.answer)} 字）")
                self.status = "done"
                for piece in _chunk_text(self.answer):
                    yield ("delta", {"content": piece})
                return

            # 情况 B：模型要调工具 → 先把「本轮决策」记一行日志
            names = [((c.get("function") or {}).get("name") or "?") for c in calls]
            store.log_step(self.run_id, round_no, 0,
                           tool=store.MODEL_STEP, decision="plan",
                           duration_ms=decision_ms,
                           result_summary=f"第 {round_no} 轮：决定调用 {len(calls)} 个工具（{'、'.join(names)}）")

            # OpenAI 协议要求：assistant 的 tool_calls 消息必须先入链，再逐条回填 tool 结果
            assistant_msg = {"role": "assistant", "content": resp.get("content") or "",
                             "tool_calls": calls}
            self.messages.append(assistant_msg)

            # ---------- ② 执行工具 ----------
            for idx, tc in enumerate(calls, start=1):
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                args = _parse_args(fn.get("arguments"))
                label = tools.TOOL_LABELS.get(name, name)

                # === 写操作拦截：执行前中断 ===
                if name in tools.HITL_TOOLS:
                    # 先「干跑」出草稿内容。预览不通过 → 当作普通工具失败回填，让模型自己改
                    t_prev = time.perf_counter()
                    preview_res = tools.call_tool("preview_replenishment_draft", args, self.ctx)
                    preview_ms = int((time.perf_counter() - t_prev) * 1000)
                    if not preview_res.get("ok"):
                        yield ("tool", {"name": name, "label": label, "state": "running"})
                        store.log_step(self.run_id, round_no, idx, tool=name,
                                       decision="tool_call", args=args, ok=False,
                                       blocking=False, code=preview_res.get("code"),
                                       duration_ms=preview_ms,
                                       result_summary=preview_res.get("message", ""))
                        yield ("tool", {"name": name, "label": label, "state": "done",
                                        "ok": False, "blocking": False,
                                        "code": preview_res.get("code"),
                                        "summary": preview_res.get("message", "")})
                        self.messages.append({"role": "tool",
                                              "tool_call_id": tc.get("id") or name,
                                              "name": name,
                                              "content": json.dumps(preview_res, ensure_ascii=False)})
                        continue

                    # 预览通过 → 挂起并结束本次流（不执行写操作）
                    preview = preview_res.get("data") or {}
                    action = store.create_action(
                        run_id=self.run_id, session_id=self.session_id, tool=name,
                        args=args, tool_call_id=tc.get("id") or name, preview=preview)

                    # 只保留到当前这条调用，其余同批调用作废
                    # （避免出现「有 tool_calls 却没有对应 tool 结果」的非法消息链）
                    assistant_msg["tool_calls"] = calls[:idx]
                    for j, dropped in enumerate(calls[idx:], start=idx + 1):
                        store.log_step(self.run_id, round_no, j,
                                       tool=((dropped.get("function") or {}).get("name") or "?"),
                                       decision="skipped", args={}, ok=False, blocking=False,
                                       code="SKIPPED_AFTER_INTERRUPT",
                                       duration_ms=0,
                                       result_summary="因人工确认中断，本轮后续调用未执行")
                    store.log_step(self.run_id, round_no, idx, tool=name,
                                   decision="interrupt", args=args, ok=True, blocking=False,
                                   duration_ms=preview_ms,
                                   result_summary="已暂停：补货申请预览已推送店长，等待人工确认")
                    self.status = "waiting_confirm"
                    store.update_run(self.run_id, status="waiting_confirm",
                                     pending_action_id=action["id"],
                                     round_used=round_no, messages=self.messages)
                    yield ("confirm_required", {
                        "run_id": self.run_id,
                        "action_id": action["id"],
                        "tool": name,
                        "round": round_no,
                        "preview": preview,
                    })
                    return

                # === 只读 / 校验类工具：直接执行 ===
                yield ("tool", {"name": name, "label": label, "state": "running"})
                t_tool = time.perf_counter()
                result = tools.call_tool(name, args, self.ctx)
                elapsed = int((time.perf_counter() - t_tool) * 1000)

                # ③ 记录轨迹（这一行就是「工具名 / 参数 / 耗时 / 结果摘要」）
                store.log_step(self.run_id, round_no, idx, tool=name,
                               decision="tool_call", args=args,
                               ok=result.get("ok", False),
                               blocking=result.get("blocking", False),
                               code=result.get("code"), duration_ms=elapsed,
                               result_summary=result.get("message", ""))

                # ③-b 命中业务停止条件 → 立刻开一张转交单（f44：原因/接手人/时限）
                #     模型说「请你联系计划员」只是措辞，这张单才是可追踪的事实。
                incident = None
                if result.get("blocking"):
                    incident = self._record_incident(
                        tool=name, code=result.get("code"),
                        reason=result.get("blocking_reason") or result.get("message"),
                        args=args)

                self.tool_trace.append({
                    "tool": name, "label": label, "args": args,
                    "ok": result.get("ok", False),
                    "blocking": result.get("blocking", False),
                    "code": result.get("code"),
                    "message": result.get("message", ""),
                    "at": db.now(),
                })

                yield ("tool", {"name": name, "label": label, "state": "done",
                                "ok": result.get("ok", False),
                                "blocking": result.get("blocking", False),
                                "code": result.get("code"),
                                "summary": result.get("message", ""),
                                # 前端据此在轨迹行下方挂一行「已转交 X，某时前处理」
                                "incident": incident})

                # ④ 结果回填：content 必须是字符串
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id") or name,
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False),
                })
            # 本轮结束，进入下一轮重新决策

    # ========================================================
    # 收尾 / 降级 / 工具方法
    # ========================================================

    def _summarize_after_draft(self, draft: dict) -> str:
        """草稿创建成功后，用一次不带 tools 的纯文本调用生成总结。

        为什么强制收尾而不是继续循环：
          写操作已完成，再让模型决策只会带来「重复建单」的风险；
          这里只需要它把依据讲清楚，不需要它再做任何动作。

        为什么要把事实逐条写进提示、并校验模型输出：
          真实故障——店长要求「20 箱」，受分配上限压到 10 箱后，
          模型从对话历史里捡回「20 箱」写进总结，与落库的 10 箱不一致。
          建单信息不能由模型措辞决定，所以：给定事实 → 生成 → 校验数量 → 不符即用模板。
        """
        basis = draft.get("basis") or {}
        inv = basis.get("inventory") or {}
        sale = basis.get("sales") or {}
        unit = draft.get("unit") or "件"
        qty = draft.get("qty")
        adj = draft.get("adjust") or {}

        adj_text = ""
        if adj:
            adj_text = (f"（原要求 {adj.get('from_qty')} {unit}／{adj.get('from_boxes')} 箱，"
                        f"{adj.get('reason')}）")

        fallback = (
            f"已生成补货申请草稿 #{draft.get('id')}："
            f"{draft.get('sku_name', '')} {qty} {unit}"
            f"（{draft.get('box_text', '')}）{adj_text}，"
            f"配送日 {draft.get('delivery_date', '')}。"
            f"依据：可售库存 {inv.get('sellable_qty', '-')}，"
            f"近 {sale.get('days', 7)} 天日均销量 {sale.get('avg_daily', '-')}。"
            "请在下方卡片点「确认提交」后才会进入审批。"
        )

        facts = (
            f"- 商品：{draft.get('sku_name')}（{draft.get('sku')}）\n"
            f"- 申请数量：{qty} {unit}（{draft.get('boxes')} 箱，{draft.get('box_text')}）\n"
            f"- 预计配送日：{draft.get('delivery_date')}\n"
        )
        if adj:
            facts += f"- 数量调整：{adj.get('reason')}（原要求 {adj.get('from_qty')} {unit}）\n"

        prompt = (
            "（系统提示）补货草稿已创建并写入系统。以下是确定事实，数字必须逐字照用：\n"
            + facts +
            "请用简洁中文向店长说明上述内容与关键依据（可售库存/日均销量/制度参数），"
            "最后提醒需要点「确认提交」才会进入审批。"
            f"数量只能写成 {qty} {unit}（{draft.get('boxes')} 箱），"
            "禁止使用对话历史里店长提过的原始数量，禁止声称已提交，不要再调用工具。"
        )
        try:
            text = (self.llm_client.chat_text(
                self.messages + [{"role": "user", "content": prompt}]) or "").strip()
        except Exception:
            return fallback

        # 数量是落库事实：模型说错就丢弃它的措辞，用模板（模板里的数字一定来自 draft）
        if not text or str(qty) not in text:
            return fallback
        return text

    def _fallback_rag(self):
        """模型不支持函数调用时的降级路径：单轮检索 + 流式生成。"""
        yield ("status", {"message": "当前模型不支持工具调用，改为制度检索问答…"})
        if self.kb is None or self.kb.is_empty():
            self.status = "failed"
            yield ("error", {"code": "KB_EMPTY", "message": "制度知识库为空，请联系管理员导入文档"})
            return
        hits = self.kb.search(self.question)
        self.ctx["last_hits"] = hits
        messages = llm.build_messages(self.history,
                                      self.kb.build_prompt(self.question, hits),
                                      self.system_prompt)
        parts = []
        for piece in self.llm_client.stream_chat(messages):
            parts.append(piece)
            yield ("delta", {"content": piece})
        self.answer = "".join(parts)
        self.status = "done"

    def _collect_sources(self) -> list:
        """把检索命中转成前端引用卡片需要的结构。"""
        return [
            {"doc": h["doc_title"], "section": h["section"],
             "snippet": h["chunk"][:80].replace("\n", " ") + "…",
             "score": h["score"]}
            for h in (self.ctx.get("last_hits") or [])
        ]

    def _record_incident(self, *, tool: str, code: str, reason: str, args: dict = None):
        """把一次停止条件落成转交单（原因 + 接手人 + 时限）。

        刻意吞掉异常：转交单是「附加的治理动作」，
        它失败了也不能影响主链路把「为什么停下」告诉店长。
        """
        try:
            return incident_rules.record(
                run_id=self.run_id, session_id=self.session_id, store_id=self.store_id,
                tool=tool, code=(code or "BLOCKING"), reason=reason or "",
                sku=(args or {}).get("sku") or "",
            )
        except Exception:
            return None

    def _persist(self):
        """把终态写回 agent_runs（messages 快照也一并更新）。"""
        store.update_run(self.run_id, status=self.status, round_used=self.round_used,
                         messages=self.messages, answer=self.answer)
