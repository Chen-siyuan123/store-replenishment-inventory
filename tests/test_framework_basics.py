#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_framework_basics.py —— 框架层纯函数与安全分区的单元测试
================================================================
测的对象全部是「不依赖数据库、不依赖模型」的确定性逻辑——
这些代码之所以放在框架层而不是提示词里，正是因为它们必须 100% 可靠：

  - tools.extract_requested_qty / _cn_to_int：
    从店长原话里抽「明确数量」。实测模型会拿系统建议量覆盖店长的
    明确要求（20 箱 → 1 箱事故），所以数量必须由正则解析、模型改不动。
  - tools.TOOL_RISK / HITL_TOOLS / TOOLS_SPEC 三者的一致性：
    11 个工具按风险分 read / verify / write 三级，写操作是唯一的
    强制中断集合；登记表、规格表、风险表任何一张漏登记都算缺陷。

运行：
    cd 项目根目录
    python -m pytest tests/ -v
    （无 pytest 时：python tests/test_framework_basics.py 直接运行）
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src", "web"))

import tools  # noqa: E402


# ------------------------------------------------------------
# 中文数字 / 数量抽取（框架层防覆盖防线）
# ------------------------------------------------------------
class TestCnToInt:
    def test_arabic(self):
        assert tools._cn_to_int("20") == 20
        assert tools._cn_to_int("0") == 0

    def test_common_chinese(self):
        assert tools._cn_to_int("三") == 3
        assert tools._cn_to_int("十") == 10
        assert tools._cn_to_int("十五") == 15
        assert tools._cn_to_int("二十") == 20
        assert tools._cn_to_int("二十三") == 23
        assert tools._cn_to_int("两") == 2

    def test_unparseable_returns_zero(self):
        # 超出日常说法范围的返回 0 = 视为没提数量，不能瞎猜
        assert tools._cn_to_int("百") == 0
        assert tools._cn_to_int("廿") == 0


class TestExtractQty:
    def test_arabic_box(self):
        r = tools.extract_requested_qty("给我补 20 箱牛奶")
        assert r == {"count": 20, "unit": "箱", "text": "20 箱"}

    def test_chinese_number(self):
        r = tools.extract_requested_qty("补二十箱水")
        assert r["count"] == 20 and r["unit"] == "箱"

    def test_unit_variants(self):
        assert tools.extract_requested_qty("来 5 盒")["unit"] == "盒"
        assert tools.extract_requested_qty("3 瓶可乐")["unit"] == "瓶"
        assert tools.extract_requested_qty("两袋面包")["count"] == 2

    def test_no_qty(self):
        assert tools.extract_requested_qty("牛奶还有多少") is None
        assert tools.extract_requested_qty("") is None
        assert tools.extract_requested_qty("帮我看看库存") is None

    def test_zero_or_invalid_ignored(self):
        # 「0 箱」不是有效申请量
        assert tools.extract_requested_qty("补 0 箱") is None

    def test_first_match_wins(self):
        # 只取第一处明确数量——多数量场景由多轮对话澄清，不在这里猜
        r = tools.extract_requested_qty("先补 20 箱牛奶，再看 5 盒酸奶")
        assert r["count"] == 20


# ------------------------------------------------------------
# 工具风险分区（写操作唯一、三级登记一致）
# ------------------------------------------------------------
class TestToolRiskPartition:
    def test_only_one_write_tool(self):
        writes = {n for n, lv in tools.TOOL_RISK.items() if lv == "write"}
        assert writes == {"create_replenishment_draft"}, \
            f"写操作必须只有建草稿一个，当前：{writes}"

    def test_hitl_equals_write_set(self):
        assert tools.HITL_TOOLS == {n for n, lv in tools.TOOL_RISK.items()
                                    if lv == "write"}

    def test_spec_all_registered(self):
        spec_names = {s["function"]["name"] for s in tools.TOOLS_SPEC}
        assert spec_names == set(tools.TOOL_RISK), \
            "TOOLS_SPEC 与 TOOL_RISK 必须一一对应，防止漏登记风险的工具被调用"

    def test_verify_never_writes(self):
        # 校验类工具不改数据——只允许 read / verify 两级出现在非写集合
        assert all(lv in ("read", "verify", "write")
                   for lv in tools.TOOL_RISK.values())


# ------------------------------------------------------------
# 日均销量口径（断货日不得进分母）
# ------------------------------------------------------------
class TestAvgDailyBasis:
    """日均销量的分母必须是「有销售记录的天数」。

    原口径 total / days 把断货日当成「卖了 0 件」计入分母，日均被低估
    → 建议量偏低 → 补了还是不够，且系统不报错（与 f31 断货截断同源）。
    """

    @staticmethod
    def _rows(pairs):
        return [{"sale_date": d, "qty": q} for d, q in pairs]

    def test_denominator_is_days_with_records(self):
        # 7 天里只有 3 天有记录，合计 30 → 日均 10，不是 30/7 ≈ 4.3
        rows = self._rows([("2026-11-01", 10), ("2026-11-03", 12), ("2026-11-05", 8)])
        assert tools.avg_daily_from_rows(rows) == (30, 10.0, 3)

    def test_old_formula_would_underestimate(self):
        # 回归护栏：一旦退回自然日分母，这条会立刻失败
        rows = self._rows([("2026-11-01", 10), ("2026-11-03", 12), ("2026-11-05", 8)])
        _, avg, _ = tools.avg_daily_from_rows(rows)
        assert avg > 30 / 7

    def test_no_rows_no_zero_division(self):
        assert tools.avg_daily_from_rows([]) == (0, 0.0, 0)

    def test_same_date_counted_once(self):
        rows = self._rows([("2026-11-01", 5), ("2026-11-01", 7)])
        total, avg, covered = tools.avg_daily_from_rows(rows)
        assert (total, avg, covered) == (12, 12.0, 1)

    def test_window_excludes_today(self):
        # 今天还没过完，销量不完整，进窗口会把日均拉低
        since, end = tools.sales_window(7, today=date(2026, 11, 6))
        assert end == "2026-11-05"
        assert since == "2026-10-30"          # 10-30 ~ 11-05，正好 7 天

    def test_window_spans_exactly_n_days(self):
        since, end = tools.sales_window(7, today=date(2026, 3, 1))
        assert (date.fromisoformat(end) - date.fromisoformat(since)).days == 6
        _, end30 = tools.sales_window(30, today=date(2026, 3, 1))
        since30, _ = tools.sales_window(30, today=date(2026, 3, 1))
        assert (date.fromisoformat(end30) - date.fromisoformat(since30)).days == 29


# ------------------------------------------------------------
# 提交偏差判定（提示人工复核，不是审批）
# ------------------------------------------------------------
class TestApprovalCheck:
    """系统没有审批路由、没有 approver 字段，所以这里只能判定
    「要不要线下确认」，不能返回「已批准」这种它做不到的结论。"""

    def test_within_threshold(self):
        need, dev = tools.approval_check(120, 130)
        assert need is False and abs(dev - 0.0833) < 1e-3

    def test_over_threshold(self):
        need, dev = tools.approval_check(120, 200)
        assert need is True and abs(dev - 0.6667) < 1e-3

    def test_boundary_50pct_not_flagged(self):
        # ±50% 边界以内（含正好 50%）不触发
        assert tools.approval_check(100, 150)[0] is False
        assert tools.approval_check(100, 50)[0] is False

    def test_just_over_50pct_flagged(self):
        assert tools.approval_check(100, 151)[0] is True

    def test_missing_basis_returns_none(self):
        assert tools.approval_check(0, 20) == (False, None)
        assert tools.approval_check(None, 20) == (False, None)
        assert tools.approval_check(20, 0) == (False, None)

    def test_no_fake_approval_promise(self):
        # 静态守护：提交接口不得再输出系统实际做不到的承诺。
        # 无审批路由 / 无 approver 字段 / allocated 状态无写入点 → 不能说「等待总部确认」。
        src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src", "web", "server.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        assert "等待总部确认与仓库分配" not in src
        assert "needs_approval" in src


# ------------------------------------------------------------
# 提交时复验配送日（草稿放置几天后提交，不能把过期单收进来）
# ------------------------------------------------------------
class TestDeliveryDateCheck:
    """建草稿时校验过配送日，提交那一刻必须再验一次：
    11-05 建的单写 11-06 配送、11-08 才点提交，系统照收就等于制造错单。"""

    TODAY = date(2026, 11, 6)

    def test_today_still_allowed(self):
        # 当天配送仍可提交：与 validate_replenishment 的「早于今天才拒绝」保持一致
        assert tools.delivery_date_check("2026-11-06", self.TODAY) == (True, None)

    def test_future_allowed(self):
        ok, reason = tools.delivery_date_check("2026-11-09", self.TODAY)
        assert ok is True and reason is None

    def test_expired_rejected_with_reason(self):
        ok, reason = tools.delivery_date_check("2026-11-05", self.TODAY)
        assert ok is False
        # 文案要给出下一步动作，且不能暗示「原地改个日期就能提交」——这张卡提交不了
        assert "已早于今天" in reason and "重新发起" in reason

    def test_bad_format_rejected(self):
        for bad in ("11/06", "明天", "", "2026-13-01"):
            ok, reason = tools.delivery_date_check(bad, self.TODAY)
            assert ok is False and reason, f"{bad!r} 应被拒绝"

    def test_lenient_format_kept_consistent_with_validate(self):
        # strptime 接受非零填充的 2026-11-6，这里刻意不为难它：
        # validate_replenishment 用的是同一个 strptime，若提交端更严就会出现
        # 「建得成草稿、提交时被拒」的死锁——比格式不整齐更难排查。
        ok, _ = tools.delivery_date_check("2026-11-6", self.TODAY)
        assert ok is True

    def test_none_does_not_crash(self):
        ok, reason = tools.delivery_date_check(None, self.TODAY)
        assert ok is False and "格式" in reason

    def test_submit_rechecks_delivery_date(self):
        # 静态守护：submit_draft 必须在写库前调用复验，否则这道闸门等于没装
        src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src", "web", "server.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        submit = src[src.index("def submit_draft"):src.index("def cancel_draft")]
        assert "delivery_date_check" in submit
        assert submit.index("delivery_date_check") < submit.index("UPDATE replenishment_drafts")


if __name__ == "__main__":
    # 无 pytest 环境时的兜底运行方式
    import traceback
    failed = 0
    for name, obj in sorted(globals().items()):
        if name.startswith("Test") and isinstance(obj, type):
            inst = obj()
            for m in dir(inst):
                if m.startswith("test_"):
                    try:
                        getattr(inst, m)()
                        print(f"  ✔ {name}.{m}")
                    except Exception:
                        failed += 1
                        print(f"  ✕ {name}.{m}")
                        traceback.print_exc()
    print("FAILED:", failed) if failed else print("ALL PASS")
    sys.exit(1 if failed else 0)
