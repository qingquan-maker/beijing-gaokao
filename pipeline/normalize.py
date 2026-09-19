"""清洗与派生计算：名称归一化、分数校验、位次换算、标签富化。"""

from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path

from config import settings

# --------------------------------------------------------------------------- 名称

_PUNCT = str.maketrans({
    "（": "(", "）": ")", "　": "", "，": ",", "、": ",",
    "：": ":", "－": "-", "—": "-", "～": "~", "．": ".",
})


def normalize_school_name(name: str | None) -> str:
    """归一化校名，用于跨源匹配（考试院 ↔ 高校招生网 ↔ 公众号）。

    只统一全/半角与空白，**不**删除 "(北京)" 之类的限定词 ——
    「华北电力大学(北京)」和「华北电力大学」是两所学校，误合并会污染数据。
    """
    if not name:
        return ""
    text = name.translate(_PUNCT)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[·•]", "", text)
    return text.strip().lower()


def strip_batch_noise(batch: str | None) -> str:
    if not batch:
        return ""
    return re.sub(r"\s+", "", batch)


# --------------------------------------------------------------------------- 标签（可选）

def load_school_tags(path: Path | None = None) -> dict[str, str]:
    """从 data/seed/school_tags.csv 读取 {归一化校名: '985,211,双一流'}。

    该文件是可选的：没有就跳过标签富化，绝不用臆测数据填表。
    CSV 表头要求：school_name,tags
    """
    path = path or (settings.SEED_DIR / "school_tags.csv")
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("school_name") or "").strip()
            tags = (row.get("tags") or "").strip()
            if name and tags:
                out[normalize_school_name(name)] = tags
    return out


def enrich_tags(conn: sqlite3.Connection) -> int:
    """把 CSV 标签刷进 schools.tags。"""
    tags = load_school_tags()
    if not tags:
        return 0
    updated = 0
    for row in conn.execute("SELECT school_code, name_norm FROM schools").fetchall():
        tag = tags.get(row["name_norm"])
        if tag:
            conn.execute(
                "UPDATE schools SET tags = ? WHERE school_code = ?", (tag, row["school_code"])
            )
            updated += 1
    conn.commit()
    return updated


# --------------------------------------------------------------------------- 分数校验

MIN_TOTAL, MAX_TOTAL = 100, 750   # 北京本科总分 750


def valid_score(score: int | float | None) -> bool:
    """分数合理性校验：过滤把「计划数」「学制」「学费」误当分数的解析错误。"""
    if score is None:
        return False
    try:
        value = float(score)
    except (TypeError, ValueError):
        return False
    return MIN_TOTAL <= value <= MAX_TOTAL


def sanitize_scores(row: dict) -> dict:
    """剔除不合理分数，并保证 min <= avg <= max 的常识约束。"""
    for key in ("min_score", "max_score"):
        if key in row and not valid_score(row.get(key)):
            row[key] = None
    if "avg_score" in row and not valid_score(row.get("avg_score")):
        row["avg_score"] = None

    lo, hi, avg = row.get("min_score"), row.get("max_score"), row.get("avg_score")
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
        row["min_score"], row["max_score"] = lo, hi
    if avg is not None:
        if lo is not None and avg < lo:
            avg = float(lo)
        if hi is not None and avg > hi:
            avg = float(hi)
        row["avg_score"] = avg
    return row


# --------------------------------------------------------------------------- 位次换算

RANK_UPDATE_SQL = """
UPDATE admissions
   SET rank_min = (
         SELECT r.cumulative_count FROM score_rank r
          WHERE r.year = admissions.year
            AND r.score_low <= admissions.min_score
            AND admissions.min_score <= r.score_high
          LIMIT 1),
       rank_avg = (
         SELECT r.cumulative_count FROM score_rank r
          WHERE r.year = admissions.year
            AND r.score_low <= CAST(ROUND(admissions.avg_score) AS INTEGER)
            AND CAST(ROUND(admissions.avg_score) AS INTEGER) <= r.score_high
          LIMIT 1),
       rank_max = (
         SELECT r.cumulative_count FROM score_rank r
          WHERE r.year = admissions.year
            AND r.score_low <= admissions.max_score
            AND admissions.max_score <= r.score_high
          LIMIT 1)
 WHERE year = ?
   AND (min_score IS NOT NULL OR avg_score IS NOT NULL OR max_score IS NOT NULL)
"""


def compute_ranks(conn: sqlite3.Connection, year: int | None = None) -> int:
    """用 score_rank 由分数换算位次，写入 admissions.rank_*。

    位次不直接采信外部文本，而是统一由一分一段表换算，保证口径一致。
    """
    years = [year] if year else [
        r["year"] for r in conn.execute("SELECT DISTINCT year FROM score_rank ORDER BY year")
    ]
    total = 0
    for y in years:
        has_rank = conn.execute(
            "SELECT 1 FROM score_rank WHERE year = ? LIMIT 1", (y,)
        ).fetchone()
        if not has_rank:
            continue
        cur = conn.execute(RANK_UPDATE_SQL, (y,))
        total += cur.rowcount
    conn.commit()
    return total


def link_programs(conn: sqlite3.Connection) -> int:
    """把 admissions.program_id 关联到对应的 programs 行。"""
    cur = conn.execute(
        """
        UPDATE admissions
           SET program_id = (
                 SELECT p.program_id FROM programs p
                  WHERE p.year        = admissions.year
                    AND p.school_code = admissions.school_code
                    AND p.batch       = admissions.batch
                    AND p.group_code  = admissions.group_code
                    AND p.major_code  = admissions.major_code
                    AND p.major_name  = admissions.major_name
                  LIMIT 1)
         WHERE program_id IS NULL
        """
    )
    conn.commit()
    return cur.rowcount


def load_score_rank_csv(conn: sqlite3.Connection, path: Path | None = None) -> int:
    """导入一分一段种子 CSV（year,score_low,score_high,segment_count,cumulative_count）。"""
    path = path or (settings.SEED_DIR / "score_rank_2025.csv")
    if not path.exists():
        return 0
    rows: list[dict] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                rows.append({
                    "year": int(row["year"]),
                    "score_low": int(row["score_low"]),
                    "score_high": int(row["score_high"]),
                    "segment_count": int(row["segment_count"]) if row.get("segment_count") else None,
                    "cumulative_count": int(row["cumulative_count"]),
                    "source_url": row.get("source_url") or "",
                })
            except (KeyError, TypeError, ValueError):
                continue
    if not rows:
        return 0
    conn.executemany(
        """INSERT INTO score_rank
             (year, score_low, score_high, segment_count, cumulative_count, source_url)
           VALUES (:year, :score_low, :score_high, :segment_count, :cumulative_count, :source_url)
           ON CONFLICT (year, score_low) DO UPDATE SET
             score_high = excluded.score_high,
             segment_count = COALESCE(excluded.segment_count, score_rank.segment_count),
             cumulative_count = excluded.cumulative_count,
             source_url = COALESCE(NULLIF(excluded.source_url,''), score_rank.source_url)""",
        rows,
    )
    conn.commit()
    return len(rows)
