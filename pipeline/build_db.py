"""入库与派生计算：把采集结果整理成查询友好的库。

顺序很重要：
  1. 先有 schools（考试院列表页提供主数据）
  2. 再有 score_rank（位次基准），否则算不出位次
  3. 然后 programs / admissions 增量 UPSERT
  4. 最后统一派生：位次换算、program 关联、位次校验
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from config import settings
from crawler import parse
from db import connect, rank_for_score, upsert
from pipeline.normalize import (
    compute_ranks,
    enrich_tags,
    link_programs,
    load_score_rank_csv,
    normalize_school_name,
    sanitize_scores,
)


def ensure_score_rank(conn: sqlite3.Connection) -> int:
    """确保位次基准表就绪：先试在线抓取，失败退回 data/seed 的 CSV。"""
    have = conn.execute("SELECT COUNT(*) AS n FROM score_rank").fetchone()["n"]
    if have:
        return 0
    try:
        from crawler.bjeea_score_rank import ScoreRankCrawler

        summary = ScoreRankCrawler(conn).run()
        if summary.get("segments"):
            return int(summary["segments"])
    except Exception as exc:
        print(f"[build] 一分一段在线抓取不可用（{exc}），改用种子 CSV")
    total = 0
    for path in sorted(settings.SEED_DIR.glob("score_rank_*.csv")):
        total += load_score_rank_csv(conn, path)
    return total


def import_plan_json(conn: sqlite3.Connection, path: Path) -> dict[str, int]:
    """导入离线整理好的招生计划 JSON（无网络环境 / 历史年份回溯用）。

    格式：
    {
      "school": {"school_code": "...", "name": "...", "province": "..."},
      "year": 2024,
      "source_url": "https://...",
      "programs": [
        {"batch": "本科普通批", "group_code": "01", "subject_req": "物理(必须选考)",
         "major_code": "01", "major_name": "计算机科学与技术", "plan_count": 30,
         "duration": "4", "tuition": "5000"}
      ]
    }
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    school = data.get("school") or {}
    code = str(school.get("school_code") or "").strip()
    name = str(school.get("name") or "").strip()
    if not code or not name:
        raise ValueError(f"{path}: 缺少 school.school_code / school.name")
    year = int(data["year"])
    source_url = data.get("source_url") or str(path)

    upsert(conn, "schools", [{
        "school_code": code,
        "name": name,
        "name_norm": normalize_school_name(name),
        "province": school.get("province") or None,
        "source_url": source_url,
    }], ["school_code"], update_only=["name", "name_norm", "province", "source_url"])

    prog_rows: list[dict[str, Any]] = []
    adm_rows: list[dict[str, Any]] = []
    for item in data.get("programs") or []:
        major_name = str(item.get("major_name") or "").strip()
        if not major_name:
            continue
        group_code = str(item.get("group_code") or "").strip()
        subject_req = str(item.get("subject_req") or "").strip()
        batch = str(item.get("batch") or "").strip()
        major_code = str(item.get("major_code") or "").strip()
        prog_rows.append({
            "year": year, "school_code": code, "batch": batch,
            "group_code": group_code, "group_label": subject_req,
            "subject_req": subject_req, "major_code": major_code,
            "major_name": major_name,
            "duration": str(item.get("duration") or "") or None,
            "tuition": str(item.get("tuition") or "") or None,
            "language": str(item.get("language") or "") or None,
            "source_url": source_url,
        })
        score = sanitize_scores({
            "min_score": parse.to_int(_s(item.get("min_score"))),
            "avg_score": parse.to_float(_s(item.get("avg_score"))),
            "max_score": parse.to_int(_s(item.get("max_score"))),
        })
        adm_rows.append({
            "year": year, "school_code": code, "batch": batch,
            "group_code": group_code, "major_code": major_code,
            "major_name": major_name,
            "plan_count": parse.to_int(_s(item.get("plan_count"))),
            "min_score": score.get("min_score"),
            "avg_score": score.get("avg_score"),
            "max_score": score.get("max_score"),
            "score_source": item.get("score_source") or "archive",
            "source_url": source_url,
        })

    n_prog = upsert(
        conn, "programs", prog_rows,
        ["year", "school_code", "batch", "group_code", "major_code", "major_name"],
        update_only=["group_label", "subject_req", "duration", "tuition", "language", "source_url"],
    )
    n_adm = upsert(
        conn, "admissions", adm_rows,
        ["year", "school_code", "batch", "group_code", "major_code", "major_name"],
        update_only=["min_score", "avg_score", "max_score", "plan_count", "score_source", "source_url"],
    )
    return {"programs": n_prog, "admissions": n_adm}


def import_all_archives(conn: sqlite3.Connection, directory: Path | None = None) -> dict[str, int]:
    directory = directory or (settings.DATA_DIR / "archive")
    if not directory.exists():
        return {}
    totals: dict[str, int] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            stat = import_plan_json(conn, path)
        except Exception as exc:
            print(f"[build] 跳过 {path.name}: {exc}")
            continue
        for key, value in stat.items():
            totals[key] = totals.get(key, 0) + value
    return totals


def _s(value: Any) -> str | None:
    return None if value is None else str(value)


# --------------------------------------------------------------------------- 示例数据

def load_demo(conn: sqlite3.Connection, directory: Path | None = None) -> dict[str, int]:
    """导入 data/seed/demo 下的**示例**数据，让前端功能可以离线演示。

    所有行标记 score_source='demo' / source_type='demo'，导出时会在
    index.json 的 demo_years 里列出，前端顶部会显示醒目警示。
    真实数据不要走这条路。
    """
    directory = directory or (settings.SEED_DIR / "demo")
    if not directory.exists():
        return {"archives": 0, "programs": 0, "admissions": 0, "difficulty": 0}

    totals = {"archives": 0, "programs": 0, "admissions": 0, "difficulty": 0}
    for path in sorted(directory.glob("*.json")):
        if path.stem == "difficulty":
            continue
        try:
            stat = import_plan_json(conn, path)
        except Exception as exc:
            print(f"[demo] 跳过 {path.name}: {exc}")
            continue
        totals["archives"] += 1
        totals["programs"] += stat.get("programs", 0)
        totals["admissions"] += stat.get("admissions", 0)

    diff_path = directory / "difficulty.json"
    if diff_path.exists():
        try:
            entries = json.loads(diff_path.read_text(encoding="utf-8"))
        except ValueError:
            entries = []
        rows = []
        for item in entries if isinstance(entries, list) else []:
            level = item.get("level")
            if level not in ("偏难", "适中", "偏易"):
                continue
            rows.append({
                "year": int(item["year"]),
                "level": level,
                "subject": (item.get("subject") or "全科").strip(),
                "summary": item.get("summary") or "",
                "evidence": item.get("evidence") or "",
                "source_title": item.get("source_title") or "",
                "source_url": item["source_url"],
                "source_type": item.get("source_type") or "demo",
                "confidence": item.get("confidence"),
            })
        if rows:
            totals["difficulty"] = upsert(
                conn, "difficulty_notes", rows,
                ["year", "subject", "source_url"],
                update_only=["level", "summary", "evidence", "source_title",
                             "source_type", "confidence"],
            )
    return totals


def clear_demo(conn: sqlite3.Connection) -> dict[str, int]:
    """清空示例数据（score_source='demo' 的录取行与 source_type='demo' 的难度条目）。"""
    removed = {}
    with_write = [
        ("admissions", "DELETE FROM admissions WHERE score_source = 'demo'"),
        ("programs",
         """DELETE FROM programs WHERE program_id NOT IN
              (SELECT program_id FROM admissions WHERE program_id IS NOT NULL)"""),
        ("difficulty_notes", "DELETE FROM difficulty_notes WHERE source_type = 'demo'"),
    ]
    for name, sql in with_write:
        cur = conn.execute(sql)
        removed[name] = cur.rowcount
    conn.commit()
    return removed


def rebuild_derived(conn: sqlite3.Connection) -> dict[str, int]:
    """派生层统一重算。任何数据源写入后都应调用一次。"""
    ranks = compute_ranks(conn)
    linked = link_programs(conn)
    tagged = enrich_tags(conn)
    filled = backfill_ranks_via_lookup(conn)
    return {"ranks": ranks, "linked": linked, "tags": tagged, "rank_fallback": filled}


def backfill_ranks_via_lookup(conn: sqlite3.Connection) -> int:
    """兜底：SQL 关联没算出来的（如 avg_score 为 NULL 但 min 有值），用 Python 侧查表补齐。"""
    rows = conn.execute(
        """SELECT admission_id, year, min_score, avg_score, max_score, rank_min, rank_avg, rank_max
             FROM admissions
            WHERE (min_score IS NOT NULL AND rank_min IS NULL)
               OR (avg_score IS NOT NULL AND rank_avg IS NULL)
               OR (max_score IS NOT NULL AND rank_max IS NULL)"""
    ).fetchall()
    updates = []
    for row in rows:
        updates.append((
            row["rank_min"] if row["rank_min"] is not None
            else rank_for_score(conn, row["year"], row["min_score"]),
            row["rank_avg"] if row["rank_avg"] is not None
            else rank_for_score(conn, row["year"], int(round(row["avg_score"])) if row["avg_score"] else None),
            row["rank_max"] if row["rank_max"] is not None
            else rank_for_score(conn, row["year"], row["max_score"]),
            row["admission_id"],
        ))
    if updates:
        conn.executemany(
            "UPDATE admissions SET rank_min=?, rank_avg=?, rank_max=? WHERE admission_id=?",
            updates,
        )
        conn.commit()
    return len(updates)


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    def scalar(sql: str, params: Iterable[Any] = ()) -> int:
        row = conn.execute(sql, tuple(params)).fetchone()
        return int(list(row)[0] or 0) if row else 0

    by_year = {
        int(r["year"]): int(r["n"])
        for r in conn.execute(
            "SELECT year, COUNT(*) AS n FROM admissions GROUP BY year ORDER BY year"
        )
    }
    return {
        "schools": scalar("SELECT COUNT(*) FROM schools"),
        "programs": scalar("SELECT COUNT(*) FROM programs"),
        "admissions": scalar("SELECT COUNT(*) FROM admissions"),
        "with_scores": scalar(
            "SELECT COUNT(*) FROM admissions WHERE min_score IS NOT NULL"
        ),
        "with_rank": scalar("SELECT COUNT(*) FROM admissions WHERE rank_min IS NOT NULL"),
        "score_rank_rows": scalar("SELECT COUNT(*) FROM score_rank"),
        "difficulty_notes": scalar("SELECT COUNT(*) FROM difficulty_notes"),
        "by_year": by_year,
    }


def build(
    db_path: Path | None = None,
    *,
    archives: bool = True,
) -> sqlite3.Connection:
    """完整构建：建表 → 位次基准 → 离线档案 → 派生重算。"""
    conn = connect(db_path)
    ensure_score_rank(conn)
    if archives:
        import_all_archives(conn)
    rebuild_derived(conn)
    return conn
