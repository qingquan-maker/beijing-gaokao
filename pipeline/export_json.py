"""把 SQLite 导出成前端直接 fetch 的静态 JSON。

为什么用「列定义 + 行数组」而不是对象数组
----------------------------------------
一行 19 个字段的对象会把键名重复写几万遍。改成 `{"columns":[...], "rows":[[...]]}`
后，10 万行数据大约能省掉 30%~40% 的体积，而且列名只出现一次、前端映射更直观。
前端 app.js 里有对应的 `toObjects()` 还原逻辑。

产物
----
web/data/index.json          元信息（年份、列定义、难度评价、数据版本）
web/data/admissions-{年}.json 按年分片，前端只加载选中的那一年
web/data/score_rank-{年}.json 一分一段表（用于校验/展示位次口径）
web/data/data.js             全量内联包，供 file:// 直接双击打开页面时使用
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from config import settings

#: 前端表格列（顺序即文件中的列顺序）
ADMISSION_COLUMNS: tuple[str, ...] = (
    "year",
    "school_code",
    "school_name",
    "province",
    "batch",
    "group_code",
    "group_label",
    "subject_req",
    "major_code",
    "major_name",
    "min_score",
    "avg_score",
    "max_score",
    "rank_min",
    "plan_count",
    "difficulty",
    "duration",
    "tuition",
    "source_url",
    "score_source",
)

SOURCES = [
    {
        "name": "北京教育考试院 · 综合查询系统（招生计划）",
        "url": "http://query.bjeea.cn/queryService/rest/plan/115",
    },
    {"name": "北京教育考试院 · 高考高招通知公告（一分一段表）", "url": settings.BJEAA_GKGZ_INDEX},
    {"name": "阳光高考平台", "url": f"{settings.CHSI_BASE}/"},
]


def _json_dump(path: Path, payload: Any, indent: int | None = None) -> int:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":") if indent is None else None,
                      indent=indent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def _difficulty_map(conn: sqlite3.Connection) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    rows = conn.execute(
        """SELECT year, level, subject, summary, evidence, source_title, source_url,
                  source_type, confidence
             FROM difficulty_notes
            ORDER BY year DESC, confidence DESC"""
    ).fetchall()
    for r in rows:
        out.setdefault(int(r["year"]), []).append({
            "level": r["level"],
            "subject": r["subject"],
            "summary": r["summary"] or "",
            "evidence": r["evidence"] or "",
            "source_title": r["source_title"] or "",
            "source_url": r["source_url"],
            "source_type": r["source_type"] or "",
            "confidence": r["confidence"],
        })
    return out


def _primary_difficulty(notes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """取一条作为该年份的整体标注：优先"全科"，其次确信度最高。"""
    if not notes:
        return None
    whole = [n for n in notes if n.get("subject") == "全科"]
    return (whole or notes)[0]


def export(
    conn: sqlite3.Connection,
    out_dir: Path | None = None,
    *,
    inline_bundle: bool = True,
    bundle_size_limit_mb: float = 8.0,
) -> dict[str, Any]:
    out_dir = out_dir or settings.WEB_DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    difficulty = _difficulty_map(conn)
    years = [
        int(r["year"])
        for r in conn.execute("SELECT DISTINCT year FROM admissions ORDER BY year DESC")
    ]

    # 难度按年份标注到每一行，前端无需再查一次
    year_level = {
        y: (_primary_difficulty(difficulty.get(y, [])) or {}).get("level")
        for y in years
    }

    rows = conn.execute(
        """SELECT year, school_code, school_name, province, batch,
                  group_code, group_label, subject_req, major_code, major_name,
                  min_score, avg_score, max_score, rank_min, plan_count,
                  duration, tuition, source_url, score_source
             FROM v_admission_export
            ORDER BY year DESC, school_name, batch, group_code, major_code, major_name"""
    ).fetchall()

    by_year: dict[int, list[list[Any]]] = {y: [] for y in years}
    for r in rows:
        year = int(r["year"])
        by_year.setdefault(year, []).append([
            year,
            r["school_code"],
            r["school_name"],
            r["province"] or "",
            r["batch"] or "",
            r["group_code"] or "",
            r["group_label"] or "",
            r["subject_req"] or "",
            r["major_code"] or "",
            r["major_name"] or "",
            r["min_score"],
            r["avg_score"],
            r["max_score"],
            r["rank_min"],
            r["plan_count"],
            year_level.get(year),
            r["duration"] or "",
            r["tuition"] or "",
            r["source_url"] or "",
            r["score_source"] or "",
        ])

    written: dict[str, int] = {}
    bundle: dict[str, Any] = {"columns": list(ADMISSION_COLUMNS), "years": {}}

    for year in years:
        payload = {
            "year": year,
            "columns": list(ADMISSION_COLUMNS),
            "rows": by_year.get(year, []),
            "difficulty": difficulty.get(year, []),
        }
        written[f"admissions-{year}.json"] = _json_dump(out_dir / f"admissions-{year}.json", payload)
        bundle["years"][str(year)] = payload

        rank_rows = conn.execute(
            """SELECT score_low, score_high, segment_count, cumulative_count, source_url
                 FROM score_rank WHERE year = ? ORDER BY score_high DESC""",
            (year,),
        ).fetchall()
        if rank_rows:
            rank_payload = {
                "year": year,
                "columns": ["score_low", "score_high", "segment_count", "cumulative_count", "source_url"],
                "rows": [[r["score_low"], r["score_high"], r["segment_count"],
                          r["cumulative_count"], r["source_url"] or ""] for r in rank_rows],
            }
            written[f"score_rank-{year}.json"] = _json_dump(
                out_dir / f"score_rank-{year}.json", rank_payload
            )

    counts = {
        "schools": conn.execute("SELECT COUNT(*) AS n FROM schools").fetchone()["n"],
        "programs": conn.execute("SELECT COUNT(*) AS n FROM programs").fetchone()["n"],
        "admissions": conn.execute("SELECT COUNT(*) AS n FROM admissions").fetchone()["n"],
        "with_scores": conn.execute(
            "SELECT COUNT(*) AS n FROM admissions WHERE min_score IS NOT NULL"
        ).fetchone()["n"],
        "with_rank": conn.execute(
            "SELECT COUNT(*) AS n FROM admissions WHERE rank_min IS NOT NULL"
        ).fetchone()["n"],
    }

    data_version = hashlib.sha256(
        json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]

    # 哪些年份掺了示例数据 —— 前端要显著提示，避免被当成真实录取分
    demo_years = sorted(
        {int(r["year"]) for r in rows if (r["score_source"] or "") == "demo"},
        reverse=True,
    )

    index = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_version": data_version,
        "latest_year": years[0] if years else settings.LATEST_YEAR,
        "years": years,
        "columns": list(ADMISSION_COLUMNS),
        "counts": counts,
        "demo_years": demo_years,
        "difficulty": {
            str(y): {
                "primary": _primary_difficulty(difficulty.get(y, [])),
                "all": difficulty.get(y, []),
            }
            for y in years
        },
        "sources": SOURCES,
        # 位次口径说明：如实告知用户位次是由一分一段表换算得来
        "rank_note": (
            "位次由当年一分一段表的累计人数换算得到；官方公布的尾部为合并区间"
            "（如 120-129），落在区间内的分数只能取该段累计人数，属官方口径下的近似值。"
        ),
    }
    written["index.json"] = _json_dump(out_dir / "index.json", index, indent=2)

    bundle["index"] = index
    bundle_bytes = len(json.dumps(bundle, ensure_ascii=False).encode("utf-8"))
    if inline_bundle and bundle_bytes <= bundle_size_limit_mb * 1024 * 1024:
        js = "window.__GAOKAO_DATA__ = " + json.dumps(bundle, ensure_ascii=False) + ";\n"
        (out_dir / "data.js").write_text(js, encoding="utf-8")
        written["data.js"] = len(js.encode("utf-8"))
    elif inline_bundle:
        print(
            f"[export] 数据总量 {bundle_bytes/1024/1024:.1f}MB 超过内联上限，"
            f"跳过 data.js（请通过 HTTP 服务访问，不要双击打开 index.html）"
        )

    return {
        "out_dir": str(out_dir),
        "years": years,
        "latest_year": index["latest_year"],
        "data_version": data_version,
        "counts": counts,
        "files": written,
    }


def export_from_db(db_path: Path | None = None, out_dir: Path | None = None, **kw: Any) -> dict[str, Any]:
    from db import connect

    conn = connect(db_path, init=False)
    try:
        return export(conn, out_dir, **kw)
    finally:
        conn.close()
