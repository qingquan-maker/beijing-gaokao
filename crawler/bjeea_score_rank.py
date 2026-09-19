"""一分一段表采集器（bjeea.cn「高考高招 > 通知公告」栏目）。

一分一段表每年 6 月下旬随成绩发布，是**位次换算的唯一官方口径**，
所以本模块只负责把表抓回来入库，位次一律由此表换算（见 pipeline/normalize.compute_ranks）。

页面形态：公告正文里挂一张「分数 | 本段人数 | 累计人数」的表格，
尾部若干段是合并区间（如 120-129），解析时保留 [score_low, score_high]。

注意：www.bjeea.cn 使用独立证书链，部分环境（含本项目的开发沙箱）会在 TLS
握手阶段被拦。因此除了在线抓取，本模块还提供 `load_seed()` 走 data/seed 下的
CSV 离线入库，保证位次换算始终可用。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable

from config import settings
from crawler import parse
from crawler.base import HttpClient, run_parallel
from crawler.store import ContentStore, TaskQueue
from pipeline.normalize import load_score_rank_csv

TARGET = "score_rank"

#: 公告标题关键词
KEYWORDS = ("一分一段", "分数分布", "考生分数分布", "高考考生分数分布")

_LINK_RE = re.compile(
    r'<a[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<text>.*?)</a>', re.S | re.I
)

#: 一分一段表常以 PDF 附件形式发布。命中这些后缀时说明公告正文没有数据表，
#: 需要走 PDF 解析（本项目未内置，以免引入重量级依赖）。
_ATTACH_RE = re.compile(r'href="(?P<href>[^"]+\.(?:pdf|xls|xlsx|doc|docx))"', re.I)


@dataclass
class DocLink:
    url: str
    title: str
    year: int | None = None


def guess_year(text: str) -> int | None:
    m = re.search(r"(20\d{2})", text)
    return int(m.group(1)) if m else None


def extract_doc_links(html: str, base: str) -> list[DocLink]:
    """从栏目列表页挑出一分一段相关公告链接。"""
    from urllib.parse import urljoin

    out: list[DocLink] = []
    seen: set[str] = set()
    for m in _LINK_RE.finditer(html):
        title = re.sub(r"<[^>]+>", "", m.group("text"))
        title = re.sub(r"\s+", "", title)
        if not title or not any(k in title for k in KEYWORDS):
            continue
        url = urljoin(base, m.group("href"))
        if url in seen:
            continue
        seen.add(url)
        out.append(DocLink(url=url, title=title, year=guess_year(title) or guess_year(url)))
    return out


class ScoreRankCrawler:
    def __init__(self, conn: sqlite3.Connection, client: HttpClient | None = None) -> None:
        self.conn = conn
        self.client = client or HttpClient()
        self.store = ContentStore(conn)
        self.tasks = TaskQueue(conn)

    # ------------------------------------------------------------------ discover
    def discover(self, list_urls: Iterable[str] | None = None) -> list[DocLink]:
        urls = list(list_urls or self._default_index_urls())
        docs: list[DocLink] = []
        for url in urls:
            try:
                page = self.client.get(url)
            except Exception as exc:
                print(f"[score_rank] 列表页抓取失败 {url}: {exc}")
                continue
            if not page.ok:
                continue
            self.store.ingest(url, page.html, page.status)
            docs.extend(extract_doc_links(page.html, url))
        dedup: dict[str, DocLink] = {d.url: d for d in docs}
        for doc in dedup.values():
            self.tasks.enqueue(
                "fetch", TARGET, doc.url, {"url": doc.url, "title": doc.title, "year": doc.year}
            )
        return list(dedup.values())

    @staticmethod
    def _default_index_urls() -> list[str]:
        """默认只抓「通知公告」第 1 页。

        实测教训：不要臆造分页 URL。考试院栏目的分页命名并不统一
        （/html/gkgz/index_1.html 实测 404），猜出来的地址只会刷一堆 404。
        历史年份的公告请把具体 URL 显式传给 run(list_urls=[...])，
        或走 data/seed 的 CSV / 公众号回溯。
        """
        return [settings.BJEAA_TZGG_INDEX]

    # ------------------------------------------------------------------ fetch + load
    def fetch_document(self, doc: DocLink, default_year: int | None = None) -> int:
        page = self.client.get(doc.url)
        if not page.ok:
            raise RuntimeError(f"HTTP {page.status}")
        change = self.store.ingest(doc.url, page.html, page.status)
        if not change.changed:
            return 0

        rows = parse.parse_score_rank_table(page.html)
        if len(rows) < 5:
            # 常见情况：公告正文没有表格，数据在 PDF/Excel 附件里。
            # 明确报出来，避免误判成"解析器坏了"。
            attaches = _ATTACH_RE.findall(page.html)
            if attaches:
                raise RuntimeError(
                    f"公告正文无数据表，数据在附件中（{len(attaches)} 个，如 {attaches[0]}）；"
                    f"需要 PDF/Excel 解析，请改用 data/seed 的 CSV 或镜像来源"
                )
            raise RuntimeError(f"解析到的分段过少（{len(rows)} 行），页面结构可能已变化")

        year = doc.year or parse.parse_title_year(page.html) or default_year
        if not year:
            raise RuntimeError("无法确定年份")

        payload = [
            {
                "year": year,
                "score_low": r.score_low,
                "score_high": r.score_high,
                "segment_count": r.segment_count,
                "cumulative_count": r.cumulative_count,
                "source_url": doc.url,
            }
            for r in rows
        ]
        self.conn.executemany(
            """INSERT INTO score_rank
                 (year, score_low, score_high, segment_count, cumulative_count, source_url)
               VALUES (:year, :score_low, :score_high, :segment_count, :cumulative_count, :source_url)
               ON CONFLICT (year, score_low) DO UPDATE SET
                 score_high = excluded.score_high,
                 segment_count = COALESCE(excluded.segment_count, score_rank.segment_count),
                 cumulative_count = excluded.cumulative_count,
                 source_url = excluded.source_url""",
            payload,
        )
        self.conn.commit()
        return len(payload)

    def run(self, list_urls: Iterable[str] | None = None) -> dict[str, int]:
        self.tasks.recover_running(target=TARGET)
        docs = self.discover(list_urls)
        summary = {"documents": len(docs), "segments": 0, "failed": 0}
        if not docs:
            return summary

        def worker(task):
            doc = DocLink(
                url=task.payload["url"],
                title=task.payload.get("title", ""),
                year=task.payload.get("year"),
            )
            try:
                n = self.fetch_document(doc)
                self.tasks.finish(task, "done")
                return n
            except Exception as exc:
                self.tasks.finish(task, "failed", str(exc)[:400])
                return exc

        pending = self.tasks.claim("fetch", TARGET, limit=64)
        for outcome in run_parallel(pending, worker, concurrency=min(3, settings.MAX_CONCURRENCY)):
            if isinstance(outcome, Exception):
                summary["failed"] += 1
            else:
                summary["segments"] += int(outcome)
        return summary

    # ------------------------------------------------------------------ 离线兜底
    def load_seed(self, year: int | None = None) -> int:
        """离线导入 data/seed/score_rank_*.csv，保证位次换算可用。"""
        from pathlib import Path

        total = 0
        seeds = sorted(settings.SEED_DIR.glob("score_rank_*.csv"))
        for path in seeds:
            if year and f"_{year}." not in path.name:
                continue
            total += load_score_rank_csv(self.conn, path)
        return total


def ensure_score_rank(conn: sqlite3.Connection) -> int:
    """确保库里有位次基准表：在线抓取失败则退回种子 CSV。"""
    have = conn.execute("SELECT COUNT(*) AS n FROM score_rank").fetchone()["n"]
    if have:
        return 0
    crawler = ScoreRankCrawler(conn)
    try:
        summary = crawler.run()
        if summary["segments"]:
            return summary["segments"]
    except Exception as exc:
        print(f"[score_rank] 在线抓取失败，改用种子数据：{exc}")
    return crawler.load_seed()
