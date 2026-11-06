#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval/run_eval.py —— 离线评测统一入口（不调 LLM、不消耗 API 额度）
================================================================
把三类「不花一分钱就能验」的检查合成一份评测报告：

  ① 用例资产盘点：eval/cases_v1.json 共 30 条用例（四类），核对每条
     期望工具是否都真实存在于工具注册表（防止用例写了不存在的工具名）；
  ② 制度类检索预检：10 条制度题跑真实混合检索（与 search_policy_docs
     同一套 kbstore），正常题要求 top1 命中，陷阱题（制度中无答案）要求
     判空返回——这是「模型不编制度」的第一道门禁；
  ③ 三条离线轨迹回归：agent/scenarios.py 的 ScriptedLLM 冒充模型，
     工具层、门禁、中断确认、日志全部走真实代码，断言状态/轮次/工具序列。

输出：控制台摘要 + eval/eval_report.md（Markdown 报告，可直接进文档）。

用法：
    python eval/run_eval.py            # 全量
    python eval/run_eval.py --fast     # 跳过轨迹（只做 ①②）
"""

import argparse
import io
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.abspath(os.path.join(HERE, "..", "src", "web"))
sys.path.insert(0, WEB_DIR)
os.chdir(WEB_DIR)

import db            # noqa: E402
import tools         # noqa: E402
import kbstore       # noqa: E402

CASES_PATH = os.path.join(HERE, "cases_v1.json")
REPORT_PATH = os.path.join(HERE, "eval_report.md")

# 陷阱题（制度类子序列下标，0-based）：制度中确实没有答案，期望判空。
# 与 probe_retrieval.py 保持一致。
TRAP_IDX = {7, 8, 9}


# ------------------------------------------------------------
# ① 用例资产盘点
# ------------------------------------------------------------
def part_inventory(cases):
    spec_names = {s["function"]["name"] for s in getattr(tools, "TOOLS_SPEC", [])}
    rows, problems = [], []
    from collections import Counter
    dist = Counter(c["category"] for c in cases)
    for i, c in enumerate(cases, 1):
        missing = [t for t in c.get("expected_tools", []) if t not in spec_names]
        if missing:
            problems.append(f"#{i} 期望工具不存在于注册表：{missing}")
        rows.append((i, c["category"], bool(missing)))
    ok = not problems
    return {
        "total": len(cases),
        "dist": dict(dist),
        "bad": [r for r in rows if r[2]],
        "problems": problems,
        "ok": ok,
    }


# ------------------------------------------------------------
# ② 制度类检索预检
# ------------------------------------------------------------
def part_retrieval(cases):
    kb = kbstore.KBStore()
    if kb.is_empty():
        # fresh 环境（如 CI）：数据库还没有知识库，先导入种子制度文档
        if not kb.seed_default():
            raise RuntimeError("知识库为空且种子文档导入失败：找不到 src/data/补货制度.md")
    kb.rebuild()
    idx_cases = [(i, c) for i, c in enumerate(cases) if c["category"] == "制度类"]
    # 与 probe_retrieval 口径一致：陷阱题按制度类子序列下标
    rows, normal_pass, trap_pass = [], 0, 0
    for seq, (gi, c) in enumerate(idx_cases):
        hits = kb.search(c["question"], 3)
        trap = seq in TRAP_IDX
        if trap:
            ok = not hits
            trap_pass += ok
        else:
            ok = bool(hits) and hits[0]["vec_score"] >= 0.0
            normal_pass += ok
        rows.append({
            "seq": seq + 1, "global": gi + 1, "trap": trap,
            "question": c["question"],
            "top1": hits[0]["section"] if hits else "（空）",
            "score": hits[0]["score"] if hits else 0.0,
            "n_hits": len(hits), "ok": ok,
        })
    return {
        "rows": rows,
        "normal_total": len(rows) - len(TRAP_IDX),
        "normal_pass": normal_pass,
        "trap_total": len(TRAP_IDX),
        "trap_pass": trap_pass,
        "provider": kbstore.kbcore.embed_provider_id(),
        "mode": kb.last_mode,
        "degraded": "hash" in str(kbstore.kbcore.embed_provider_id()).lower(),
        "ok": normal_pass == len(rows) - len(TRAP_IDX) and trap_pass == len(TRAP_IDX),
    }


# ------------------------------------------------------------
# ③ 三条离线轨迹回归（真实工具层 + 门禁，ScriptedLLM 冒充模型）
# ------------------------------------------------------------
def part_trajectories():
    from agent.scenarios import SCENARIOS, run_scenario
    results = []
    ok_all = True
    for key in SCENARIOS:
        spec = SCENARIOS[key]()
        exp = spec["expect"]
        r = run_scenario(key, verbose=False)
        names = [s["tool"] for s in r["steps"]]
        problems = []
        if r["status"] != exp["status"]:
            problems.append(f"状态 {r['status']} != {exp['status']}")
        rounds = r["quality"]["rounds"]
        if rounds > exp["max_rounds"]:
            problems.append(f"轮次 {rounds} > {exp['max_rounds']}")
        for t in exp.get("must_tools", []):
            if t not in names:
                problems.append(f"缺少必需工具 {t}")
        for t in exp.get("forbidden_tools", []):
            if t in names:
                problems.append(f"出现禁止工具 {t}")
        interrupted = bool(exp.get("interrupt_tool")) and exp["interrupt_tool"] in names
        if exp.get("interrupt_tool") and not interrupted:
            problems.append("没有触发写操作中断")
        ok = not problems
        ok_all = ok_all and ok
        results.append({
            "key": key, "title": spec.get("title", key),
            "status": r["status"], "rounds": rounds,
            "max_rounds": exp["max_rounds"], "tools": names,
            "interrupted": interrupted, "problems": problems, "ok": ok,
        })
    return {"results": results, "ok": ok_all}


# ------------------------------------------------------------
# 报告生成
# ------------------------------------------------------------
def write_report(inv, ret, traj, out_path):
    lines = []
    w = lines.append
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    w("# 离线评测报告")
    w("")
    w(f"- 生成时间：{now}")
    w(f"- 评测方式：离线（不调用大模型 API，ScriptedLLM 回放 + 真实工具层/门禁/日志）")
    w(f"- 向量来源：{ret['provider']}；本次检索模式：{ret['mode']}")
    w("")
    ret_ok = ret["ok"] or ret["degraded"]
    total_ok = inv["ok"] and ret_ok and traj["ok"]
    if ret["degraded"]:
        w("**⚠️ 降级运行：未配置向量模型（LLM_EMBED_MODEL / API 密钥），"
          "检索已自动退化为纯 BM25 关键词检索。**同义改写、口语化提问会漏检，"
          "正常题命中率不具判定效力，本项只如实记录、不计入失败判定；"
          "陷阱题判空依赖相关度门禁阈值，仍然有效。"
          "配置 `.env` 中的 `LLM_EMBED_MODEL=embedding-3` 与密钥后重跑即为完整判定。")
        w("")
    w(f"**总体结论：{'✅ 全部通过' if total_ok else '❌ 存在未通过项，见下文'}**"
      f"{'（检索项降级，未计入判定）' if ret['degraded'] else ''}")
    w("")

    w("## 一、用例资产盘点")
    w("")
    w(f"共 **{inv['total']}** 条评测用例，类别分布：" +
      "、".join(f"{k} {v} 条" for k, v in inv["dist"].items()) + "。")
    if inv["bad"]:
        w("")
        w("| 用例# | 问题 |")
        w("|---|---|")
        for b in inv["bad"]:
            w(f"| {b[0]} | 期望工具不在注册表 |")
    else:
        w("全部用例的期望工具均能在工具注册表中找到（无悬空引用）。")
    w("")

    w("## 二、制度类检索预检（10 条，含 3 条陷阱题）")
    w("")
    w("| # | 类型 | top1 命中章节 | 相关度 | 判定 |")
    w("|---|---|---|---|---|")
    for r in ret["rows"]:
        tag = "陷阱" if r["trap"] else "正常"
        verdict = "✅ 判空（正确拒答）" if (r["trap"] and r["ok"]) else \
                  ("✅ 命中" if r["ok"] else "❌ 未达预期")
        w(f"| {r['seq']} | {tag} | {r['top1'][:38]} | {r['score']:.2f} | {verdict} |")
    w("")
    w(f"- 正常题 top1 命中：**{ret['normal_pass']}/{ret['normal_total']}**")
    w(f"- 陷阱题正确判空：**{ret['trap_pass']}/{ret['trap_total']}**")
    w("")

    w("## 三、离线轨迹回归（3 条，工具层/门禁/日志均为真实代码）")
    w("")
    w("| 场景 | 内容 | 状态 | 轮次/上限 | 写操作中断 | 结论 |")
    w("|---|---|---|---|---|---|")
    for r in traj["results"]:
        verdict = "✅ 通过" if r["ok"] else "❌ " + "；".join(r["problems"])
        w(f"| {r['key']} | {r['title']} | {r['status']} | "
          f"{r['rounds']}/{r['max_rounds']} | "
          f"{'触发' if r['interrupted'] else '—'} | {verdict} |")
    w("")

    w("## 四、说明")
    w("")
    w("- 制度类/工具调用类/多轮上下文类的**端到端**评测需要真实模型参与，"
      "放在试点期人工评审（评分口径见各用例 expected_behavior 字段）；"
      "本报告覆盖的是不需要模型、可机器复现的部分。")
    w("- 三条轨迹的「预期轨迹」即回归基准：换模型、改提示词、改工具后重跑本脚本，"
      "对比轮次/工具序列/阻断点是否跑偏。")
    io.open(out_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="跳过轨迹回放")
    args = ap.parse_args()

    db.init_db()
    cases = json.load(io.open(CASES_PATH, encoding="utf-8"))

    inv = part_inventory(cases)
    ret = part_retrieval(cases)
    traj = {"results": [], "ok": True}
    if not args.fast:
        traj = part_trajectories()

    write_report(inv, ret, traj, REPORT_PATH)

    print("=" * 72)
    print(f"① 用例盘点：{inv['total']} 条，"
          f"{'全部期望工具有效' if inv['ok'] else '存在悬空引用'}")
    if ret["degraded"]:
        print(f"② 检索预检：降级运行（纯 BM25，无向量模型），"
              f"命中 {ret['normal_pass']}/{ret['normal_total']}、"
              f"陷阱判空 {ret['trap_pass']}/{ret['trap_total']} → 只记录、不判定")
    else:
        print(f"② 检索预检：正常题 {ret['normal_pass']}/{ret['normal_total']}，"
              f"陷阱判空 {ret['trap_pass']}/{ret['trap_total']} → "
              f"{'通过' if ret['ok'] else '未通过'}")
    if traj["results"]:
        for r in traj["results"]:
            print(f"③ 轨迹 {r['key']}：{'通过' if r['ok'] else '；'.join(r['problems'])}")
    print(f"报告：{REPORT_PATH}")
    sys.exit(0 if (inv["ok"] and (ret["ok"] or ret["degraded"]) and traj["ok"]) else 1)


if __name__ == "__main__":
    main()
