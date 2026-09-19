"""SQLite 连接、建表与通用 UPSERT。

`upsert(..., coalesce=True)` 是本项目的关键：招生计划侧只带 plan_count，
录取数据侧只带分数，两边写的是 admissions 的同一自然键。用
`COALESCE(excluded.col, table.col)` 让「有值的覆盖、没值的保留」，
于是两个来源可以任意顺序增量写入而互不破坏。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from config import settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: 采集用线程池（并发 ≤5），所有线程共用一条连接，因此必须：
#:   1) connect(check_same_thread=False)  否则跨线程用连接会直接抛 ProgrammingError
#:   2) 用这把可重入锁把每次读写串行化，避免多个线程的事务相互交错
_DB_LOCK = threading.RLock()


@contextmanager
def write_lock():
    """串行化数据库写入。测试里可用来模拟无锁环境。"""
    with _DB_LOCK:
        yield


def connect(path: str | Path | None = None, *, init: bool = True) -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(
        str(path or settings.DB_PATH),
        timeout=30.0,
        check_same_thread=False,   # 采集线程池会跨线程使用这条连接
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    if init:
        init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def upsert(
    conn: sqlite3.Connection,
    table: str,
    rows: Iterable[Mapping[str, Any]],
    conflict_keys: Sequence[str],
    *,
    coalesce: bool = True,
    update_only: Sequence[str] | None = None,
) -> int:
    """幂等写入。返回写入（含更新）的行数。

    coalesce=True 时，若新值为 NULL 则保留旧值。
    """
    rows = [dict(r) for r in rows if r]
    if not rows:
        return 0

    cols: list[str] = []
    for row in rows:
        for key in row:
            if key not in cols:
                cols.append(key)

    placeholders = ", ".join(f":{c}" for c in cols)
    col_list = ", ".join(cols)

    updatable = update_only or [c for c in cols if c not in conflict_keys]
    if updatable:
        if coalesce:
            sets = ", ".join(f"{c} = COALESCE(excluded.{c}, {table}.{c})" for c in updatable)
        else:
            sets = ", ".join(f"{c} = excluded.{c}" for c in updatable)
        clause = f"DO UPDATE SET {sets}"
    else:
        clause = "DO NOTHING"

    sql = (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(conflict_keys)}) {clause}"
    )
    with _DB_LOCK:
        conn.executemany(sql, rows)
        conn.commit()
    return len(rows)


def query(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    with _DB_LOCK:
        return list(conn.execute(sql, params))


def query_one(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    with _DB_LOCK:
        return conn.execute(sql, params).fetchone()


def rank_for_score(conn: sqlite3.Connection, year: int, score: int | None) -> int | None:
    """位次换算：查 score_rank 中覆盖该分数的那一段，返回累计人数。

    北京公布的尾部是合并区间（如 120-129），落在区间内只能取该段累计人数，
    属于官方口径下的近似值 —— 前端会标注这一点。
    """
    if score is None:
        return None
    with _DB_LOCK:
        row = conn.execute(
            """
            SELECT cumulative_count FROM score_rank
             WHERE year = ? AND score_low <= ? AND ? <= score_high
             ORDER BY score_high DESC LIMIT 1
            """,
            (year, score, score),
        ).fetchone()
        if row:
            return int(row["cumulative_count"])
        # 分数高于表内最高段：即位次 1 附近
        top = conn.execute(
            "SELECT MIN(cumulative_count) AS c FROM score_rank WHERE year = ?", (year,)
        ).fetchone()
        if top and top["c"] is not None and score > 0:
            highest = conn.execute(
                "SELECT MAX(score_high) AS s FROM score_rank WHERE year = ?", (year,)
            ).fetchone()
            if highest and score > (highest["s"] or 0):
                return int(top["c"])
    return None
