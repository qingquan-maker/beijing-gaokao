"""各高校本科招生网采集器（配置驱动，按学校分别设置采集计划）。

为什么不做「一次性全量抓取」
--------------------------
各高校录取数据的**发布时间不统一**（有的 7 月初，有的 8 月底，还有的只发到
公众号），页面结构更是千校千面。因此这里采用「学校源注册表 + 两种策略」：

  strategy = "table" : 招生网有结构化表格 → 通用表头别名映射，零 API 成本
  strategy = "llm"   : 只有新闻稿/富文本     → 交给 DeepSeek Flash 抽取

新增一所学校只要注册一条 SchoolSource（或写进 data/seed/school_sources.csv），
不必改代码。没注册的学校不会被抓，避免对目标站点做无意义的全站扫描。
"""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from config import settings
from crawler import parse
from crawler.base import HttpClient, run_parallel
from crawler.selector import element_text
from crawler.store import ContentStore, TaskQueue
from db import upsert
from pipeline.normalize import normalize_school_name, sanitize_scores

TARGET = "school_site"

# --------------------------------------------------------------------------- 源定义

@dataclass
class SchoolSource:
    school_code: str          # 考试院院校代码；未知可留空，靠校名匹配
    school_name: str
    admissions_url: str       # 录取分数发布页
    strategy: str = "table"   # table | llm
    year_hint: int | None = None
    note: str = ""


#: 内置注册表。留空表示"本轮不抓高校官网"，此时录取分数只能来自
#: 考试院投档线或公众号抽取 —— 这是有意为之的默认值，不是遗漏。
BUILTIN_SOURCES: tuple[SchoolSource, ...] = ()


def load_sources(path: Any = None) -> list[SchoolSource]:
    """读取注册表：内置 + data/seed/school_sources.csv（可选）。"""
    path = path or (settings.SEED_DIR / "school_sources.csv")
    sources: list[SchoolSource] = list(BUILTIN_SOURCES)
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                url = (row.get("admissions_url") or "").strip()
                name = (row.get("school_name") or "").strip()
                if not url or not name:
                    continue
                try:
                    year_hint = int(row.get("year_hint") or 0) or None
                except ValueError:
                    year_hint = None
                sources.append(
                    SchoolSource(
                        school_code=(row.get("school_code") or "").strip(),
                        school_name=name,
                        admissions_url=url,
                        strategy=(row.get("strategy") or "table").strip().lower(),
                        year_hint=year_hint,
                        note=(row.get("note") or "").strip(),
                    )
                )
    return sources


# --------------------------------------------------------------------------- 通用表格映射

#: 表头别名 → 字段。命中即映射，顺序即优先级。
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "major_name": ("专业名称", "招生专业", "专业", "专业(类)", "专业类"),
    "major_code": ("专业代码", "专业代号"),
    "group_code": ("专业组代码", "院校专业组", "专业组", "组别"),
    "batch": ("录取批次", "批次"),
    "min_score": ("最低分", "录取最低分", "投档最低分", "最低录取分"),
    "avg_score": ("平均分", "录取平均分", "平均"),
    "max_score": ("最高分", "录取最高分", "最高"),
    "rank": ("最低位次", "位次", "最低排名", "排名"),
    "plan_count": ("计划招生数", "计划数", "招生计划", "计划"),
    "admit_count": ("录取人数", "录取数"),
}

_GROUP_RE = re.compile(r"^\{?\s*([0-9A-Za-z]{1,4})\s*\}?\s*(.*)$", re.S)

#: 「录取概况」表的表头别名。这类表是院校/科类级的，和上面的专业级表分开解析。
SUMMARY_ALIASES: dict[str, tuple[str, ...]] = {
    "year": ("年份", "年度", "招生年份"),
    "province": ("省市", "省份", "生源省份", "招生省份", "生源地"),
    "subject_type": ("科类", "选考科目", "文理科"),
    "batch_type": ("类型", "招生类型", "计划类型"),
    "min_score": ("最低分", "录取最低分"),
    "avg_score": ("平均分", "录取平均分"),
    "max_score": ("最高分", "录取最高分"),
    "control_line": ("控制线", "省控线", "批次线", "录取控制分数线"),
}


def _match_column(header: str) -> str | None:
    squeezed = re.sub(r"\s+", "", header)
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if squeezed == alias or alias in squeezed:
                return field
    return None


def _match_summary_column(header: str) -> str | None:
    squeezed = re.sub(r"\s+", "", header)
    for field, aliases in SUMMARY_ALIASES.items():
        for alias in aliases:
            if squeezed == alias or alias in squeezed:
                return field
    return None


def parse_summary_table(html: str) -> list[dict[str, Any]]:
    """解析「录取概况」表（年份 / 省市 / 科类 / 类型 / 最低分 / 平均分 / 控制线）。

    与专业级表格的区分条件：必须有最低分，且带控制线或省市 ——
    专业级表格没有控制线，也不会按省市分行。
    """
    page = parse._select(html)  # noqa: SLF001
    best: list[dict[str, Any]] = []

    for table in page.css("table"):
        trs = table.css("tr")
        if len(trs) < 2:
            continue
        header_cells = [parse.clean_text(element_text(c)) for c in trs[0].css("td, th")]
        if len(header_cells) < 3:
            continue
        mapping: dict[int, str] = {}
        for idx, head in enumerate(header_cells):
            field = _match_summary_column(head)
            if field and field not in mapping.values():
                mapping[idx] = field
        fields = set(mapping.values())
        if "min_score" not in fields:
            continue
        if not ({"control_line", "province"} & fields):
            continue

        rows: list[dict[str, Any]] = []
        for tr in trs[1:]:
            cells = [parse.clean_text(element_text(c)) for c in tr.css("td, th")]
            if len(cells) < 2:
                continue
            row: dict[str, Any] = {}
            for idx, field in mapping.items():
                if idx < len(cells):
                    row[field] = cells[idx]
            if not row.get("min_score"):
                continue
            rows.append(row)
        if len(rows) > len(best):
            best = rows
    return best


def load_summary_rows(
    conn: sqlite3.Connection,
    school_code: str,
    rows: Iterable[dict[str, Any]],
    source_url: str,
    default_year: int,
) -> int:
    """写入 school_summaries（院校级录取概况 + 批次控制线）。"""
    payload: list[dict[str, Any]] = []
    for raw in rows:
        year = parse.to_int(raw.get("year")) or default_year
        if not (2000 <= int(year) <= 2100):
            year = default_year
        min_score = parse.to_int(raw.get("min_score"))
        if min_score is None or not (100 <= min_score <= 750):
            continue
        payload.append({
            "year": int(year),
            "school_code": school_code,
            "province": parse.clean_text(raw.get("province") or ""),
            "subject_type": parse.clean_text(raw.get("subject_type") or ""),
            "batch_type": parse.clean_text(raw.get("batch_type") or ""),
            "min_score": min_score,
            "avg_score": parse.to_float(raw.get("avg_score")),
            "max_score": parse.to_int(raw.get("max_score")),
            "control_line": parse.to_int(raw.get("control_line")),
            "source_url": source_url,
            "source_kind": "school_site",
        })
    if not payload:
        return 0
    return upsert(
        conn, "school_summaries", payload,
        ["year", "school_code", "province", "subject_type", "batch_type"],
        update_only=["min_score", "avg_score", "max_score", "control_line",
                     "source_url", "source_kind"],
    )


def parse_generic_admission_table(html: str) -> list[dict[str, Any]]:
    """通用录取分数表解析：靠表头别名映射，不依赖固定列序。

    高校招生网表格样式五花八门，但表头用词高度收敛，因此用别名映射比写
    每校一套 XPath 更省维护成本。
    """
    page = parse._select(html)  # noqa: SLF001 - 复用同一 Selector 构造逻辑
    best: list[dict[str, Any]] = []

    for table in page.css("table"):
        trs = table.css("tr")
        if len(trs) < 3:
            continue
        header_cells = [parse.clean_text(element_text(c)) for c in trs[0].css("td, th")]
        if len(header_cells) < 3:
            continue
        mapping: dict[int, str] = {}
        for idx, head in enumerate(header_cells):
            field = _match_column(head)
            if field and field not in mapping.values():
                mapping[idx] = field
        # 至少要能认出专业名 + 一个分数列，才算录取分数表
        if "major_name" not in mapping.values():
            continue
        if not ({"min_score", "avg_score", "max_score"} & set(mapping.values())):
            continue

        rows: list[dict[str, Any]] = []
        for tr in trs[1:]:
            cells = [parse.clean_text(element_text(c)) for c in tr.css("td, th")]
            if len(cells) < 2:
                continue
            row: dict[str, Any] = {}
            for idx, field in mapping.items():
                if idx < len(cells):
                    row[field] = cells[idx]
            if not row.get("major_name"):
                continue
            rows.append(row)
        if len(rows) > len(best):
            best = rows
    return best


def _coerce(raw: dict[str, Any], year: int, source_url: str) -> dict[str, Any]:
    """把字符串行转成 admissions 的列。"""
    group_code = parse.clean_text(raw.get("group_code") or "")
    group_label = ""
    if group_code:
        m = _GROUP_RE.match(group_code)
        if m:
            group_code, group_label = m.group(1), parse.clean_text(m.group(2))
    row: dict[str, Any] = {
        "year": year,
        "school_code": raw.get("school_code") or "",
        "batch": parse.clean_text(raw.get("batch") or ""),
        "group_code": group_code,
        "major_code": parse.clean_text(raw.get("major_code") or ""),
        "major_name": parse.clean_text(raw.get("major_name") or ""),
        "min_score": parse.to_int(raw.get("min_score")),
        "avg_score": parse.to_float(raw.get("avg_score")),
        "max_score": parse.to_int(raw.get("max_score")),
        "rank_min": parse.to_int(raw.get("rank")),
        "plan_count": parse.to_int(raw.get("plan_count")),
        "admit_count": parse.to_int(raw.get("admit_count")),
        "score_source": "school",
        "source_url": source_url,
    }
    if group_label:
        row["group_label"] = group_label
    return sanitize_scores(row)


# --------------------------------------------------------------------------- 采集器

class SchoolSiteCrawler:
    def __init__(self, conn: sqlite3.Connection, client: HttpClient | None = None) -> None:
        self.conn = conn
        self.client = client or HttpClient()
        self.store = ContentStore(conn)
        self.tasks = TaskQueue(conn)
        self._extractor = None
        self._render_client = None

    @property
    def extractor(self):
        if self._extractor is None:
            from extract.deepseek import DeepSeekExtractor

            self._extractor = DeepSeekExtractor(self.conn)
        return self._extractor

    @property
    def render_client(self):
        """渲染用客户端：录取概况页基本是 JS 单页应用，必须走真实浏览器。"""
        if self._render_client is None:
            self._render_client = HttpClient(render=True)
        return self._render_client

    # ------------------------------------------------------------------
    def plan(self, sources: Iterable[SchoolSource] | None = None) -> list[SchoolSource]:
        """把注册表登记为任务（按学校粒度，便于分批/续爬）。"""
        srcs = list(sources if sources is not None else load_sources())
        for src in srcs:
            self.tasks.enqueue(
                "fetch", TARGET, f"{src.school_code or src.school_name}",
                {
                    "school_code": src.school_code,
                    "school_name": src.school_name,
                    "url": src.admissions_url,
                    "strategy": src.strategy,
                    "year_hint": src.year_hint,
                },
            )
        return srcs

    # ------------------------------------------------------------------
    def resolve_school_code(self, name: str, hint: str = "") -> str | None:
        if hint:
            row = self.conn.execute(
                "SELECT school_code FROM schools WHERE school_code = ?", (hint,)
            ).fetchone()
            if row:
                return row["school_code"]
        norm = normalize_school_name(name)
        if not norm:
            return None
        row = self.conn.execute(
            "SELECT school_code FROM schools WHERE name_norm = ? LIMIT 1", (norm,)
        ).fetchone()
        return row["school_code"] if row else None

    # ------------------------------------------------------------------
    def fetch_source(self, src: SchoolSource) -> dict[str, int]:
        """抓一个学校源并入库。返回统计。"""
        stat = {"rows": 0, "llm_calls": 0, "skipped": 0}
        client = self.render_client if src.strategy == "summary" else self.client
        try:
            page = client.get(src.admissions_url)
        except Exception as exc:
            # 渲染不可用（例如 CI 里没装浏览器内核）时退回静态抓取：
            # JS 页面会拿到空壳、解析出 0 行，这比整个任务判失败更诚实。
            if src.strategy != "summary":
                raise
            print(f"[school] 渲染失败，退回静态抓取 {src.admissions_url}: {str(exc)[:120]}")
            page = self.client.get(src.admissions_url)
        if not page.ok:
            raise RuntimeError(f"HTTP {page.status}")

        change = self.store.ingest(src.admissions_url, page.html, page.status)
        if not change.changed:
            stat["skipped"] = 1
            return stat

        year = src.year_hint or parse.parse_title_year(page.html) or settings.LATEST_YEAR
        school_code = self.resolve_school_code(src.school_name, src.school_code)

        if src.strategy == "summary":
            if not school_code:
                raise RuntimeError(f"库中找不到学校「{src.school_name}」")
            rows_summary = parse_summary_table(page.html)
            stat["rows"] = load_summary_rows(
                self.conn, school_code, rows_summary, src.admissions_url, year
            )
            return stat

        if src.strategy == "llm":
            records = self.extractor.extract_admissions(_visible_text(page.html), year)
            stat["llm_calls"] = 1
            rows = []
            for rec in records:
                code = self.resolve_school_code(
                    str(rec.get("school_name") or src.school_name), school_code or ""
                )
                if not code:
                    continue
                rows.append(
                    sanitize_scores({
                        "year": int(rec.get("year") or year),
                        "school_code": code,
                        "batch": (rec.get("batch") or "").strip(),
                        "group_code": (rec.get("group_code") or "").strip(),
                        "major_code": "",
                        "major_name": (rec.get("major_name") or "").strip(),
                        "min_score": parse.to_int(rec.get("min_score")),
                        "avg_score": parse.to_float(rec.get("avg_score")),
                        "max_score": parse.to_int(rec.get("max_score")),
                        "rank_min": parse.to_int(rec.get("rank")),
                        "plan_count": parse.to_int(rec.get("plan_count")),
                        "score_source": "wechat" if rec.get("_from_wechat") else "school",
                        "source_url": src.admissions_url,
                    })
                )
        else:
            if not school_code:
                raise RuntimeError(f"库中找不到学校「{src.school_name}」，先跑考试院采集建立学校主数据")
            rows = [
                _coerce({**raw, "school_code": school_code}, year, src.admissions_url)
                for raw in parse_generic_admission_table(page.html)
            ]

        rows = [r for r in rows if r.get("major_name")]
        stat["rows"] = upsert(
            self.conn, "admissions", rows,
            ["year", "school_code", "batch", "group_code", "major_code", "major_name"],
            update_only=[
                "min_score", "avg_score", "max_score", "rank_min",
                "plan_count", "admit_count", "score_source", "source_url",
            ],
        )
        return stat

    # ------------------------------------------------------------------
    def run(self, sources: Iterable[SchoolSource] | None = None) -> dict[str, int]:
        self.tasks.recover_running(target=TARGET)
        srcs = list(sources if sources is not None else load_sources())
        if not srcs:
            return {"sources": 0, "rows": 0, "failed": 0, "note": "未配置任何高校源"}
        self.plan(srcs)
        summary = {"sources": len(srcs), "rows": 0, "failed": 0, "llm_calls": 0}

        by_key = {f"{s.school_code or s.school_name}": s for s in srcs}
        pending = self.tasks.claim("fetch", TARGET, limit=500)

        def worker(task):
            src = by_key.get(task.task_key)
            if src is None:
                self.tasks.finish(task, "skipped", "source not in registry")
                return 0
            try:
                stat = self.fetch_source(src)
                self.tasks.finish(task, "done")
                return stat
            except Exception as exc:
                self.tasks.finish(task, "failed", str(exc)[:400])
                return exc

        for outcome in run_parallel(pending, worker, concurrency=min(3, settings.MAX_CONCURRENCY)):
            if isinstance(outcome, Exception):
                summary["failed"] += 1
            elif isinstance(outcome, dict):
                summary["rows"] += outcome["rows"]
                summary["llm_calls"] += outcome["llm_calls"]
        return summary


def _html_to_text(html: str) -> str:
    """极简正文抽取：去掉 script/style/标签，压缩空白。"""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _visible_text(html: str) -> str:
    """正文抽取：优先用 Scrapling 的文本聚合，失败退回正则清洗。

    送进大模型前一定要去掉 script/style 与标签，否则白烧 token。
    """
    try:
        getter = getattr(parse._select(html), "get_all_text", None)  # noqa: SLF001
        if callable(getter):
            text = getter()
            if text and text.strip():
                return text.strip()
    except Exception:
        pass
    return _html_to_text(html)
