"""统计工具层调用耗时，生成性能基线表。

数据来源：chat.db 的 agent_steps（duration_ms 为工具函数自身执行耗时，不含模型推理）。
用途：给性能基线一份可复现的数字，也方便以后改工具时对比有没有变慢。

用法：
    python scripts/latency_stats.py            # 打印 Markdown 表
    python scripts/latency_stats.py --write    # 覆盖写入 docs/性能基线.md 的数据段
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "src" / "web" / "chat.db"
DOC_PATH = ROOT / "docs" / "delivery" / "性能基线.md"


def pct(values: list[int], p: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1)))))
    return ordered[idx]


def collect() -> tuple[dict[str, list[int]], int]:
    if not DB_PATH.exists():
        raise SystemExit(f"找不到数据库：{DB_PATH}（先跑 cd src/2-MVP项目 && python -m agent.scenarios）")
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "select tool, duration_ms from agent_steps "
        "where tool <> '__model__' and duration_ms is not null"
    ).fetchall()
    by_tool: dict[str, list[int]] = defaultdict(list)
    for tool, ms in rows:
        by_tool[tool].append(int(ms))
    return by_tool, len(rows)


def render(by_tool: dict[str, list[int]], total: int) -> str:
    allv = [ms for v in by_tool.values() for ms in v]
    lines = [
        "| 工具 | 调用次数 | p50 | p95 | 最大 |",
        "|---|---:|---:|---:|---:|",
    ]
    for tool, v in sorted(by_tool.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"| `{tool}` | {len(v)} | {pct(v, 50)} ms | {pct(v, 95)} ms | {max(v)} ms |")
    lines.append(
        f"| **合计** | **{total}** | **{pct(allv, 50)} ms** | **{pct(allv, 95)} ms** | **{max(allv)} ms** |"
    )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="工具层耗时统计")
    ap.add_argument("--write", action="store_true", help="把表格写进 docs/性能基线.md 的数据段")
    args = ap.parse_args()

    by_tool, total = collect()
    table = render(by_tool, total)
    print(table)

    if args.write and DOC_PATH.exists():
        text = DOC_PATH.read_text(encoding="utf-8")
        start, end = "<!-- stats:start -->", "<!-- stats:end -->"
        if start in text and end in text:
            head, rest = text.split(start, 1)
            _, tail = rest.split(end, 1)
            DOC_PATH.write_text(f"{head}{start}\n{table}\n{end}{tail}", encoding="utf-8")
            print(f"\n已更新 {DOC_PATH}")
        else:
            raise SystemExit("性能基线.md 里找不到 stats 标记")


if __name__ == "__main__":
    main()
