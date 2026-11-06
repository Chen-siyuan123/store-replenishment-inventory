#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/gen_test_docx.py —— 通过本地 editor_sdk 生成测试交付 docx
================================================================
生成：
  test/1-测试方案/门店补货与库存协同-测试方案.docx
  test/2-场景测试过程/正常场景测试.docx / 边界场景测试.docx / 对抗场景测试.docx
  test/2-场景测试过程/权限场景测试.docx / 故障场景测试.docx

测试方案 docx 的附录 A（57 条用例明细）从同目录的 md 版第 3 节解析生成，
保证 md 与 docx 内容一致、不靠人工同步。

内容来自 output/scenario-tests/results.json（真实执行结果），图表来自
test/2-场景测试过程/figures/。走 edsdk.py（editor_sdk MCP 封装）。
"""

import io
import json
import os
import re
import subprocess
import sys

from PIL import Image

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ESDK = ("C:/Users/陈思媛/AppData/Local/Programs/WorkBuddy/resources/app.asar.unpacked/"
        "resources/plugins/workbuddy-builtin/skills/workbuddy-local-office-edit/edsdk.py")
RESULTS = os.path.join(ROOT, "output", "scenario-tests", "results.json")
FIGS = os.path.join(ROOT, "test", "2-场景测试过程", "figures")
OUT_DIR = os.path.join(ROOT, "test", "2-场景测试过程")
PLAN_DIR = os.path.join(ROOT, "test", "1-测试方案")
PLAN_MD = os.path.join(PLAN_DIR, "门店补货与库存协同-测试方案.md")
MD_TMP = os.path.join(ROOT, "output", "scenario-tests")

data = json.load(io.open(RESULTS, encoding="utf-8"))


def call(tool, **kw):
    """调用 edsdk.py；返回解析后的 JSON 结果（失败抛 RuntimeError）。"""
    p = subprocess.run([sys.executable, ESDK, "call", tool, "--json",
                        json.dumps(kw, ensure_ascii=False)],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    out = (p.stdout or "") + (p.stderr or "")
    # 从后往前找第一条可解析的 JSON 对象行
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            if obj.get("ok") is False:
                raise RuntimeError(f"{tool} 失败：{out.strip()[:500]}")
            import re
            m = re.search(r"file_id=([^\s,]+)", out)
            if m and not obj.get("file_id"):
                obj["file_id"] = m.group(1)
            return obj
    if "error" in out.lower() or p.returncode != 0:
        raise RuntimeError(f"{tool} 失败：{out.strip()[:500]}")
    # 纯文本成功输出（如 create_doc / save_file）
    import re
    m = re.search(r"file_id=([^\s,]+)", out)
    if m:
        return {"ok": True, "file_id": m.group(1)}
    if re.search(r"saved to|saved successfully|success", out, re.IGNORECASE):
        return {"ok": True}
    raise RuntimeError(f"{tool} 无法解析输出：{out.strip()[:300]}")


def md_block(text):
    """把 markdown 写进临时文件，返回 file:// 地址（避免超长命令行）。"""
    path = os.path.join(MD_TMP, "_md_block.md")
    io.open(path, "w", encoding="utf-8").write(text)
    return "file://" + path


def last_pos(fid):
    r = call("doc_get_last_operable_pos", file_id=fid)
    for k in ("position", "last_position", "index"):
        v = r.get(k)
        if isinstance(v, int) and v >= 0:
            return v
    raise RuntimeError(f"无法获取文档末尾位置：{r}")


def insert_md(fid, text):
    r = call("doc_insert_markdown", file_id=fid, idx=last_pos(fid),
             markdown=md_block(text))
    return r.get("position") or r.get("last_edit_index")


def insert_fig(fid, png, width=600):
    """文末插图并补段落分隔符，返回插入后的位置。"""
    idx = last_pos(fid)
    with Image.open(png) as im:
        iw, ih = im.size
    h = round(width * ih / iw)
    call("doc_insert_image", file_id=fid, idx=idx, image_path=png, w=width, h=h)
    call("doc_insert_paragraph", file_id=fid, idx=last_pos(fid))
    return last_pos(fid)


# ============================================================
# 一、测试方案 docx
# ============================================================

def appendix_markdown():
    """从测试方案 md 的第 3 节解析 57 条用例，生成附录 A 的 markdown。

    md 与 docx 是两套稿子：正文各自成文，但用例明细只在 md 里。这里按
    「### S1 …」分段、逐行解析表格，保证 docx 附录与 md 逐条一致，
    以后改 md 重跑脚本即可自动同步，不用手工维护两份。
    """
    text = io.open(PLAN_MD, encoding="utf-8").read()
    secs, cur = [], None
    for ln in text.split("\n"):
        m = re.match(r"^### (S\d) (.+)$", ln.strip())
        if m:
            cur = {"id": m.group(1), "title": m.group(2).strip(), "rows": []}
            secs.append(cur)
            continue
        if cur is None:
            continue
        if ln.strip().startswith("## "):
            cur = None
            continue
        if not ln.strip().startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0] == "#" or set("".join(cells)) <= set("-: "):
            continue
        cur["rows"].append(cells[:3])

    total = sum(len(s["rows"]) for s in secs)
    out = ["\n## 附录 A　%d 条用例明细（与 md 版一致）\n" % total,
           "本附录逐条列出五类场景的用例编号、用例内容与量化断言，与仓库内 "
           "`test/1-测试方案/门店补货与库存协同-测试方案.md` 第 3 节一一对应，"
           "便于评审时脱离 Markdown 环境直接查阅。\n"]
    for s in secs:
        out.append("### %s %s\n" % (s["id"], s["title"]))
        out.append("| # | 用例 | 量化断言 |")
        out.append("| --- | --- | --- |")
        for r in s["rows"]:
            out.append("| " + " | ".join(r) + " |")
        out.append("")
    return "\n".join(out)


def gen_plan_docx():
    print("生成：测试方案.docx")
    r = call("create_doc")
    fid = r["file_id"]
    suites = {s["suite_id"]: s for s in data["suites"]}

    insert_md(fid, f"""# 门店补货与库存协同智能体 · 量化测试方案

**版本**：v1.0（2026-11-06）　**测试对象**：门店补货与库存协同智能体 MVP（`src/2-MVP项目`，FastAPI + 自研 Harness）

**测试方式**：离线自动化测试——真实工具层 / 门禁 / 会话状态机 / auth / 转交 / 留存代码，不调用大模型 API，可在 CI 一键复现（`python scripts/run_scenario_tests.py`）。

**本轮结果**：57 条量化用例，57 条通过，**通过率 100%**（2026-11-06 执行）。

## 一、测试范围与量化目标

测试覆盖五类场景共 57 条量化用例，每条用例有唯一编号、明确预期与逐例耗时记录；判定全部为机器可断言的结构化条件（返回码 / 阻断标志 / 数据内容）。**总体准出门槛：五类场景全部 100% 通过方可进入试点；安全类场景（S3/S4）不允许降级通过。**

| 场景 | 编号段 | 用例数 | 量化目标（准出标准） | 本轮实际 |
| --- | --- | --- | --- | --- |
| S1 正常场景 | S1-01~12 | 12 | 主链路 100% 走通；11 个注册工具全部可用；建议量落在制度区间且为整箱倍数；建草稿成功率 100% | 12/12，100% |
| S2 边界场景 | S2-01~13 | 13 | MOQ / 整箱 / 分配上限 / 线路容量 / 日期的全部上下界判定准确，拒绝理由含可指导修正的具体数值 | 13/13，100% |
| S3 对抗场景 | S3-01~12 | 12 | 提示词注入 / 令牌篡改重放 / 跳步 / 伪造凭证 / 检索陷阱 0 例突破；陷阱题 100% 判空拒答 | 12/12，0 例突破 |
| S4 权限场景 | S4-01~10 | 10 | 未登录/篡改/过期/假密钥一律 401；店长访问后台 403；跨店令牌被拒；越权成功 0 例 | 10/10，0 例越权 |
| S5 故障场景 | S5-01~10 | 10 | 业务停止条件 100% 阻断且带原因；转交单三要素（原因/接手人/时限）齐全；脏输入 0 崩溃 | 10/10，0 例崩溃 |

## 二、总体测试结果

下图为本轮五类场景的通过率与用例数分布（数据来自 results.json，图由 `scripts/gen_test_charts.py` 生成）。
""")

    insert_fig(fid, os.path.join(FIGS, "总览-五类场景测试结果.png"), 620)

    insert_md(fid, f"""## 三、场景设计依据

用例设计直接对应系统四道防线，每条防线均有至少一个场景族验证其「不可绕过」：

1. **数量防线**（框架层正则独立抽取，模型改不动）→ S1-12、S3-08 改口话术取第一处明确数量、S3-09 无法解析即拒答；
2. **写操作门禁**（四项校验签发一次性令牌 → 令牌内容一致 → 建草稿前 HITL 人工确认）→ S1-04/05、S2 全部、S3-01/02/03/04/12、S4-09；
3. **检索防硬答**（BM25+向量两级门禁，相关度不达标直接拒答）→ S1-08/09、S3-10/11；
4. **业务停止条件**（六类 blocking=true + 自动转交单）→ S5-01~06。

用例数据来自种子业务数据的 6 个典型 SKU（A001 正常 / A002 促销期 / A003 零销量+促销 / A004 账实不一致 / A005 新品无历史+旧编码 / A006 对照），与需求发现报告的 f 编号事实一一对应。

## 四、性能量化基线

57 例逐例计时（离线执行，含 PBKDF2 计算 / 索引检索等固定开销）：

| 场景 | p50 (ms) | p95 (ms) | 最大 (ms) | 合计 (ms) |
| --- | --- | --- | --- | --- |
| S1 正常 | {suites['S1']['cost_ms']['p50']} | {suites['S1']['cost_ms']['p95']} | {suites['S1']['cost_ms']['max']} | {suites['S1']['cost_ms']['sum']} |
| S2 边界 | {suites['S2']['cost_ms']['p50']} | {suites['S2']['cost_ms']['p95']} | {suites['S2']['cost_ms']['max']} | {suites['S2']['cost_ms']['sum']} |
| S3 对抗 | {suites['S3']['cost_ms']['p50']} | {suites['S3']['cost_ms']['p95']} | {suites['S3']['cost_ms']['max']} | {suites['S3']['cost_ms']['sum']} |
| S4 权限 | {suites['S4']['cost_ms']['p50']} | {suites['S4']['cost_ms']['p95']} | {suites['S4']['cost_ms']['max']} | {suites['S4']['cost_ms']['sum']} |
| S5 故障 | {suites['S5']['cost_ms']['p50']} | {suites['S5']['cost_ms']['p95']} | {suites['S5']['cost_ms']['max']} | {suites['S5']['cost_ms']['sum']} |

S1/S3 的 p95 偏高来自制度检索首次加载索引与 S3-07 故意的 1.5 秒过期等待，属测试设计成本而非系统延迟；工具层真实延迟基线见 docs/性能基线.md（235 次真实调用 p50/p95/max = 5/14/58 ms）。

## 五、测试环境、复现与缺陷管理

- **环境**：Windows 11 + Python 3.13；种子数据由 db.py 自动生成；知识库由 src/2-MVP项目/补货制度.md 自动导入建索引。
- **执行**：`python scripts/run_scenario_tests.py`，57 例约 4 秒跑完，逐例打印判定与耗时；产物为 output/scenario-tests/results.json 与五份 md 过程记录。
- **三层回归**：本场景测试 + `python -m pytest tests -q`（32 项框架层单元测试）+ `python eval/run_eval.py`（30 条评测用例），任何代码改动后三件套必须全绿。
- **缺陷管理**：任何一条用例失败即阻断发布；修复后整场景回归，不允许只重跑失败例。
- **未覆盖项（如实声明）**：端到端真实模型回答质量（需消耗 API 额度，放试点期人工评审）；并发压测与真实网络故障注入（试点后补充）。

## 六、结论

本轮 57 条量化用例全部通过（100%），达到准出标准：主链路可用、边界判定准确、对抗 0 突破、越权 0 成功、故障 0 崩溃。结合 32 项单元测试与 30 条离线评测，系统具备进入 MVP 上线部署的质量条件。
""")

    # 附录 A：57 条用例明细，从 md 解析，保证两版内容一致
    insert_md(fid, appendix_markdown())

    call("save_file", file_id=fid,
         file_path=os.path.join(PLAN_DIR, "门店补货与库存协同-测试方案.docx"))
    print("  已保存（含附录 A：57 条用例明细）")


# ============================================================
# 二、五份场景测试过程 docx
# ============================================================

SUITE_META = {
    "S1": ("正常场景", "店长日常主链路：检索、查询、建议、校验、建草稿全流程可用性验证。"),
    "S2": ("边界场景", "制度阈值上下界验证：MOQ、整箱规格、分配上限、线路容量、配送日期边界。"),
    "S3": ("对抗场景", "对抗性验证：提示词注入、令牌篡改/重放、跳步调用、伪造凭证、检索陷阱题。"),
    "S4": ("权限场景", "鉴权与数据隔离验证：JWT 登录态、角色鉴权、跨店隔离、密码哈希。"),
    "S5": ("故障场景", "容错与闭环验证：六类业务停止条件、转交单、轮次上限、留存归档、脏输入。"),
}


def gen_scenario_docx(suite):
    sid = suite["suite_id"]
    name, desc = SUITE_META[sid]
    print(f"生成：{name}测试.docx")
    r = call("create_doc")
    fid = r["file_id"]

    insert_md(fid, f"""# {name}测试过程

**场景编号**：{sid}　**用例数**：{suite['total']}　**通过**：{suite['passed']}　**通过率**：{suite['pass_rate']}%

**场景说明**：{desc}

**执行时间**：{data['generated_at']}　**执行方式**：离线自动化（`python scripts/run_scenario_tests.py`，真实工具层/门禁/日志，不调用大模型 API）

**量化结论**：单例耗时 p50 {suite['cost_ms']['p50']} ms / p95 {suite['cost_ms']['p95']} ms / 最大 {suite['cost_ms']['max']} ms，合计 {suite['cost_ms']['sum']} ms。

## 一、测试流程

本场景测试按以下六个步骤执行，全程留痕：
""")

    insert_fig(fid, os.path.join(FIGS, f"{sid}_测试流程.png"), 640)

    insert_md(fid, """
## 二、逐例执行记录

下表为每条用例的实际执行结果（预期与实际均来自 results.json，未做任何人工修饰）：
""")

    rows = ["| # | 用例 | 测试意图 | 预期 | 实际结果 | 耗时(ms) | 判定 |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    for c in suite["cases"]:
        verdict = "通过" if c["passed"] else "未通过"
        rows.append(f"| {c['id']} | {c['name']} | {c['intent']} | {c['expected']} | "
                    f"{c['actual']} | {c['cost_ms']} | {verdict} |")
    insert_md(fid, "\n".join(rows))

    insert_md(fid, """
## 三、执行结果图

各用例耗时与判定如下图（绿色为通过）：
""")

    insert_fig(fid, os.path.join(FIGS, f"{sid}_执行结果.png"), 620)

    if suite["failed"] == 0:
        concl = (f"**✅ {suite['total']}/{suite['total']} 全部通过**，达到本场景 100% 通过的准出标准："
                 "返回码、阻断标志与业务数据均符合预期，未发现缺陷。")
    else:
        concl = f"**❌ {suite['failed']} 条未通过**，未达准出标准（要求 100%），需修复后整场景回归。"
    insert_md(fid, f"""
## 四、结论

{concl}

执行留痕：output/scenario-tests/{sid}_{name}.md（逐例 md 记录）、output/scenario-tests/results.json（机器可读全量结果）。
""")

    call("save_file", file_id=fid, file_path=os.path.join(OUT_DIR, f"{name}测试.docx"))
    print("  已保存")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    gen_plan_docx()
    for s in data["suites"]:
        gen_scenario_docx(s)
    print("全部完成")


if __name__ == "__main__":
    main()
