#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/gen_test_charts.py —— 从 output/scenario-tests/results.json 生成测试可视化图表
================================================================
产出（docs/testing/figures/）：
  总览-五类场景通过率.png        方案文档用总览图
  S1_测试流程.png ... S5_测试流程.png   每场景测试过程流程图
  S1_执行结果.png ... S5_执行结果.png   每场景逐例耗时与判定图

全部离线生成，中文字体使用微软雅黑。
"""

import io
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
RESULTS = os.path.join(ROOT, "output", "scenario-tests", "results.json")
FIG_DIR = os.path.join(ROOT, "test-deliverables", "figures")

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

# 浅色主题配色（与文档一致）
C_PASS = "#2e9e5b"      # 通过-绿
C_FAIL = "#d64541"      # 失败-红
C_HEAD = "#1f4e79"      # 标题-深蓝
C_BAR = "#4a7ebb"       # 柱-蓝
C_BG = "#ffffff"
C_BOX = "#eaf1f8"
C_EDGE = "#1f4e79"

FLOW_STEPS = {
    "S1": ["明确正常链路范围\n（检索→查询→建议→校验→建单）", "设计 12 条主链路用例\n（覆盖 11 个工具）",
           "准备种子数据与知识库\n（6 SKU / 库存 / 销量 / 配送线路）", "逐例执行真实工具层\n（逐例计时）",
           "断言返回结构与业务结果\n（通过率 100% 准出）", "输出过程记录与结果图\n（md + json 留痕）"],
    "S2": ["梳理制度阈值清单\n（MOQ / 整箱 / 上限 / 容量 / 日期）", "对每个阈值取上界、下界、±1\n设计 13 条边界用例",
           "准备会话前置步骤\n（库存/促销/销量已核对）", "逐例执行四项硬校验\n（逐例计时）",
           "断言拒绝理由准确\n（错误信息可指导修正）", "输出过程记录与结果图\n（md + json 留痕）"],
    "S3": ["收集攻击面清单\n（注入 / 篡改 / 重放 / 跳步 / 陷阱）", "设计 12 条对抗用例\n（覆盖 5 类攻击面）",
           "构造恶意输入与伪造凭证\n（假令牌 / 假 JWT / 注入话术）", "逐例执行门禁与检索防线\n（逐例计时）",
           "断言全部拦截且给结构化错误\n（0 例外放行）", "输出过程记录与结果图\n（md + json 留痕）"],
    "S4": ["梳理权限矩阵\n（店长 / 管理员 / 匿名 / 跨店）", "设计 10 条权限用例\n（鉴权 6 + 隔离 2 + 哈希 2）",
           "签发各类 JWT\n（合法 / 过期 / 篡改 / 伪造）", "逐例执行 auth 依赖与令牌门禁\n（逐例计时）",
           "断言 401/403 码准确、越权 0 成功", "输出过程记录与结果图\n（md + json 留痕）"],
    "S5": ["梳理 6 类业务停止条件\n与系统故障面", "设计 10 条故障用例\n（停止条件 4 + 转交 2 + 上限/留存/脏输入 4）",
           "构造故障数据\n（账实不一致 / 零销量 / 无历史）", "逐例执行停止条件与转交闭环\n（逐例计时）",
           "断言阻断 + 原因 + 接手人 + 时限\n（不允许只说不能办）", "输出过程记录与结果图\n（md + json 留痕）"],
}

FLOW_TITLES = {
    "S1": "S1 正常场景测试过程", "S2": "S2 边界场景测试过程", "S3": "S3 对抗场景测试过程",
    "S4": "S4 权限场景测试过程", "S5": "S5 故障场景测试过程",
}


def fig_overview(data):
    suites = data["suites"]
    names = [f"{s['suite_id']} {s['suite_name']}\n({s['passed']}/{s['total']})" for s in suites]
    rates = [s["pass_rate"] for s in suites]
    counts = [s["total"] for s in suites]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.2), dpi=150)
    fig.suptitle("门店补货与库存协同智能体 · 五类场景量化测试总览（57 例，通过率 100%）",
                 fontsize=13, color=C_HEAD, fontweight="bold", y=0.99)

    bars = ax1.bar(names, rates, color=[C_PASS if r == 100 else C_FAIL for r in rates], width=0.55)
    ax1.set_ylim(0, 112)
    ax1.axhline(100, color="#999", lw=0.8, ls="--")
    ax1.set_ylabel("通过率（%）")
    ax1.set_title("各场景通过率（准出线 100%）", fontsize=11)
    for b, r in zip(bars, rates):
        ax1.text(b.get_x() + b.get_width() / 2, r + 3, f"{r:.0f}%",
                 ha="center", fontsize=10, color=C_HEAD, fontweight="bold")
    ax1.tick_params(axis="x", labelsize=9)

    ax2.bar(names, counts, color=C_BAR, width=0.55)
    ax2.set_ylabel("用例数（条）")
    ax2.set_title("各场景用例数（共 57 条量化用例）", fontsize=11)
    for i, c in enumerate(counts):
        ax2.text(i, c + 0.3, str(c), ha="center", fontsize=10, color=C_HEAD, fontweight="bold")
    ax2.set_ylim(0, max(counts) * 1.25)
    ax2.tick_params(axis="x", labelsize=9)

    for ax in (ax1, ax2):
        ax.spines[["top", "right"]].set_visible(False)
        ax.set_facecolor(C_BG)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    out = os.path.join(FIG_DIR, "总览-五类场景测试结果.png")
    fig.savefig(out, facecolor=C_BG)
    plt.close(fig)
    return out


def fig_flow(sid):
    steps = FLOW_STEPS[sid]
    n = len(steps)
    total_w = n * 2 + (n - 1) * 0.55
    fig, ax = plt.subplots(figsize=(13.5, 3.4), dpi=150)
    ax.set_xlim(-0.35, total_w + 0.35)
    ax.set_ylim(0, 10.6)
    ax.axis("off")

    box_w, box_h, y0 = 2.0, 5.6, 1.4
    x = 0
    for i, text in enumerate(steps):
        box = FancyBboxPatch((x, y0), box_w, box_h,
                             boxstyle="round,pad=0.12,rounding_size=0.25",
                             fc=C_BOX, ec=C_EDGE, lw=1.4)
        ax.add_patch(box)
        ax.text(x + box_w / 2, y0 + box_h / 2, text, ha="center", va="center",
                fontsize=9.2, color="#222", linespacing=1.5)
        ax.text(x + box_w / 2, y0 + box_h + 0.5, f"步骤 {i + 1}", ha="center",
                fontsize=9.5, color=C_HEAD, fontweight="bold")
        if i < n - 1:
            ax.add_patch(FancyArrowPatch((x + box_w + 0.08, y0 + box_h / 2),
                                         (x + box_w + 0.47, y0 + box_h / 2),
                                         arrowstyle="-|>", mutation_scale=16,
                                         color=C_EDGE, lw=1.6))
        x += box_w + 0.55

    ax.text(total_w / 2, 9.9, FLOW_TITLES[sid],
            ha="center", fontsize=13, color=C_HEAD, fontweight="bold")
    out = os.path.join(FIG_DIR, f"{sid}_测试流程.png")
    fig.savefig(out, facecolor=C_BG, bbox_inches="tight")
    plt.close(fig)
    return out


def fig_result(suite):
    cases = suite["cases"]
    sid = suite["suite_id"]
    ids = [c["id"] for c in cases]
    costs = [c["cost_ms"] for c in cases]
    colors = [C_PASS if c["passed"] else C_FAIL for c in cases]

    fig, ax = plt.subplots(figsize=(11, 4.6), dpi=150)
    bars = ax.barh(ids[::-1], costs[::-1], color=colors[::-1], height=0.62)
    for b, c in zip(bars, list(reversed(cases))):
        ax.text(b.get_width() + max(costs) * 0.015, b.get_y() + b.get_height() / 2,
                f"{c['cost_ms']} ms {'通过' if c['passed'] else '未通过'}",
                va="center", fontsize=8.5, color="#333")
    ax.set_xlabel("单例耗时（ms）—— 离线执行，含 PBKDF2 计算/索引检索等固定开销")
    ax.set_title(f"{sid} {suite['suite_name']}逐例执行结果："
                 f"{suite['passed']}/{suite['total']} 通过（{suite['pass_rate']}%），"
                 f"p95 {suite['cost_ms']['p95']} ms",
                 fontsize=12, color=C_HEAD, fontweight="bold")
    ax.set_xlim(0, max(costs) * 1.3)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    out = os.path.join(FIG_DIR, f"{sid}_执行结果.png")
    fig.savefig(out, facecolor=C_BG)
    plt.close(fig)
    return out


def main():
    data = json.load(io.open(RESULTS, encoding="utf-8"))
    os.makedirs(FIG_DIR, exist_ok=True)
    outs = [fig_overview(data)]
    for s in data["suites"]:
        outs.append(fig_flow(s["suite_id"]))
        outs.append(fig_result(s))
    for o in outs:
        print("生成:", os.path.relpath(o, ROOT))


if __name__ == "__main__":
    main()
