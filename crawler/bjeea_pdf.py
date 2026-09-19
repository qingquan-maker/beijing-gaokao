"""北京教育考试院「PDF 附件型」数据采集：录取投档线 + 分数分布（一分一段）。

为什么单独写一个采集器
----------------------
考试院近年把最关键的两类数据放进了 **PDF 附件**，公告正文只有一行链接：

  * 录取投档线   —— 「2026年北京市高招本科普通批录取投档线」
  * 分数分布     —— 「北京市2025年高考考生分数分布」（即一分一段表）

于是 HTML 表格解析在这里必然拿到 0 行，需要走 PDF。

实测（2026 本科普通批投档线）
-----------------------------
52 页 / 1597 行，列为::

    序号  院校代号  院校名称  专业组  选考要求  总分  语文  数学  外语  三科选考  [备注]

两条实测出来的解析规则（都踩过坑）：

  1. **单科成绩是可选的**。相当一部分行只公布总分，例如::

        63 1027 北京化工大学 01 不限 610
        70 1028 北京邮电大学 02 物理＋化学 651

     如果正则强制要求"总分 + 4 个单科"，这些行会被整行静默丢掉 ——
     北京大学、中国人民大学的多条记录正是这样消失的。
  2. **总分允许比单科合计高**，差值来自政策性加分（实测多为 10 分）。
     所以校验规则是 `0 ≤ 总分 − 单科合计 ≤ 20`，而不是严格相等；
     超出范围才判为错行。PDF 解析最容易出"错列"，而错列几乎必然
     破坏这条不等式。

设计取舍
--------
1. 投档线是**专业组级**数据，落进独立的 `group_admissions` 表，
   而不是硬塞进 `admissions` 的专业级自然键。塞进去会让同一份分数
   在组内每个专业上重复一次，页面上看起来就像"每个专业都考了同一个分"。
2. 批次以 `programs` 表里的真实批次为准。投档线 PDF 标题里的"提前批"是
   粗粒度说法，实际含 A/B 段；匹配不到才退回标题里的粗粒度批次。
"""

from __future__ import annotations

import io
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Sequence
from urllib.parse import quote, urljoin

from config import settings
from crawler.base import FetchError, HttpClient
from crawler.store import ContentStore
from db import rank_for_score, upsert

TARGET_SCORE_LINE = "bjeea_score_line"
TARGET_RANK_PDF = "bjeea_score_rank_pdf"

_SITE = "https://www.bjeea.cn"
LIST_URL = f"{_SITE}/html/gkgz/tzgg/index.html"

#: 注意：考试院页面混用单双引号（分页链接是 href='...'，正文链接是 href="..."），
#: 只认双引号会让「下一页」全部落空，列表永远只扫到第 1 页。
_LINK_RE = re.compile(r"""<a[^>]+href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_PDF_RE = re.compile(r"""href\s*=\s*["']([^"']+\.pdf)["']""", re.I)

#: 该 CMS 的列表分页是 /html/gkgz/tzgg/list_347_2.html（栏目 ID + 页码）。
#: 栏目 ID 会变，所以不硬编模板，而是从第 1 页的「末页/下一页」链接里发现。
_PAGER_RE = re.compile(r"(?P<path>[\w/\-]*list_\d+_)(?P<n>\d+)\.html", re.I)

#: 附件文件名常是未编码的中文（如 /uploads/soft/260720/2026年北京市高招…pdf），
#: urllib 发请求前要求 URL 是 ASCII，否则直接抛 UnicodeEncodeError。
#: safe 里保留 % 是为了不把已经编码过的链接二次编码。
_URL_SAFE = "/:?&=#%+,;@[]!$'()*~"


def ascii_url(url: str) -> str:
    return quote(url, safe=_URL_SAFE)

#: 投档线数据行。列顺序实测固定，用右锚定的数字组避免把校名里的字符吃进去。
SCORE_LINE_RE = re.compile(
    r"^(?P<no>\d{1,4})\s+"
    r"(?P<code>\d{4})\s+"
    r"(?P<name>\S+?)\s+"
    # 专业组代码通常是 01/02，但专科批会出现 M0 这类字母数字混排的代码
    r"(?P<group>[A-Za-z0-9]{1,4})\s+"
    r"(?P<req>\S+?)\s+"
    r"(?P<total>\d{3})"
    # 本科批给 4 个单科（语数外 + 三科选考），专科批只给 3 个（语数外）
    r"(?:\s+(?P<cn>\d{1,3})\s+(?P<math>\d{1,3})\s+(?P<en>\d{1,3})"
    r"(?:\s+(?P<three>\d{1,3}))?)?"
    r"(?:\s+(?P<note>.+))?$"
)

#: 总分与单科合计的最大允许差值（政策性加分，实测 10 分）
MAX_POLICY_BONUS = 20

#: 北京 3+3 的选考科目只有这 6 门（外加"不限"）。用白名单判断第 5 列
#: 到底是不是选考要求 —— 专科批那一列其实是**专业名称**。
SUBJECT_TOKENS = ("不限", "物理", "化学", "生物", "思想政治", "历史", "地理")

#: PDF 上的水印「北京市教育考试院」会散落成单个字混进正文 ——
#: 实测把「中外合办」污染成「中外育合办」、把专业名污染成「技试术」。
#: 直接删这七个字会误伤「教育」「北京」等正常词，所以只做**带校验的清理**：
#: 删掉水印字后如果正好落回已知词汇，才采纳。
WATERMARK_CHARS = "北京市教育考试院"
_WM_RE = re.compile("[" + WATERMARK_CHARS + "]")

#: 选考要求里出现过的后缀（括号内）
KNOWN_SUFFIXES = ("必须选考", "均须选考", "中外合办", "中外合作办学", "男", "女")


def is_subject_combo(text: str) -> bool:
    """判断一段文字是不是纯选考科目组合（不限 / 物理＋化学 …）。"""
    parts = re.split(r"[＋+、,，]", (text or "").strip())
    return bool(parts) and all(p and p in SUBJECT_TOKENS for p in parts)


def clean_subject_req(text: str) -> str:
    """清理选考要求里的水印字。宁可不改，也不改错。"""
    t = (text or "").strip()
    if not t or not _WM_RE.search(t):
        return t
    m = re.match(r"^(?P<base>.*?)[（(](?P<suffix>[^)）]*)[)）]$", t)
    base, suffix = (m.group("base"), m.group("suffix")) if m else (t, "")

    stripped_base = _WM_RE.sub("", base)
    if stripped_base != base and is_subject_combo(stripped_base):
        base = stripped_base

    if not suffix:
        return base
    stripped_suffix = _WM_RE.sub("", suffix)
    if stripped_suffix != suffix and stripped_suffix in KNOWN_SUFFIXES:
        suffix = stripped_suffix
    return f"{base}({suffix})"


def looks_like_subject_req(text: str) -> bool:
    """判断第 5 列是「选考要求」还是「专业名称」。

    选考要求由选考科目拼成（"物理＋化学"、"不限(中外合办)"），
    去掉括号后缀后按分隔符拆开，每一段都必须正好是一门科目 ——
    这样"测绘地理信息技术"这种含"地理"的专业名不会被误判。
    """
    t = (text or "").strip()
    if not t or len(t) > 20:
        return False
    t = re.sub(r"[（(].*?[)）]", "", t)
    if not t:
        return False
    parts = re.split(r"[＋+、,，]", t)
    return all(p and p in SUBJECT_TOKENS for p in parts)


#: 分数分布数据行。
#: 行首可能被水印字符污染 —— 实测「北京市教育考试院」这七个字会散落在行内，
#: 例如 `京671 64 943`、`北667 75 1251`。所以允许行首有少量非数字字符，
#: 再用「分数必须逐段递减」这条硬约束兜底校验。
RANK_ROW_RE = re.compile(
    r"^(?P<noise>[^\d]{0,4})"
    r"(?P<score>\d{3})"
    r"(?P<above>分以上)?\s+"
    r"(?P<segment>\d{1,6})\s+"
    r"(?P<cumulative>\d{1,6})$"
)


@dataclass
class Announcement:
    title: str
    page_url: str
    pdf_url: str
    year: int
    kind: str  # score_line | rank


# --------------------------------------------------------------------------- 公告发现


def _clean(html_fragment: str) -> str:
    return _TAG_RE.sub("", html_fragment).replace("&nbsp;", " ").strip()


def list_announcements(
    client: HttpClient,
    year: int,
    keywords: Sequence[str],
    max_pages: int = 8,
) -> list[tuple[str, str]]:
    """扫「高考高招 > 通知公告」的列表页，返回 (详情页 URL, 标题)。"""
    try:
        first = client.get(LIST_URL)
    except FetchError:
        return []
    if not first.ok:
        return []

    pages: list[tuple[str, str]] = [(LIST_URL, first.html)]
    pager: dict[int, str] = {}
    for href in _LINK_RE.findall(first.html):
        m = _PAGER_RE.search(href[0])
        if m:
            pager[int(m.group("n"))] = urljoin(LIST_URL, href[0])
    for n, url in sorted(pager.items())[: max(0, max_pages - 1)]:
        try:
            page = client.get(url)
        except FetchError:
            break
        if not page.ok:
            break
        pages.append((url, page.html))

    found: dict[str, str] = {}
    for url, html in pages:
        for href, raw in _LINK_RE.findall(html):
            title = _clean(raw)
            if not title or str(year) not in title:
                continue
            if not any(k in title for k in keywords):
                continue
            full = urljoin(url, href)
            found.setdefault(full, title)
    return list(found.items())


def announcement_pdf(client: HttpClient, page_url: str) -> str | None:
    """详情页正文里取 PDF 附件链接。"""
    page = client.get(page_url)
    if not page.ok:
        return None
    m = _PDF_RE.search(page.html)
    return ascii_url(urljoin(page_url, m.group(1))) if m else None


def discover(
    client: HttpClient,
    year: int,
    *,
    want_score_lines: bool = True,
    want_rank: bool = True,
    max_pages: int = 8,
) -> list[Announcement]:
    keywords: list[str] = []
    if want_score_lines:
        keywords.append("投档线")
    if want_rank:
        keywords.append("分数分布")

    out: list[Announcement] = []
    for page_url, title in list_announcements(client, year, keywords, max_pages=max_pages):
        kind = "score_line" if "投档线" in title else "rank"
        pdf_url = announcement_pdf(client, page_url)
        if not pdf_url:
            continue
        out.append(Announcement(title, page_url, pdf_url, year, kind))
    return out


#: 投档线公告标题 → 粗粒度批次（真正的批次以 programs 表为准，这里只是兜底）
def batch_from_title(title: str) -> str:
    if "专科" in title and "提前批" in title:
        return "专科提前批普通"
    if "专科" in title:
        return "专科普通批"
    if "提前批" in title:
        return "本科提前批普通A段"
    if "本科" in title:
        return "本科普通批"
    return ""


def is_plain_rank_title(title: str) -> bool:
    """判断一份"分数分布"PDF 是不是**普通类全口径**的一分一段表。

    考试院同时发布多张分布表：普通类、艺术类（综合分）、专科……它们的
    分数口径完全不同，混进同一张 `score_rank` 会让位次换算彻底失真。
    这里只接受普通类全口径那张。
    """
    if "分数分布" not in title:
        return False
    return not any(bad in title for bad in ("艺术类", "综合分", "专科", "体育"))


# --------------------------------------------------------------------------- PDF 解析


def parse_score_line_pdf(pdf_bytes: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """解析投档线 PDF。返回 (行, 校验失败的行)。"""
    import pdfplumber

    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for raw in text.splitlines():
                line = raw.strip()
                m = SCORE_LINE_RE.match(line)
                if not m:
                    continue
                d = m.groupdict()
                total = int(d["total"])
                subs: list[int] = []
                if d["cn"] is not None:
                    subs = [int(d["cn"]), int(d["math"]), int(d["en"])]
                    if d["three"] is not None:
                        subs.append(int(d["three"]))
                    gap = total - sum(subs)
                    if not (0 <= gap <= MAX_POLICY_BONUS):
                        problems.append(line)
                        continue
                else:
                    gap = 0
                # 第 5 列在本科批是选考要求，在专科批是**专业名称** —— 必须分开存，
                # 否则专业名会污染前端的「选科要求」筛选器。
                fifth = clean_subject_req(d["req"])
                if looks_like_subject_req(fifth):
                    subject_req, major_name = fifth, ""
                else:
                    subject_req, major_name = "", fifth
                note = (d["note"] or "").strip()
                if major_name and not note:
                    note = major_name
                key = (d["code"], d["group"])
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "school_code": d["code"],
                    "school_name": d["name"],
                    "group_code": d["group"].zfill(2),
                    "subject_req": subject_req,
                    "min_score": total,
                    "sub_scores": subs,
                    "policy_bonus": gap,
                    "note": note,
                })
    return rows, problems


def parse_rank_pdf(pdf_bytes: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    """解析分数分布（一分一段）PDF。

    北京的表是「分数 / 本段人数 / 累计人数」，尾部会合并成区间
    （如 120-129）。解析后用**累计人数单调不减**做校验，
    并丢弃明显不可能的分数段。
    """
    import pdfplumber

    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    last_cumulative = 0
    last_score: int | None = None

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for raw in text.splitlines():
                line = raw.strip()
                m = RANK_ROW_RE.match(line)
                if not m:
                    continue
                score = int(m.group("score"))
                segment = int(m.group("segment"))
                cumulative = int(m.group("cumulative"))
                if not (100 <= score <= 750):
                    continue
                if cumulative < last_cumulative:
                    problems.append(line)
                    continue
                # 分数必须逐段递减：这是行首被水印字符污染时唯一的兜底校验
                if last_score is not None and score >= last_score:
                    problems.append(line)
                    continue
                last_cumulative = cumulative
                last_score = score
                rows.append({
                    # "692分以上" 是合并的顶段，上界取满分 750
                    "score_high": 750 if m.group("above") else score,
                    "score_low": score,
                    "segment_count": segment,
                    "cumulative_count": cumulative,
                })

    # 合并相邻的相同累计人数段（PDF 里尾部区间会写成一行）
    merged: list[dict[str, Any]] = []
    for r in rows:
        if merged and merged[-1]["cumulative_count"] == r["cumulative_count"]:
            merged[-1]["score_low"] = r["score_low"]
            merged[-1]["segment_count"] += r["segment_count"]
        else:
            merged.append(dict(r))
    return merged, problems


# --------------------------------------------------------------------------- 入库


def _ensure_schools(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]], source_url: str) -> None:
    """投档线里出现的学校如果主数据里没有，补一条最小记录（不覆盖已有字段）。"""
    known = {r["school_code"] for r in conn.execute("SELECT school_code FROM schools")}
    missing = [
        {
            "school_code": r["school_code"],
            "name": r["school_name"],
            "name_norm": r["school_name"],
            "source_url": source_url,
        }
        for r in rows
        if r["school_code"] not in known
    ]
    if missing:
        upsert(conn, "schools", missing, ["school_code"], update_only=["source_url"])


def resolve_batch(
    conn: sqlite3.Connection, year: int, school_code: str, group_code: str, fallback: str
) -> str:
    """把投档线的粗粒度批次对齐到 programs 表里的真实批次。"""
    rows = conn.execute(
        """SELECT DISTINCT batch FROM programs
            WHERE year = ? AND school_code = ? AND group_code = ?""",
        (year, school_code, group_code),
    ).fetchall()
    batches = [r["batch"] for r in rows if r["batch"]]
    if not batches:
        return fallback
    if len(batches) == 1:
        return batches[0]
    for b in batches:
        if "普通批" in b:
            return b
    return sorted(batches)[0]


def load_score_lines(
    conn: sqlite3.Connection,
    year: int,
    rows: Sequence[dict[str, Any]],
    source_url: str,
    batch_fallback: str = "",
) -> int:
    if not rows:
        return 0
    _ensure_schools(conn, rows, source_url)
    major_cache: dict[str, set[str]] = {}
    #: 全校专业名录。水印字会插进专业名中间（「网络新闻与传试播」），
    #: 只用本校目录校验会漏掉部分（该专业当年没进计划表），因此再兜一层
    #: 「该名字在任意学校的专业目录里存在」——专业名是标准名称，可以这样校验。
    global_majors: set[str] = set()
    for r in conn.execute("SELECT DISTINCT major_name FROM programs"):
        name = re.sub(r"[【\[（(].*?[】\]）)]", "", r["major_name"] or "").strip()
        if name:
            global_majors.add(name)

    def known_majors(code: str) -> set[str]:
        """该校在考试院专业目录里的专业名（去掉【选科要求】后缀），用于校验清洗结果。"""
        if code not in major_cache:
            names: set[str] = set()
            for r in conn.execute(
                "SELECT major_name FROM programs WHERE year = ? AND school_code = ?",
                (year, code),
            ):
                name = re.sub(r"[【\[（(].*?[】\]）)]", "", r["major_name"] or "").strip()
                if name:
                    names.add(name)
            major_cache[code] = names
        return major_cache[code]

    payload = []
    for r in rows:
        batch = resolve_batch(conn, year, r["school_code"], r["group_code"], batch_fallback)
        note = r.get("note") or ""
        # 专科批的第 5 列是专业名，同样会被水印污染（如「技试术」）。
        # 只在该校专业目录里确实存在清洗后的名字时才采纳，避免改错。
        if note and _WM_RE.search(note):
            cleaned = _WM_RE.sub("", note).strip()
            if cleaned and (cleaned in known_majors(r["school_code"])
                            or cleaned in global_majors):
                note = cleaned
        score = r["min_score"]
        payload.append({
            "year": year,
            "school_code": r["school_code"],
            "batch": batch,
            "group_code": r["group_code"],
            "subject_req": r["subject_req"],
            "min_score": score,
            "rank_min": rank_for_score(conn, year, score),
            "sub_scores": ",".join(str(x) for x in r.get("sub_scores") or []),
            "note": note or None,
            "source_kind": "official_pdf",
            "source_url": source_url,
        })
    return upsert(
        conn,
        "group_admissions",
        payload,
        ["year", "school_code", "batch", "group_code"],
        update_only=["subject_req", "min_score", "rank_min", "sub_scores", "note",
                     "source_kind", "source_url"],
    )


def load_rank_rows(
    conn: sqlite3.Connection, year: int, rows: Sequence[dict[str, Any]], source_url: str
) -> int:
    if not rows:
        return 0
    # 一分一段表是一份完整快照。PDF 覆盖到的分数区间先清空再写入，
    # 避免旧的（更粗的）分段与新的精确分段同时留在表里互相打架。
    # 只清 PDF 覆盖到的区间，区间之外（种子 CSV 的尾部低分段）保留。
    lo = min(r["score_low"] for r in rows)
    hi = max(r["score_high"] for r in rows)
    conn.execute(
        "DELETE FROM score_rank WHERE year = ? AND score_low >= ? AND score_high <= ?",
        (year, lo, hi),
    )
    conn.commit()
    payload = [
        {
            "year": year,
            "score_low": r["score_low"],
            "score_high": r["score_high"],
            "segment_count": r["segment_count"],
            "cumulative_count": r["cumulative_count"],
            "source_url": source_url,
        }
        for r in rows
    ]
    return upsert(
        conn, "score_rank", payload, ["year", "score_low"],
        update_only=["score_high", "segment_count", "cumulative_count", "source_url"],
    )


def recompute_group_ranks(conn: sqlite3.Connection, year: int | None = None) -> int:
    """按一分一段表重算专业组投档线对应的位次。"""
    sql = "SELECT year, school_code, batch, group_code, min_score FROM group_admissions"
    params: tuple[Any, ...] = ()
    if year is not None:
        sql += " WHERE year = ?"
        params = (year,)
    rows = conn.execute(sql, params).fetchall()
    updated = 0
    with_rank: list[dict[str, Any]] = []
    for r in rows:
        rank = rank_for_score(conn, int(r["year"]), r["min_score"])
        if rank is None:
            continue
        with_rank.append({
            "year": int(r["year"]),
            "school_code": r["school_code"],
            "batch": r["batch"],
            "group_code": r["group_code"],
            "rank_min": rank,
        })
        updated += 1
    if with_rank:
        upsert(
            conn, "group_admissions", with_rank,
            ["year", "school_code", "batch", "group_code"],
            update_only=["rank_min"],
        )
    return updated


# --------------------------------------------------------------------------- 编排


class BjeeaPdfCrawler:
    def __init__(self, conn: sqlite3.Connection, client: HttpClient | None = None) -> None:
        self.conn = conn
        self.client = client or HttpClient()
        self.store = ContentStore(conn)

    def run(
        self,
        years: Iterable[int],
        *,
        want_score_lines: bool = True,
        want_rank: bool = True,
        max_list_pages: int = 8,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {"score_lines": 0, "rank_rows": 0, "problems": 0, "files": []}
        skipped: list[dict[str, Any]] = []
        for year in years:
            announcements = discover(
                self.client, year,
                want_score_lines=want_score_lines, want_rank=want_rank,
                max_pages=max_list_pages,
            )
            for item in announcements:
                page = self.client.get(item.pdf_url)
                if not page.ok:
                    continue
                self.store.ingest(item.pdf_url, page.html, page.status)
                if item.kind == "score_line":
                    rows, problems = parse_score_line_pdf(page.body)
                    n = load_score_lines(
                        self.conn, year, rows, item.pdf_url,
                        batch_fallback=batch_from_title(item.title),
                    )
                    summary["score_lines"] += n
                    summary["problems"] += len(problems)
                    summary["files"].append(
                        {"year": year, "kind": "score_line", "title": item.title,
                         "rows": len(rows), "problems": len(problems), "url": item.pdf_url}
                    )
                else:
                    if not is_plain_rank_title(item.title):
                        skipped.append({"year": year, "title": item.title, "reason": "非普通类全口径"})
                        continue
                    rows, problems = parse_rank_pdf(page.body)
                    n = load_rank_rows(self.conn, year, rows, item.pdf_url)
                    summary["rank_rows"] += n
                    summary["problems"] += len(problems)
                    summary["files"].append(
                        {"year": year, "kind": "rank", "title": item.title,
                         "rows": len(rows), "problems": len(problems), "url": item.pdf_url}
                    )
        summary["ranks_filled"] = recompute_group_ranks(self.conn)
        summary["skipped"] = skipped
        return summary


def crawl_pdf_sources(
    conn: sqlite3.Connection,
    years: Iterable[int],
    **kw: Any,
) -> dict[str, Any]:
    return BjeeaPdfCrawler(conn).run(years, **kw)
