#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/gen_deploy_docx.py —— 生成《门店补货与库存协同-部署说明.docx》"""

import importlib.util
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT = os.path.join(ROOT, "test", "3-上线部署", "门店补货与库存协同-部署说明.docx")
MD = os.path.join(ROOT, "test", "3-上线部署", "门店补货与库存协同-部署说明.md")

spec = importlib.util.spec_from_file_location(
    "g", os.path.join(ROOT, "scripts", "gen_test_docx.py"))
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

print("生成：部署说明.docx")
r = g.call("create_doc")
fid = r["file_id"]

md_text = open(MD, encoding="utf-8").read()
# 分段插入：单次插入过长会导致 HTML 转换失败
import re
blocks, cur = [], []
for line in md_text.splitlines():
    cur.append(line)
    if line.strip() == "" and cur:
        blocks.append("\n".join(cur))
        cur = []
if cur:
    blocks.append("\n".join(cur))
for i, b in enumerate(blocks):
    b = b.strip("\n")
    if not b.strip():
        continue
    g.insert_md(fid, b)
    if i % 8 == 0:
        print(f"  进度 {i}/{len(blocks)}")
print(f"  共插入 {len(blocks)} 段")

g.call("save_file", file_id=fid, file_path=OUT)
print("  已保存：", OUT)
