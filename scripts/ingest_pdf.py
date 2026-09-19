#!/usr/bin/env python3
"""采集/解析考试院 PDF 附件：录取投档线 + 分数分布（一分一段）。

用法
----
  # 联网：自动发现「高考高招 > 通知公告」里的投档线与分数分布 PDF 并入库
  python scripts/ingest_pdf.py --years 2026

  # 只处理其中一类
  python scripts/ingest_pdf.py --years 2026 --only score_line
  python scripts/ingest_pdf.py --years 2025,2026 --only rank

  # 离线：手工下载好 PDF 后直接喂进来（公告页抓不到时用）
  python scripts/ingest_pdf.py --local 投档线.pdf --kind score_line --year 2026
  python scripts/ingest_pdf.py --local 分数分布.pdf --kind rank --year 2026

  # 只看解析结果、不写库（体检 PDF 结构是否变了）
  python scripts/ingest_pdf.py --local 投档线.pdf --kind score_line --year 2026 --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings            # noqa: E402
from crawler import bjeea_pdf          # noqa: E402
from db import connect                 # noqa: E402
from pipeline import build_db, export_json  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="考试院 PDF 附件采集入库")
    p.add_argument("--years", default="2026", help="年份，逗号分隔")
    p.add_argument("--only", choices=["score_line", "rank"], help="只处理一类")
    p.add_argument("--local", help="本地 PDF 路径（离线模式，不联网）")
    p.add_argument("--kind", choices=["score_line", "rank"], help="配合 --local 指定 PDF 类型")
    p.add_argument("--year", type=int, help="配合 --local 指定年份")
    p.add_argument("--batch", default="", help="配合 --local 指定兜底批次")
    p.add_argument("--max-list-pages", type=int, default=8, help="公告列表最多翻几页")
    p.add_argument("--dry-run", action="store_true", help="只解析不写库")
    p.add_argument("--no-export", action="store_true", help="不重新导出前端 JSON")
    return p.parse_args()


def cmd_local(args: argparse.Namespace, conn) -> int:
    if not args.kind or not args.year:
        print("--local 需要同时指定 --kind 与 --year", file=sys.stderr)
        return 2
    data = Path(args.local).read_bytes()
    if args.kind == "score_line":
        rows, problems = bjeea_pdf.parse_score_line_pdf(data)
        print(f"解析到 {len(rows)} 行，校验失败 {len(problems)} 行")
        for r in rows[:3]:
            print("  ", json.dumps(r, ensure_ascii=False))
        print("   ...")
        for r in rows[-2:]:
            print("  ", json.dumps(r, ensure_ascii=False))
        for line in problems[:5]:
            print("   [校验失败]", line)
        if args.dry_run:
            return 0
        n = bjeea_pdf.load_score_lines(
            conn, args.year, rows, f"local:{Path(args.local).name}",
            batch_fallback=args.batch,
        )
        print(f"入库 group_admissions: {n} 行")
    else:
        rows, problems = bjeea_pdf.parse_rank_pdf(data)
        print(f"解析到 {len(rows)} 段，异常 {len(problems)} 行")
        for r in rows[:3]:
            print("  ", json.dumps(r, ensure_ascii=False))
        for line in problems[:5]:
            print("   [异常]", line)
        if args.dry_run:
            return 0
        n = bjeea_pdf.load_rank_rows(
            conn, args.year, rows, f"local:{Path(args.local).name}"
        )
        print(f"入库 score_rank: {n} 段")
    return 0


def main() -> int:
    args = parse_args()
    settings.ensure_dirs()
    conn = connect()

    if args.local:
        code = cmd_local(args, conn)
        if code or args.dry_run:
            return code
    else:
        years = [int(y) for y in args.years.split(",") if y.strip()]
        want_score_lines = args.only in (None, "score_line")
        want_rank = args.only in (None, "rank")
        result = bjeea_pdf.crawl_pdf_sources(
            conn, years,
            want_score_lines=want_score_lines, want_rank=want_rank,
            max_list_pages=args.max_list_pages,
        )
        print("== PDF 采集 ==")
        for f in result["files"]:
            print(f"  [{f['kind']:<10}] {f['year']} {f['title']} "
                  f"→ {f['rows']} 行（校验失败 {f['problems']}）")
            print(f"               {f['url']}")
        print(json.dumps({k: v for k, v in result.items() if k != "files"},
                         ensure_ascii=False, indent=2))
        if not result["files"]:
            print("  未发现任何投档线/分数分布 PDF —— 公告标题或栏目结构可能变了")

    print("\n== 重算专业组投档位次 ==")
    print(f"  已回填位次 {bjeea_pdf.recompute_group_ranks(conn)} 行")

    print("\n== 派生 + 统计 ==")
    print(json.dumps(build_db.rebuild_derived(conn), ensure_ascii=False))
    stats = build_db.stats(conn)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    group_n = conn.execute("SELECT COUNT(*) AS n FROM group_admissions").fetchone()["n"]
    print(f"  group_admissions: {group_n}")

    if not args.no_export:
        print("\n== 导出前端 JSON ==")
        print(json.dumps(export_json.export(conn)["counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
