"""北京教育考试院「招生计划查询」采集器（表格型，零 API 成本）。

实测接口（2026 年度）
---------------------
列表页（按学校查询）:
    GET  http://query.bjeea.cn/queryService/rest/plan/115
翻页:
    POST 同一 URL，表单 {pageFlag:true, token:<页面里的 token>, pageSize:50, pageNo:N}
学校详情:
    POST http://query.bjeea.cn/queryService/rest/plan/115/{schoolcode}
         表单 {examId:<年度ID>, schoolcode:<校代码>, subjectName:<校代码>}

两个必须注意的实测结论
---------------------
1. 详情页**只 GET 拿不到数据**：GET `/plan/115/1021` 返回 200，但只有表头、
   0 行数据（10KB）；必须 POST 带 examId/schoolcode/subjectName 才返回真实
   专业计划（46KB、含 40+ 行）。很多现成脚本就栽在这一步。
2. 列表页的分页 token 每请求都变，所以内容指纹必须先归一化（见 base.normalize_for_hash），
   否则增量去重永远判定为「已变更」。

三阶段编排
----------
discover : 解析学校列表 → 为每所学校登记 fetch 任务
fetch    : 逐校抓详情 → 内容指纹判定是否变更 → 变更才解析入库（断点续爬）
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from typing import Iterable

from config import settings
from crawler import parse
from crawler.base import FetchedPage, FetchError, HttpClient, run_parallel
from crawler.store import ContentStore, TaskQueue
from db import upsert
from pipeline.normalize import normalize_school_name

TARGET = "bjeea_plan"


def service_url(service_id: str | None = None) -> str:
    sid = service_id or settings.BJEAA_PLAN_SERVICE_ID
    return f"{settings.BJEAA_QUERY_BASE}/plan/{sid}"


def school_url(school_code: str, service_id: str | None = None) -> str:
    return f"{service_url(service_id)}/{school_code}"


class BjeeaPlanCrawler:
    def __init__(
        self,
        conn: sqlite3.Connection,
        client: HttpClient | None = None,
        service_id: str | None = None,
        exam_id: str | None = None,
        year: int | None = None,
    ) -> None:
        self.conn = conn
        self.client = client or HttpClient()
        self.service_id = service_id or settings.BJEAA_PLAN_SERVICE_ID
        self.store = ContentStore(conn)
        self.tasks = TaskQueue(conn)
        self.exam_id = exam_id
        self.year = year

    # ------------------------------------------------------------------ 年度 ID
    def resolve_exam_id(self, year: int, refresh: bool = False) -> str:
        """把年份解析成考试院的 examId。

        优先读页面 <select id="examId"> 的真实选项，其次用 settings 里的兜底映射。
        考试院只保留当年入口，因此历史年份通常解析不到 —— 这时由调用方决定
        走存档页/公众号回溯，而不是猜一个 ID 硬编。
        """
        cached = self._cached_exam_id(year)
        if cached and not refresh:
            return cached

        page = self.client.get(service_url(self.service_id))
        options = parse.parse_exam_options(page.html)
        for y, eid in options.items():
            self.tasks.enqueue("discover", TARGET, f"exam:{y}", {"exam_id": eid})
        if year in options:
            return options[year]
        if year in settings.BJEAA_EXAM_ID_FALLBACK:
            return settings.BJEAA_EXAM_ID_FALLBACK[year]
        raise LookupError(
            f"未能解析 {year} 年的 examId（页面仅提供 {sorted(options) or '未知'}）。"
            f"历史年份请改用存档页/公众号回溯，或手工在 settings.BJEAA_EXAM_ID_FALLBACK 配置。"
        )

    def _cached_exam_id(self, year: int) -> str | None:
        row = self.conn.execute(
            "SELECT payload FROM crawl_tasks WHERE stage='discover' AND target=? AND task_key=?",
            (TARGET, f"exam:{year}"),
        ).fetchone()
        if not row or not row["payload"]:
            return None
        import json

        try:
            return json.loads(row["payload"]).get("exam_id")
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------ discover
    def discover_schools(self, year: int, exam_id: str | None = None) -> list[parse.SchoolRow]:
        """抓「按学校查询」全部分页，返回学校列表。"""
        exam_id = exam_id or self.resolve_exam_id(year)
        url = service_url(self.service_id)

        first: FetchedPage = self.client.get(url)
        info = parse.parse_page_info(first.html)
        schools = parse.parse_school_list(first.html)
        self.store.ingest(url, first.html, first.status)

        total_pages = info.total_pages or 1
        if total_pages > 1:
            pages = list(range(2, min(total_pages, 60) + 1))  # 上限保护
            fetched = run_parallel(
                pages,
                lambda n: (n, self.client.post(url, {
                    "pageFlag": "true",
                    "token": info.token,
                    "pageSize": str(info.page_size),
                    "pageNo": str(n),
                })),
                concurrency=min(2, settings.MAX_CONCURRENCY),  # 列表页翻页放慢
            )
            for item in fetched:
                if isinstance(item, Exception):
                    continue
                page_no, page = item
                if not page.ok:
                    continue
                self.store.ingest(f"{url}?pageNo={page_no}", page.html, page.status)
                schools.extend(parse.parse_school_list(page.html))

        # 去重（同学科代码只留一条）
        dedup: dict[str, parse.SchoolRow] = {}
        for s in schools:
            dedup.setdefault(s.school_code or s.name, s)
        result = list(dedup.values())

        # 登记每所学校的 fetch 任务 → 断点续爬的粒度
        for s in result:
            self.tasks.enqueue(
                "fetch", TARGET, f"plan:{year}:{s.school_code}",
                {"year": year, "exam_id": exam_id, "school_code": s.school_code, "name": s.name},
            )
        self._save_schools(result)
        return result

    def _save_schools(self, schools: Iterable[parse.SchoolRow]) -> int:
        rows = [
            {
                "school_code": s.school_code,
                "name": s.name,
                "name_norm": normalize_school_name(s.name),
                "province": s.province or None,
                "source_url": service_url(self.service_id),
            }
            for s in schools
            if s.school_code and s.name
        ]
        # province 只补空，不覆盖已有值
        return upsert(
            self.conn, "schools", rows, ["school_code"],
            update_only=["name", "name_norm", "province", "source_url"],
        )

    # ------------------------------------------------------------------ fetch
    def fetch_school(
        self, year: int, exam_id: str, school_code: str, force: bool = False
    ) -> list[parse.ProgramRow]:
        """抓一所学校的专业计划并入库。返回解析出的专业行。"""
        url = school_url(school_code, self.service_id)
        page = self.client.post(
            url,
            {"examId": exam_id, "schoolcode": school_code, "subjectName": school_code},
        )
        if not page.ok:
            raise FetchError(url, f"HTTP {page.status}", page.status)

        programs = parse.parse_school_programs(page.html)
        # 详情页标题里的年份比 examId 更可信，用它与入参交叉校验
        title_year = parse.parse_title_year(page.html)
        if title_year and title_year != year:
            year = title_year

        # 内容指纹：只有真变了才落快照 + 重新入库
        task_url = f"{url}#{year}"
        change = self.store.ingest(task_url, page.html, page.status)
        if not change.changed and not force:
            return programs

        self.load_programs(year, school_code, programs, url)
        return programs

    def load_programs(
        self,
        year: int,
        school_code: str,
        programs: list[parse.ProgramRow],
        source_url: str,
    ) -> int:
        """写入 programs + admissions（计划侧：只填 plan_count，分数留空）。"""
        if not programs:
            return 0
        prog_rows = [
            {
                "year": year,
                "school_code": school_code,
                "batch": p.batch or "",
                "group_code": p.group_code or "",
                "group_label": p.group_label or "",
                "subject_req": p.subject_req or "",
                "major_code": p.major_code or "",
                "major_name": p.major_name,
                "duration": p.duration or None,
                "tuition": p.tuition or None,
                "language": p.language or None,
                "source_url": source_url,
            }
            for p in programs
        ]
        upsert(
            self.conn, "programs", prog_rows,
            ["year", "school_code", "batch", "group_code", "major_code", "major_name"],
            update_only=[
                "group_label", "subject_req", "duration", "tuition", "language", "source_url",
            ],
        )

        # admissions 的自然键要与 programs 对齐，计划数据先占位
        adm_rows = [
            {
                "year": year,
                "school_code": school_code,
                "batch": p.batch or "",
                "group_code": p.group_code or "",
                "major_code": p.major_code or "",
                "major_name": p.major_name,
                "plan_count": p.plan_count,
                "score_source": "plan",
                "source_url": source_url,
            }
            for p in programs
        ]
        # coalesce=True：分数列在计划侧为 NULL，不会覆盖已抓到的录取分数
        return upsert(
            self.conn, "admissions", adm_rows,
            ["year", "school_code", "batch", "group_code", "major_code", "major_name"],
            update_only=["plan_count", "source_url"],
        )

    # ------------------------------------------------------------------ 编排
    def run(
        self,
        year: int,
        school_codes: Iterable[str] | None = None,
        force: bool = False,
        stages: tuple[str, ...] = ("discover", "fetch"),
    ) -> dict[str, int]:
        """跑一轮采集。school_codes 为空表示全量（按学校粒度分批更稳妥）。"""
        summary = {"schools": 0, "programs": 0, "failed": 0}
        # 上次异常退出可能留下 running 任务，先放回 pending 再开工
        self.tasks.recover_running(target=TARGET)
        exam_id = self.resolve_exam_id(year)

        if "discover" in stages:
            schools = self.discover_schools(year, exam_id)
            if school_codes:
                wanted = {c.strip() for c in school_codes}
                schools = [s for s in schools if s.school_code in wanted]
            summary["schools"] = len(schools)
            for s in schools:
                self.tasks.enqueue(
                    "fetch", TARGET, f"plan:{year}:{s.school_code}",
                    {"year": year, "exam_id": exam_id, "school_code": s.school_code},
                )

        if "fetch" not in stages:
            return summary

        pending = self.tasks.claim("fetch", TARGET, limit=2000)
        if school_codes:
            wanted = {c.strip() for c in school_codes}
            keep = [t for t in pending if t.payload.get("school_code") in wanted]
            keep_ids = {t.task_id for t in keep}
            # claim() 已把任务置为 running，本轮不处理的必须放回，
            # 否则它们会永远卡在 running 状态不再被拾取。
            self.tasks.release([t for t in pending if t.task_id not in keep_ids])
            pending = keep

        def worker(task):
            payload = task.payload
            try:
                rows = self.fetch_school(
                    int(payload["year"]), str(payload["exam_id"]),
                    str(payload["school_code"]), force=force,
                )
                self.tasks.finish(task, "done")
                return len(rows)
            except Exception as exc:  # 单校失败不影响整体，留在队列里下次重试
                self.tasks.finish(task, "failed", str(exc)[:400])
                return exc

        for outcome in run_parallel(pending, worker, concurrency=settings.MAX_CONCURRENCY):
            if isinstance(outcome, Exception):
                summary["failed"] += 1
            else:
                summary["programs"] += int(outcome)
        return summary


def crawl_plan_years(
    conn: sqlite3.Connection,
    years: Iterable[int] | None = None,
    school_codes: Iterable[str] | None = None,
    force: bool = False,
) -> dict[str, dict[str, int]]:
    """按年份逐轮采集。历史年份解析不到 examId 时跳过而不是崩掉。"""
    crawler = BjeeaPlanCrawler(conn)
    out: dict[str, dict[str, int]] = {}
    for year in (years or settings.PLAN_QUERY_YEARS):
        try:
            out[str(year)] = crawler.run(year, school_codes, force=force)
        except LookupError as exc:
            out[str(year)] = {"skipped": str(exc)}  # type: ignore[dict-item]
    return out
