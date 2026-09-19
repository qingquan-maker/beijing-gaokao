#!/usr/bin/env python3
"""发现各高校本科招生网的「录取分数」页，生成 data/seed/school_sources.csv。

为什么单独做成一个脚本
----------------------
`crawler/school_sites.py` 已经能把招生网表格解析入库，但注册表是空的 ——
缺的不是解析能力，而是「每所学校的录取分数页在哪」这份清单。
本脚本只负责**发现**，不抓数据；抓取仍然交给 SchoolSiteCrawler。

发现链路（实测可行）
--------------------
1. `cn.bing.com/search?q=<校名> 本科招生网 录取分数`
   —— 搜狗网页搜索与百度在服务端都被反爬挡住，必应国内版可用，能返回 .edu.cn 域名。
2. 从结果里挑出招生网域名（zsb / zsw / zhaosheng / bkzs 等特征）。
3. 打开该站首页，按链接文字找「录取分数 / 历年分数 / 录取查询」页面。

用法
----
  python scripts/discover_school_sites.py --schools 1021,1023,1028 --out data/seed/school_sources.csv
  python scripts/discover_school_sites.py --top 20 --out data/seed/school_sources.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings          # noqa: E402
from crawler.base import FetchError, HttpClient  # noqa: E402
from crawler.school_sites import parse_generic_admission_table  # noqa: E402
from db import connect               # noqa: E402

BING = "https://cn.bing.com/search?q={q}"

#: 招生网域名特征（按优先级）
HOST_HINTS = ("zsb", "zsw", "bkzs", "zhaosheng", "zsks", "zs", "admission")

#: 录取分数页的链接特征
PAGE_HINTS = (
    "历年分数", "历年录取", "录取分数", "录取查询", "分数线",
    "录取统计", "录取情况", "分数查询", "往年录取",
)

#: 明显不是本科录取分数的页面：研究生/博士/复试/调剂，以及 PDF 附件
_NOT_BACHELOR = ("硕士", "研究生", "博士", "复试", "调剂", "推免", "考研")

#: 招生网常见的历年分数路径（首页导航是 JS 菜单时用来兜底）
FALLBACK_PATHS = (
    "lnfs.htm", "lnfs/", "bkzn/lnfs.htm", "zsfsx.htm", "lqfs.htm",
    "lnlqfsx.htm", "zsxx/lnfs.htm", "score.htm",
)

#: 候选页最多试几个（每个都要真实请求 + 解析，控制总量）
MAX_CANDIDATES = 6

_EDU_URL_RE = re.compile(r"https?://[A-Za-z0-9.\-]*\.edu\.cn[^\s\"'<>\\]*")
_LINK_RE = re.compile(r"""<a[^>]+href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", "", _TAG_RE.sub("", fragment or "")).replace("&nbsp;", "")


def search_hosts(client: HttpClient, query: str) -> list[str]:
    """必应搜索，返回出现的 .edu.cn 域名（去重、保序）。"""
    url = BING.format(q=urllib.parse.quote(query))
    try:
        page = client.get(url)
    except FetchError:
        return []
    if not page.ok:
        return []
    hosts: list[str] = []
    for raw in _EDU_URL_RE.findall(page.html):
        host = urllib.parse.urlparse(raw).netloc.lower()
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def pick_admissions_host(hosts: list[str]) -> str | None:
    """从域名列表里挑最像本科招生网的那个。"""
    for hint in HOST_HINTS:
        for host in hosts:
            # 只看一级子域，避免把 yzb.(研究生院) 之类误判
            sub = host.split(".")[0]
            if sub == hint:
                return host
    return hosts[0] if hosts else None


def _validate(client: HttpClient, url: str) -> int:
    """真正打开候选页并用录取分数表解析器验证：能解析出几行。"""
    if url.lower().endswith(".pdf"):
        return 0
    try:
        page = client.get(url)
    except FetchError:
        return 0
    if not page.ok:
        return 0
    try:
        return len(parse_generic_admission_table(page.html))
    except Exception:
        return 0


def _candidates(client: HttpClient, host: str) -> list[tuple[str, str]]:
    """候选录取分数页：首页导航里像「历年分数」的链接 + 常见路径兜底。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    home = ""
    for scheme in ("https", "http"):
        home = f"{scheme}://{host}/"
        try:
            page = client.get(home)
        except FetchError:
            continue
        if not page.ok:
            continue
        for href, raw in _LINK_RE.findall(page.html):
            label = _text(raw)
            haystack = label + href
            hit = next((h for h in PAGE_HINTS if h in haystack), None)
            if not hit:
                continue
            if any(bad in haystack for bad in _NOT_BACHELOR):
                continue
            target = urllib.parse.urljoin(home, href)
            if not target.startswith(("http://", "https://")) or target in seen:
                continue
            seen.add(target)
            out.append((target, label or hit))
        break
    if home:
        base = urllib.parse.urlparse(home).scheme + "://" + host + "/"
        for path in FALLBACK_PATHS:
            target = urllib.parse.urljoin(base, path)
            if target not in seen:
                seen.add(target)
                out.append((target, path))
    return out[:MAX_CANDIDATES]


def find_score_page(client: HttpClient, host: str) -> tuple[str, str] | None:
    """在招生网里找出**能被解析器验证**的录取分数页。"""
    for url, label in _candidates(client, host):
        if _validate(client, url) >= 3:
            return url, label
    return None


def discover(client: HttpClient, code: str, name: str) -> dict[str, str]:
    # 实测：查询词越长，必应越容易返回「掌上高考」这类聚合站，.edu.cn 反而归零。
    # 用「<校名> 本科招生网」这种短查询才会命中学校官网域名。
    hosts: list[str] = []
    for query in (f"{name} 本科招生网", f"{name} 招生网", f"{name} 招生信息网"):
        hosts = search_hosts(client, query)
        if hosts:
            break
    host = pick_admissions_host(hosts)
    if not host:
        return {"school_code": code, "school_name": name, "admissions_url": "",
                "status": "未找到招生网域名", "candidates": ""}
    found = find_score_page(client, host)
    if not found:
        return {"school_code": code, "school_name": name,
                "admissions_url": "",
                "status": f"有招生网({host})但未找到可解析的分数表",
                "candidates": host}
    url, label = found
    return {"school_code": code, "school_name": name, "admissions_url": url,
            "status": f"已验证「{label}」", "candidates": host}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="发现高校本科招生网录取分数页")
    p.add_argument("--schools", help="院校代码，逗号分隔")
    p.add_argument("--top", type=int, default=20, help="未指定 --schools 时，取招生计划最多的前 N 所")
    p.add_argument("--out", default="data/seed/school_sources.csv")
    p.add_argument("--delay", type=float, default=2.0, help="请求间隔（秒）")
    p.add_argument("--year-hint", type=int, default=2025)
    p.add_argument("--dry-run", action="store_true", help="只打印不写文件")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    settings.ensure_dirs()
    conn = connect()
    conn.row_factory = None

    if args.schools:
        codes = [c.strip() for c in args.schools.split(",") if c.strip()]
        qmarks = ",".join("?" * len(codes))
        rows = conn.execute(
            f"SELECT school_code, name FROM schools WHERE school_code IN ({qmarks})",
            codes,
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT s.school_code, s.name
                 FROM schools s
                 JOIN (SELECT school_code, COUNT(*) n FROM programs
                        WHERE year = (SELECT MAX(year) FROM programs)
                          AND batch LIKE '%普通批%'
                        GROUP BY school_code) p ON p.school_code = s.school_code
                ORDER BY p.n DESC LIMIT ?""",
            (args.top,),
        ).fetchall()

    client = HttpClient(delay=args.delay)
    out_rows: list[dict[str, str]] = []
    hit = 0
    for code, name in rows:
        result = discover(client, code, name)
        if result["admissions_url"]:
            hit += 1
        print(f"  {code} {name:<22} {result['status']:<28} {result['admissions_url']}")
        out_rows.append({
            "school_code": code,
            "school_name": name,
            "admissions_url": result["admissions_url"],
            "strategy": "table",
            "year_hint": str(args.year_hint),
            "note": result["status"],
        })
        time.sleep(args.delay)

    print(f"\n命中 {hit}/{len(out_rows)}")
    if args.dry_run:
        return 0

    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["school_code", "school_name", "admissions_url",
                            "strategy", "year_hint", "note"],
        )
        writer.writeheader()
        writer.writerows([r for r in out_rows if r["admissions_url"]])
    print(f"已写入 {path}（只写命中的 {hit} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
