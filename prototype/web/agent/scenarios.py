#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三条典型轨迹的离线回放（agent/scenarios.py）
================================================================
作用有两个：

1. **验收**：不消耗任何模型额度，把「模型决策 → 工具执行 → 结果回填 → 再决策」
   跑通一遍，轨迹真实写入 agent_runs / agent_steps，可在管理后台轨迹页查看。
2. **基准**：这三条脚本就是「预期轨迹」。以后换模型、改提示词、改工具，
   跑一遍对比轮数/工具序列/阻断点有没有跑偏，就是最朴素的回归测试。

实现方式：
  用一个 ScriptedLLM 冒充模型，按轮次返回预设的 tool_calls；
  工具层、门禁、中断确认、日志全部走**真实代码**，没有 mock。
  RAG 检索用 _OfflineKB（按关键词重合度打分），因为真实检索需要调 embedding API。

运行：
    cd prototype/web
    python -m agent.scenarios          # 跑全部三条
    python -m agent.scenarios b        # 只跑场景 b
"""

import json
import os
import sys
import uuid
from datetime import date, timedelta

import db
import tools
from agent import store
from agent.harness import Harness

KB_MD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "..", "data", "补货制度.md")


# ============================================================
# 离线知识库：接口与 kbstore.KBStore 对齐（is_empty / search / build_prompt）
# ============================================================

class _OfflineKB:
    """不调 embedding 的极简检索：按关键词重合度排序。

    只用于离线回放；线上仍走 kbstore 的 BM25 + 向量混合检索。
    """

    def __init__(self, path: str = KB_MD):
        self.docs = []
        path = os.path.abspath(path)
        if os.path.exists(path):
            text = open(path, encoding="utf-8").read()
            title = "补货制度"
            section, buf = "概述", []
            for line in text.split("\n"):
                if line.startswith("# "):
                    title = line[2:].strip()
                elif line.startswith("## "):
                    if buf:
                        self.docs.append((title, section, "\n".join(buf).strip()))
                    section, buf = line[3:].strip(), []
                else:
                    buf.append(line)
            if buf:
                self.docs.append((title, section, "\n".join(buf).strip()))

    def is_empty(self) -> bool:
        return not self.docs

    def search(self, query: str, top_k: int = 3):
        q = set(query.replace(" ", ""))
        ranked = []
        for i, (title, section, chunk) in enumerate(self.docs):
            if not chunk:
                continue
            overlap = len(q & set(section + chunk))
            score = overlap / (len(q) or 1)
            ranked.append({"rank": i, "doc_title": title, "section": section,
                           "chunk": chunk, "score": round(score, 3),
                           "vec_score": round(score, 3)})
        ranked.sort(key=lambda h: h["score"], reverse=True)
        hits = ranked[:top_k]
        for i, h in enumerate(hits, start=1):
            h["rank"] = i
        return hits

    def build_prompt(self, question: str, hits) -> str:
        ctx = "\n\n".join(f"《{h['doc_title']}》·{h['section']}\n{h['chunk']}" for h in hits)
        return f"制度条款：\n{ctx}\n\n问题：{question}"


# ============================================================
# 脚本化模型：按轮次返回预设决策
# ============================================================

class ScriptedLLM:
    """假模型：让执行循环在没有 API Key 的情况下也能跑完整流程。

    script 每一轮一个元素，支持两种写法：
      {"calls": [("工具名", {参数}), ...]}       → 模拟模型发起工具调用
      {"content": "最终回答文本"}                → 模拟模型给出最终回答
    也支持传函数：fn(messages) -> 上述 dict（用于「需要读取上一轮工具返回值」的场景，
    例如建草稿要用 validate_replenishment 刚签发的令牌）。
    """

    LLMError = Exception

    def __init__(self, script, final_text: str = ""):
        self.script = list(script)
        self.i = 0
        self.final_text = final_text

    def chat_with_tools(self, messages, tools_spec=None, **_kwargs):
        if self.i >= len(self.script):
            return {"content": "（脚本已结束）", "tool_calls": [], "finish_reason": "stop"}
        step = self.script[self.i]
        self.i += 1
        if callable(step):
            step = step(messages)
        if "content" in step:
            return {"content": step["content"], "tool_calls": [], "finish_reason": "stop"}
        calls = []
        for n, (name, args) in enumerate(step.get("calls", []), start=1):
            calls.append({
                "id": f"call_{self.i}_{n}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            })
        return {"content": "", "tool_calls": calls, "finish_reason": "tool_calls"}

    def chat_text(self, messages, **_kwargs):
        return self.final_text or "（离线回放：此处由真实模型生成收尾总结）"

    def stream_chat(self, messages, **_kwargs):
        yield self.final_text or "（离线回放）"


# ============================================================
# 工具函数：日期 / 从消息链里取令牌
# ============================================================

def _next_delivery(today: date = None) -> str:
    """按种子配送日历（周一/三/五）算下一个可发日。"""
    today = today or date.today()
    for i in range(14):
        d = today + timedelta(days=i)
        if d.weekday() in (0, 2, 4):
            return str(d)
    return str(today)


def _last_tool_result(messages):
    for m in reversed(messages):
        if m.get("role") == "tool":
            try:
                return json.loads(m.get("content") or "{}")
            except (TypeError, ValueError):
                return {}
    return {}


# ============================================================
# 三条预期轨迹
# ============================================================

def _scenario_a():
    """a.「整箱规格和最小起订量是多少」——只查制度，不碰校验和建单。

    预期：1 轮工具（RAG 检索） + 1 轮回答，共 2 轮。
    关键断言：tool_trace 里不能出现 validate_replenishment / create_replenishment_draft。
    """
    return {
        "title": "a. 制度问答（只走 RAG）",
        "question": "整箱规格和最小起订量是多少",
        "script": [
            {"calls": [("search_policy_docs",
                        {"query": "整箱规格 最小起订量 MOQ", "top_k": 3})]},
            {"content": ("按《补货制度》：① 最小起订量以商品档案为准，日用百货类 MOQ 为 12 件、"
                         "食品饮料类为 24 件；② 申请量必须按整箱规格取整，箱规以商品档案为准"
                         "（如矿泉水 24 瓶/箱）；③ 换算公式：申请箱数 = 向上取整(建议件数 ÷ 箱规)。\n"
                         "引用来源：《补货制度》·「一、最小起订量（MOQ）」「二、整箱规格」。")},
        ],
        "final_text": "",
        "expect": {"status": "done", "max_rounds": 2,
                   "forbidden_tools": ["validate_replenishment", "create_replenishment_draft"],
                   "must_tools": ["search_policy_docs"]},
    }


def _scenario_b():
    """b.「帮我给门店申请补 20 箱牛奶」——完整补货链路 + 写操作中断确认。

    关键点：20 箱 × 12 盒/箱 = 240 盒，超过分配上限 120 盒，
    校验不通过 → 按整箱压到上限内（10 箱 = 120 盒）→ 再校验通过 → 建草稿前中断。

    预期 5 轮（正好用满预算）：
      1 轮：查编码 + 库存 + 促销 + 销量（4 个只读工具并行）
      2 轮：查制度参数 + 配送日历
      3 轮：validate(240) → 超上限，被拒
      4 轮：validate(120) → 通过，签发令牌
      5 轮：create_replenishment_draft → **中断，等店长确认**
    确认后：真正写入草稿 → 生成收尾总结。
    """
    next_day = _next_delivery()

    def draft_call(messages):
        # 令牌是上一轮工具刚签发的，脚本从消息链里取（真实模型也从工具返回里取）
        token = _last_tool_result(messages).get("validation_token", "")
        return {"calls": [("create_replenishment_draft",
                           {"sku": "A002", "qty": 120, "delivery_date": next_day,
                            "validation_token": token})]}

    return {
        "title": "b. 补货申请（完整链路 + 中断确认）",
        "question": "帮我给门店申请补 20 箱牛奶",
        "script": [
            {"calls": [
                ("search_sku", {"keyword": "牛奶"}),
                ("get_sellable_inventory", {"sku": "A002"}),
                ("check_promotion_status", {"sku": "A002"}),
                ("get_sales_context", {"sku": "A002", "days": 7}),
            ]},
            {"calls": [
                ("get_replenishment_policy", {"sku": "A002"}),
                ("get_delivery_schedule", {"delivery_date": next_day}),
            ]},
            # 店长说 20 箱，模型先按整箱换算成 240 盒提交校验
            {"calls": [("validate_replenishment",
                        {"sku": "A002", "qty": 240, "delivery_date": next_day})]},
            # 超上限被拒 → 按返回建议调整为 120 盒（10 箱）重新校验
            {"calls": [("validate_replenishment",
                        {"sku": "A002", "qty": 120, "delivery_date": next_day})]},
            draft_call,
        ],
        "final_text": ("已生成补货申请草稿：鲜牛奶 1L，120 盒（10 箱 × 12 盒/箱），"
                       f"预计配送日 {next_day}。依据：可售库存 12 盒、近 7 天日均约 5 盒，"
                       "原申请的 20 箱（240 盒）超过单店分配上限 120 盒，已按整箱压到上限内。"
                       "请在下方卡片点「确认提交」，提交后即记录在案。"),
        "expect": {"status": "done", "max_rounds": 5,
                   "must_tools": ["search_sku", "get_sellable_inventory", "get_replenishment_policy",
                                  "validate_replenishment", "create_replenishment_draft"],
                   "interrupt_tool": "create_replenishment_draft"},
        # 回放时自动替店长点「确认」，把第二段流也跑出来
        "confirm": True,
    }


def _scenario_c():
    """c.「这批 30 箱今天能发吗」——查配送日历与线路容量，只给结论，不建单。

    预期 2 轮：
      1 轮：search_sku（确认箱规）+ get_delivery_schedule（今天是否可发、余量）
      2 轮：结论（今天不可发 + 最近可发日 + 容量不足提示）
    关键断言：不出现 validate / create 工具。
    """
    today = str(date.today())
    next_day = _next_delivery()
    weekday = "周" + "一二三四五六日"[date.today().weekday()]

    return {
        "title": "c. 配送可行性（只查日历，不建单）",
        "question": "这批 30 箱牛奶今天能发吗",
        "script": [
            {"calls": [
                ("search_sku", {"keyword": "牛奶"}),
                ("get_delivery_schedule", {"delivery_date": today}),
            ]},
            {"content": (f"今天（{today}，{weekday}）不是配送日，线路为周一/三/五发车，不能今天发。"
                         f"最近可发日是 {next_day}。另外 30 箱牛奶 = 360 盒，"
                         "当前线路余量只有 180 件，容量不够，需要拆成两批发（或改下周一）。"
                         "如果是紧急缺货，建议先按 15 箱（180 盒）报，剩余部分下个配送日再补。")},
        ],
        "final_text": "",
        "expect": {"status": "done", "max_rounds": 2,
                   "forbidden_tools": ["validate_replenishment", "create_replenishment_draft"],
                   "must_tools": ["get_delivery_schedule"]},
    }


SCENARIOS = {"a": _scenario_a, "b": _scenario_b, "c": _scenario_c}


# ============================================================
# 回放执行器
# ============================================================

DEMO_USER = {"uid": 2, "sub": "store01", "role": "store_manager",
             "store_id": "S001", "store_name": "朝阳路店"}


def run_scenario(key: str, verbose: bool = True, auto_confirm: bool = True) -> dict:
    """跑一条场景，返回 {run_id, status, steps, events}。"""
    spec = SCENARIOS[key]()
    session_id = f"demo-{key}-{uuid.uuid4().hex[:6]}"
    tools.reset_session(session_id)          # 清掉内存里的会话状态（步骤/令牌）

    h = Harness(
        session_id=session_id,
        store_id="S001",
        user=DEMO_USER,
        username="store01",
        kb=_OfflineKB(),
        system_prompt="（离线回放，系统提示词不参与决策）",
        history=[],
        question=spec["question"],
        llm_client=ScriptedLLM(spec["script"], spec.get("final_text", "")),
    )

    events = []
    for ev, payload in h.run():
        events.append((ev, payload))
        if ev == "confirm_required" and spec.get("confirm") and auto_confirm:
            # 模拟店长在卡片上点「确认」→ 从断点继续（第二段流）
            events.append(("__user_confirm__", {"action_id": payload["action_id"]}))
            for ev2, payload2 in h.resume("confirm", "store01"):
                events.append((ev2, payload2))

    steps = store.list_steps(h.run_id)
    quality = store.trace_quality(h.run_id)

    if verbose:
        _print_scenario(key, spec, h, steps, quality, events)

    return {"run_id": h.run_id, "status": h.status, "steps": steps,
            "quality": quality, "events": events}


# ============================================================
# 控制台打印
# ============================================================

def _width(text: str) -> int:
    """粗略计算显示宽度（中文算 2 列），用于对齐表格。"""
    return sum(2 if ord(c) > 127 else 1 for c in str(text))


def _pad(text, width: int) -> str:
    text = str(text)
    return text + " " * max(0, width - _width(text))


def _cut(text, width: int) -> str:
    text = str(text).replace("\n", " ")
    out = ""
    for ch in text:
        if _width(out + ch) > width:
            return out + "…"
        out += ch
    return out


def _print_scenario(key, spec, h, steps, quality, events):
    line = "─" * 96
    print()
    print("=" * 96)
    print(f"场景 {spec['title']}")
    print(f"用户：{spec['question']}")
    print(line)
    print(f"{_pad('轮', 4)}{_pad('步', 4)}{_pad('工具', 30)}"
          f"{_pad('参数', 34)}{_pad('耗时', 8)}结果摘要")
    print(line)
    for s in steps:
        marker = ""
        if s["decision"] == "interrupt":
            marker = " ⏸"
        elif s["decision"] == "confirmed":
            marker = " ✔"
        elif s["decision"] == "skipped":
            marker = " ↷"
        args = "" if s["tool"] == store.MODEL_STEP else json.dumps(s["args"], ensure_ascii=False)
        print(f"{_pad(s['round_no'], 4)}{_pad(s['step_no'], 4)}"
              f"{_pad(_cut(s['tool'], 28), 30)}"
              f"{_pad(_cut(args, 32), 34)}"
              f"{_pad(str(s['duration_ms']) + 'ms', 8)}"
              f"{_cut(s['result_summary'], 54)}{marker}")
    print(line)
    print(f"运行 {h.run_id}｜状态 {h.status}｜轮次 {quality['rounds']}/{h.max_rounds}｜"
          f"工具调用 {quality['tool_calls']} 次｜阻断 {quality['blocked']} 次｜"
          f"重复调用 {quality['repeats']} 次｜轮内效率 {quality['round_efficiency']}")
    print(f"最终回答：{_cut(h.answer, 150)}")

    # 场景 b 额外展示中断卡的内容（店长在界面上看到的东西）
    for ev, payload in events:
        if ev == "confirm_required":
            p = payload["preview"]
            print(line)
            print("⏸ 中断确认卡（执行前推给店长）：")
            print(f"   商品：{p['sku']} {p['sku_name']}（{p['spec']}）")
            print(f"   数量：{p['qty_text']}    整箱换算：{p['box_text']}")
            print(f"   配送：{p['delivery_date']}（{p['delivery_weekday']}），{p['line_name']}，"
                  f"余量 {p['line_remaining']} 件")
            for c in p["checks"]:
                print(f"   {'✓' if c['ok'] else '✕'} {c['name']}：{c['detail']}")
            if p.get("adjust"):
                a = p["adjust"]
                print(f"   ↻ 已调整：原申请 {a['from_qty']} 件（{a['from_boxes']} 箱）"
                      f"→ {a['to_qty']} 件（{a['to_boxes']} 箱）：{a['reason']}")
            break


def run_all(keys=None):
    db.init_db()
    results = {}
    for key in (keys or list(SCENARIOS.keys())):
        results[key] = run_scenario(key)
    print()
    print("=" * 96)
    print("回归断言：")
    ok_all = True
    for key, r in results.items():
        spec = SCENARIOS[key]()
        exp = spec["expect"]
        names = [s["tool"] for s in r["steps"]]
        problems = []
        if r["status"] != exp["status"]:
            problems.append(f"状态 {r['status']} != {exp['status']}")
        if r["quality"]["rounds"] > exp["max_rounds"]:
            problems.append(f"轮次 {r['quality']['rounds']} > {exp['max_rounds']}")
        for t in exp.get("must_tools", []):
            if t not in names:
                problems.append(f"缺少必需工具 {t}")
        for t in exp.get("forbidden_tools", []):
            if t in names:
                problems.append(f"出现了不该调用的工具 {t}")
        if exp.get("interrupt_tool") and exp["interrupt_tool"] not in names:
            problems.append("没有触发写操作中断")
        ok = not problems
        ok_all = ok_all and ok
        print(f"  {'✔' if ok else '✕'} 场景 {key}：{'通过' if ok else '；'.join(problems)}")
    return ok_all


if __name__ == "__main__":
    keys = [k for k in sys.argv[1:] if k in SCENARIOS] or None
    run_all(keys)
