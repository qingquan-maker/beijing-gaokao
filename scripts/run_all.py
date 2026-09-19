#!/usr/bin/env python3
"""三阶段采集编排入口。

用法
----
  # 只跑采集 + 入库 + 导出（默认，无需 API Key）
  python scripts/run_all.py --years 2026 --schools 1021,1023,1028

  # 定时任务：按天轮转，只抓今天轮到的那一批学校（28 天覆盖全校）
  python scripts/run_all.py --rotate-daily 28

  # 强制忽略内容哈希，全量重抓
  python scripts/run_all.py --years 2026 --schools 1021 --force

  # 带上公众号抽取（需要 DEEPSEEK_API_KEY）
  python scripts/run_all.py --with-wechat --schools 1021

  # 只重新导出 JSON，不联网
  python scripts/run_all.py --offline

  # 载入/清除示例数据（前端功能演示用）
  python scripts/run_all.py --demo
  python scripts/run_all.py --clear-demo

  # 查看任务队列断点状态
  python scripts/run_all.py --status
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings          # noqa: E402
from db import connect               # noqa: E402
from pipeline import build_db        # noqa: E402
from pipeline import export_json     # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="北京高考录取数据采集/入库/导出")
    p.add_argument("--years", help="年份，逗号分隔，如 2025,2026")
    p.add_argument("--schools", help="学校代码，逗号分隔；留空需配合 --all 或 --rotate-daily")
    p.add_argument("--stages", default="discover,fetch",
                   help="阶段：discover,fetch,extract（默认 discover,fetch）")
    p.add_argument("--force", action="store_true", help="忽略内容哈希，强制重抓")
    p.add_argument("--offline", action="store_true", help="不联网，只重建派生数据并导出 JSON")
    p.add_argument("--all", action="store_true",
                   help="确认要全量抓取所有学校（不加 --schools 时必须显式指定，"
                        "防止误触发长时间抓取）")
    p.add_argument("--rotate-daily", type=int, nargs="?", const=28, default=None,
                   metavar="DAYS",
                   help="按天轮转：只抓今天轮到的那一批学校（默认 28 天一轮，"
                        "每月覆盖全校）。适合定时任务")
    p.add_argument("--no-export", action="store_true", help="只入库，不导出 JSON")
    p.add_argument("--with-wechat", action="store_true", help="同时跑公众号抽取（需要 API Key）")
    p.add_argument("--demo", action="store_true",
                   help="额外导入 data/seed/demo 的示例数据（前端演示用，页面会显著标注）")
    p.add_argument("--clear-demo", action="store_true", help="清除已导入的示例数据后退出")
    p.add_argument("--status", action="store_true", help="打印断点续爬状态后退出")
    p.add_argument("--db", help="SQLite 路径，默认 data/gaokao.db")
    return p.parse_args()


def split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def cmd_status(conn) -> None:
    from crawler.store import TaskQueue
    from pipeline.build_db import stats

    print("== 任务队列 ==")
    rows = TaskQueue(conn).stats()
    if not rows:
        print("  （空）")
    for r in rows:
        print(f"  {r['stage']:<9} {r['target']:<14} {r['status']:<8} {r['n']}")
    print("\n== 数据统计 ==")
    print(json.dumps(stats(conn), ensure_ascii=False, indent=2))


def main() -> int:
    args = parse_args()
    settings.ensure_dirs()
    conn = connect(Path(args.db) if args.db else None)

    if args.status:
        cmd_status(conn)
        return 0

    if args.clear_demo:
        print("== 清除示例数据 ==")
        print(json.dumps(build_db.clear_demo(conn), ensure_ascii=False, indent=2))
        build_db.rebuild_derived(conn)
        if not args.no_export:
            print(json.dumps(export_json.export(conn)["counts"], ensure_ascii=False))
        return 0

    if args.demo:
        print("== 导入示例数据（DEMO，非真实录取分）==")
        print(json.dumps(build_db.load_demo(conn), ensure_ascii=False, indent=2))
        # --demo 默认只做离线演示；要真的联网抓取需显式加 --all
        if not args.all:
            args.offline = True

    years = [int(y) for y in split_list(args.years)] or list(settings.PLAN_QUERY_YEARS)
    schools = split_list(args.schools)
    stages = tuple(split_list(args.stages))

    # 定时任务：不加 --schools 时，自动抓“今天轮到”的那一批学校
    if not schools and args.rotate_daily and not args.offline:
        from pipeline.schedule import daily_school_batch

        schools = daily_school_batch(conn, args.rotate_daily)
        if schools:
            preview = ",".join(schools[:6]) + (" …" if len(schools) > 6 else "")
            print(f"== 按天轮转（{args.rotate_daily} 天一轮）==")
            print(f"  今日批次：{len(schools)} 所学校  {preview}")
        else:
            print("== 按天轮转 ==\n  库中还没有学校主数据，先跑一次带 --schools 的采集")

    print("== 目标 ==")
    print(f"  年份   : {years}")
    if len(schools) > 8:
        print(f"  学校   : {len(schools)} 所（{','.join(schools[:8])} …）")
    else:
        print(f"  学校   : {schools or '全部（建议用 --schools 或 --rotate-daily 分批）'}")
    print(f"  阶段   : {stages}")
    print(f"  数据库 : {settings.DB_PATH}")
    print(f"  抓取后端: {'Scrapling' if _scrapling() else 'urllib 兜底（未安装 Scrapling）'}")

    if not args.offline:
        # 全量抓取是个长时间操作（600+ 所学校 × ≥1s 限速 ≈ 15 分钟以上），
        # 因此不加 --schools 时必须显式 --all 确认，避免误触发。
        if not schools and not args.all:
            print("\n!! 未指定 --schools，已跳过联网抓取。")
            print("   按学校分批是推荐做法，三种方式任选：")
            print("     python scripts/run_all.py --years 2026 --schools 1021,1023,1028")
            print("     python scripts/run_all.py --rotate-daily 28      # 按天轮转，每月覆盖全校")
            print("     python scripts/run_all.py --years 2026 --all     # 确实要全量")
            print("   只想重建派生数据并导出 JSON 请加 --offline。\n")

        do_crawl = bool(schools) or args.all

        # ---- 阶段 1/2：考试院招生计划 ----
        if do_crawl and ("discover" in stages or "fetch" in stages):
            from crawler.bjeea_plan import crawl_plan_years

            print("\n== 阶段 1/2：北京教育考试院招生计划 ==")
            result = crawl_plan_years(conn, years, schools or None, force=args.force)
            print(json.dumps(result, ensure_ascii=False, indent=2))

        # ---- 一分一段表（位次基准）----
        if do_crawl and ("discover" in stages or "fetch" in stages):
            print("\n== 一分一段表（位次基准）==")
            loaded = build_db.ensure_score_rank(conn)
            print(f"  并入 {loaded} 个分段" if loaded else "  已存在位次基准表，跳过")

        # ---- 高校招生网（按注册表逐校）----
        if do_crawl and ("discover" in stages or "fetch" in stages):
            print("\n== 高校本科招生网 ==")
            from crawler.school_sites import SchoolSiteCrawler

            print(json.dumps(SchoolSiteCrawler(conn).run(), ensure_ascii=False))

        # ---- 公众号抽取（唯一的大模型链路）----
        if args.with_wechat and ("extract" in stages or "fetch" in stages):
            print("\n== 公众号文章抽取（DeepSeek Flash）==")
            from crawler.wechat_articles import WechatCrawler

            print(json.dumps(WechatCrawler(conn).run(), ensure_ascii=False))

    # ---- 阶段 3：派生计算 + 导出 ----
    print("\n== 派生计算：位次换算 / 关联 / 标签 ==")
    print(json.dumps(build_db.rebuild_derived(conn), ensure_ascii=False))

    print("\n== 数据统计 ==")
    print(json.dumps(build_db.stats(conn), ensure_ascii=False, indent=2))

    if args.no_export:
        return 0

    print("\n== 导出前端 JSON ==")
    result = export_json.export(conn)
    print(json.dumps({k: v for k, v in result.items() if k != "files"},
                     ensure_ascii=False, indent=2))
    print("  产出文件：")
    for name, size in sorted(result["files"].items()):
        print(f"    web/data/{name:<26} {size/1024:8.1f} KB")

    # LLM 成本核算
    try:
        from extract.deepseek import monthly_cost_report

        report = monthly_cost_report(conn)
        if report["calls"]:
            print(f"\n== 本月大模型成本 ==  调用 {report['calls']:.0f} 次 · "
                  f"约 ${report['usd']:.4f}")
    except Exception:
        pass

    print("\n完成。本地预览：python scripts/serve.py")
    return 0


def _scrapling() -> bool:
    try:
        from crawler.base import HAS_SCRAPLING

        return HAS_SCRAPLING
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
