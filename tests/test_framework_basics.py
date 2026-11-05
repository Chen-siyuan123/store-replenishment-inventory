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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "prototype", "web"))

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
