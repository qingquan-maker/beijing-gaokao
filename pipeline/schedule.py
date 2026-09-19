"""采集排程：把全校清单按天轮转，切成每天一小批。

为什么要这样
------------
考试院侧目前有 620 所学校。全量抓一遍要 600+ 次请求（受 ≥1s 限速约束，
约 15 分钟以上），既慢又对目标站点不够友好；而每天什么都不做又失去了
"定时更新"的意义。

折中方案：**按天轮转**。把学校按代码排序后按 31 天切片，每天只抓属于今天
的那一批（620 / 31 ≈ 20 所）。这样：
  * 单次运行约 1 分钟，稳稳落在 GitHub Actions 免费额度内
  * 一个月刚好把所有学校覆盖一遍
  * 内容指纹（SHA-256）保证重复抓到的学校只要没变就不会重复入库

这正是需求里"按学校分别设置采集计划，而不是一次性全量抓取"的落地方式。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402
from db import connect       # noqa: E402


def all_school_codes(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT school_code FROM schools WHERE school_code != '' ORDER BY school_code"
    ).fetchall()
    return [r["school_code"] for r in rows]


def daily_school_batch(
    conn: sqlite3.Connection,
    days: int = 28,
    on_date: date | None = None,
) -> list[str]:
    """返回"今天轮到"的学校代码。

    用 `序号 % days == 当月第几天 % days` 切片，保证一个周期内不重不漏。

    days 默认取 **28** 而不是 31：每月最少 28 天，28 天周期能让每个槽位
    每个月都被轮到；若用 31，则 2 月（28/29 天）永远轮不到第 29、30 号槽位，
    会出现"部分学校长期抓不到"的隐蔽问题。
    """
    if days < 1:
        raise ValueError("days 必须 >= 1")
    codes = all_school_codes(conn)
    if not codes:
        return []
    today = on_date or date.today()
    slot = today.day % days
    return [code for index, code in enumerate(codes) if index % days == slot]


def main() -> int:
    parser = argparse.ArgumentParser(description="打印今日应采集的学校代码批次")
    parser.add_argument("--days", type=int, default=28, help="轮转周期天数，默认 28")
    parser.add_argument("--date", help="指定日期 YYYY-MM-DD（用于验证切片是否正确）")
    parser.add_argument("--db", help="SQLite 路径")
    parser.add_argument("--count", action="store_true", help="只打印批次大小")
    args = parser.parse_args()

    conn = connect(Path(args.db) if args.db else None, init=False)
    try:
        on_date = date.fromisoformat(args.date) if args.date else None
        batch = daily_school_batch(conn, args.days, on_date)
        total = len(all_school_codes(conn))
    finally:
        conn.close()

    if args.count:
        print(f"{len(batch)}/{total}")
        return 0

    # 输出给 shell 用的逗号分隔清单（无日志噪声）
    sys.stdout.write(",".join(batch))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
