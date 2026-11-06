#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eval/probe_retrieval.py —— 制度类用例的「检索侧」预检
================================================================
目的：制度类用例能不能判分，取决于两件事——
  ① 该问题的正确答案所在章节，是否被检索排到 top1；
  ② 陷阱题（制度中无答案）是否会被检索「强行」返回高相关片段。
本脚本把 cases_v1.json 里 category=制度类 的用例逐条送进
kbstore.search（与 search_policy_docs 工具同一套混合检索），
打印 top1 命中的章节与分数，并给出风险提示。

只读：不写库、不调 LLM、不消耗 API 额度。

用法：cd src/web && python ../../eval/probe_retrieval.py
"""

import io
import json
import os
import sys

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "web")
sys.path.insert(0, os.path.abspath(WEB_DIR))
os.chdir(WEB_DIR)

import kbstore  # noqa: E402

CASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases_v1.json")

# 陷阱题下标（0-based）：制度中确实没有答案，期望回答「制度中未找到」
TRAP_IDX = {7, 8, 9}


def main():
    cases = json.load(io.open(CASES_PATH, encoding="utf-8"))
    kb = kbstore.KBStore()
    kb.rebuild()

    print(f"{'#':>2} {'类型':<4} {'命中条数':<6} {'top3 章节（按相关度）':<44} 问题")
    print("-" * 104)

    warnings = []
    for i, c in enumerate(cases):
        if c["category"] != "制度类":
            continue
        hits = kb.search(c["question"], 3)
        trap = i in TRAP_IDX
        tag = "陷阱" if trap else "正常"
        secs = " > ".join(h["section"] for h in hits) or "（空：判定为未找到）"
        print(f"{i + 1:>2} {tag:<4} {len(hits):<8} {secs[:44]:<46} {c['question'][:26]}")

        if trap:
            if hits:
                warnings.append(
                    f"#{i + 1} 陷阱题仍返回 {len(hits)} 条（top1={hits[0]['score']:.2f} "
                    f"{hits[0]['section']}），门禁未生效")
        else:
            if not hits:
                warnings.append(f"#{i + 1} 正常题被判为未找到，门禁过严")
            elif hits[0]["vec_score"] < 0.05:
                # 区分两种原因：接口本次失败（可重试） vs 根本没配向量模型（伪向量）
                if kb.last_embed_error:
                    warnings.append(
                        f"#{i + 1} 本次向量化失败，已降级为纯词法：{kb.last_embed_error[:60]}")
                else:
                    warnings.append(
                        f"#{i + 1} top1 向量分仅 {hits[0]['vec_score']:.4f}，语义路无贡献"
                        f"（疑似未配置 LLM_EMBED_MODEL，走了哈希伪向量）")

    print("\n【风险提示】")
    for w in warnings or ["无"]:
        print("  - " + w)
    print(f"\n向量来源：{kbstore.kbcore.embed_provider_id()}")
    print(f"检索模式：{kb.last_mode}（lexical 表示本次降级为纯 BM25）")


if __name__ == "__main__":
    main()
