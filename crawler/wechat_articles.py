"""公众号 / 新闻文章采集与抽取（唯一需要大模型兜底的链路）。

这是成本控制的**第三原则**的落点：先用 SHA-256 判断内容有没有变，
只有"新出现或发生变更"的文章才会调用 DeepSeek Flash，避免重复烧钱。

阶段划分
--------
discover : 从源列表页挑出文章链接 → 登记 extract 任务
fetch    : 抓正文 → 内容指纹判定 → 变更加入抽取代办
extract  : 调 DeepSeek Flash（JSON Output）→ 写 difficulty_notes / admissions
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from config import settings
from crawler.base import HttpClient, run_parallel
from crawler.selector import element_text
from crawler.school_sites import _visible_text
from crawler.store import ContentStore, TaskQueue
from db import upsert
from pipeline.normalize import normalize_school_name, sanitize_scores

TARGET = "article"

#: 公众号文章正文容器
ARTICLE_SELECTORS = ("#js_content", "div.rich_media_content", "article", "div.content")

_LINK_RE = re.compile(r'<a[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<text>.*?)</a>', re.S | re.I)


@dataclass
class ArticleLink:
    url: str
    title: str
    source: str = ""


def extract_article_links(html: str, source: str = "") -> list[ArticleLink]:
    from urllib.parse import urljoin

    out: list[ArticleLink] = []
    seen: set[str] = set()
    for m in _LINK_RE.finditer(html):
        href = m.group("href")
        if "mp.weixin.qq.com" not in href:
            continue
        url = urljoin("https://mp.weixin.qq.com/", href)
        if url in seen:
            continue
        title = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", m.group("text")))
        if len(title) < 4:
            continue
        seen.add(url)
        out.append(ArticleLink(url=url, title=title, source=source))
    return out


def extract_article_body(html: str) -> str:
    """抽出正文文本。抽不到就用去标签兜底（宁可糙也不能漏内容）。"""
    from crawler import parse

    try:
        page = parse._select(html)  # noqa: SLF001
        for css in ARTICLE_SELECTORS:
            nodes = page.css(css)
            if nodes:
                text = element_text(nodes[0]).strip()
                if len(text) > 120:
                    return text
    except Exception:
        pass
    return _visible_text(html)


#: 判断文章是否值得送大模型：标题/正文命中关键词才抽，省 token
DIFFICULTY_HINTS = ("难度", "均分", "平均分", "难易", "评析", "点评", "试卷分析", "得分")
ADMISSION_HINTS = ("录取", "投档", "最低分", "分数线", "位次", "一分一段")


class WechatCrawler:
    def __init__(self, conn: sqlite3.Connection, client: HttpClient | None = None) -> None:
        self.conn = conn
        self.client = client or HttpClient()
        self.store = ContentStore(conn)
        self.tasks = TaskQueue(conn)
        self._extractor = None

    @property
    def extractor(self):
        if self._extractor is None:
            from extract.deepseek import DeepSeekExtractor

            self._extractor = DeepSeekExtractor(self.conn)
        return self._extractor

    # ------------------------------------------------------------------ discover
    def discover(self, sources: Iterable[dict[str, str]] | None = None) -> list[ArticleLink]:
        srcs = list(sources or settings.WECHAT_SOURCES)
        links: list[ArticleLink] = []
        for src in srcs:
            url = src.get("list_url", "")
            if not url:
                continue
            try:
                page = self.client.get(url)
            except Exception as exc:
                print(f"[wechat] 列表页失败 {url}: {exc}")
                continue
            if not page.ok:
                continue
            links.extend(extract_article_links(page.html, src.get("name", "")))
        dedup: dict[str, ArticleLink] = {a.url: a for a in links}
        for art in dedup.values():
            self.tasks.enqueue("extract", TARGET, art.url, {"url": art.url, "title": art.title})
        return list(dedup.values())

    # ------------------------------------------------------------------ fetch + extract
    def process(self, url: str, title: str = "") -> dict[str, int]:
        stat = {"difficulty": 0, "admissions": 0, "llm_calls": 0, "skipped": 0}

        page = self.client.get(url)
        if not page.ok:
            raise RuntimeError(f"HTTP {page.status}")

        change = self.store.ingest(url, page.html, page.status)
        if not change.changed:
            stat["skipped"] = 1
            return stat

        body = extract_article_body(page.html)
        if len(body) < 200:
            raise RuntimeError("正文过短，可能未取到内容")

        year = self._guess_year(title + body)
        haystack = title + body[:2000]

        if self.extractor.available and any(h in haystack for h in DIFFICULTY_HINTS):
            note = self.extractor.extract_difficulty(body, year)
            stat["llm_calls"] += 1
            if note:
                note.update({
                    "source_title": title,
                    "source_url": url,
                    "source_type": "wechat",
                })
                stat["difficulty"] = upsert(
                    self.conn, "difficulty_notes", [note],
                    ["year", "subject", "source_url"],
                    update_only=[
                        "level", "summary", "evidence", "source_title",
                        "source_type", "confidence",
                    ],
                )

        if self.extractor.available and any(h in haystack for h in ADMISSION_HINTS):
            records = self.extractor.extract_admissions(body, year)
            stat["llm_calls"] += 1
            rows = []
            for rec in records:
                code = self._resolve_school(str(rec.get("school_name") or ""))
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
                        "min_score": _int(rec.get("min_score")),
                        "avg_score": _float(rec.get("avg_score")),
                        "max_score": _int(rec.get("max_score")),
                        "rank_min": _int(rec.get("rank")),
                        "plan_count": _int(rec.get("plan_count")),
                        "score_source": "wechat",
                        "source_url": url,
                    })
                )
            rows = [r for r in rows if r["major_name"]]
            if rows:
                stat["admissions"] = upsert(
                    self.conn, "admissions", rows,
                    ["year", "school_code", "batch", "group_code", "major_code", "major_name"],
                    update_only=[
                        "min_score", "avg_score", "max_score", "rank_min",
                        "plan_count", "score_source", "source_url",
                    ],
                )
        return stat

    # ------------------------------------------------------------------
    def _resolve_school(self, name: str) -> str | None:
        norm = normalize_school_name(name)
        if not norm:
            return None
        row = self.conn.execute(
            "SELECT school_code FROM schools WHERE name_norm = ? LIMIT 1", (norm,)
        ).fetchone()
        return row["school_code"] if row else None

    @staticmethod
    def _guess_year(text: str) -> int:
        years = [int(y) for y in re.findall(r"(20\d{2})", text or "")]
        plausible = [y for y in years if y in settings.TARGET_YEARS or y == settings.LATEST_YEAR]
        return max(plausible) if plausible else settings.LATEST_YEAR

    # ------------------------------------------------------------------
    def run(self, sources: Iterable[dict[str, str]] | None = None) -> dict[str, int]:
        self.tasks.recover_running(target=TARGET)
        links = self.discover(sources)
        summary = {
            "articles": len(links), "difficulty": 0, "admissions": 0,
            "llm_calls": 0, "skipped": 0, "failed": 0,
        }
        if not links:
            return summary

        by_url = {a.url: a for a in links}
        pending = self.tasks.claim("extract", TARGET, limit=200)

        def worker(task):
            art = by_url.get(task.task_key)
            if art is None:
                self.tasks.finish(task, "skipped", "not discovered this round")
                return 0
            try:
                stat = self.process(art.url, art.title)
                self.tasks.finish(task, "done")
                return stat
            except Exception as exc:
                self.tasks.finish(task, "failed", str(exc)[:400])
                return exc

        for outcome in run_parallel(pending, worker, concurrency=min(3, settings.MAX_CONCURRENCY)):
            if isinstance(outcome, Exception):
                summary["failed"] += 1
            elif isinstance(outcome, dict):
                for key in ("difficulty", "admissions", "llm_calls", "skipped"):
                    summary[key] += outcome[key]
        return summary


def _int(value: Any) -> int | None:
    from crawler.parse import to_int

    return to_int(value if isinstance(value, str) else (str(value) if value is not None else None))


def _float(value: Any) -> float | None:
    from crawler.parse import to_float

    return to_float(value if isinstance(value, str) else (str(value) if value is not None else None))


def add_manual_difficulty(
    conn: sqlite3.Connection,
    year: int,
    level: str,
    source_url: str,
    summary: str = "",
    evidence: str = "",
    subject: str = "全科",
    source_title: str = "",
    source_type: str = "news",
    confidence: float = 1.0,
) -> int:
    """人工录入难度评价（有权威来源时优于让模型猜）。"""
    if level not in ("偏难", "适中", "偏易"):
        raise ValueError("level 必须是 偏难/适中/偏易")
    return upsert(
        conn, "difficulty_notes",
        [{
            "year": year, "level": level, "subject": subject, "summary": summary,
            "evidence": evidence, "source_title": source_title, "source_url": source_url,
            "source_type": source_type, "confidence": confidence,
        }],
        ["year", "subject", "source_url"],
        update_only=["level", "summary", "evidence", "source_title", "source_type", "confidence"],
    )
