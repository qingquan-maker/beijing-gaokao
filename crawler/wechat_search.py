"""公众号文章发现与抓取（搜狗微信搜索入口）。

为什么需要这个模块
------------------
考试院只公布到「院校专业组」，专业级录取分数、专业目录细节等只能在
各校/各机构的公众号文章里找。但公众号没有公开 API，`WECHAT_SOURCES`
要求你事先知道文章地址 —— 本模块补上的正是「怎么找到文章」这一段。

实测可行的三步链路
------------------
1. **搜索**：`https://weixin.sogou.com/weixin?type=2&query=...` 服务端可调用，
   返回 20 条/页的结果（标题、公众号名、日期、摘要）。
2. **解析跳转**：结果链接是 `/link?url=...` 的防爬跳转，直接请求会
   `ERR_CONNECTION_CLOSED`；但跳转页的正文是一段 JS，把真实地址拆成
   `url += '...'` 片段。带上搜索页的 Cookie + Referer 取回该页，拼接片段
   即可还原 `mp.weixin.qq.com/s?...` 真实地址。
3. **抓正文**：`mp.weixin.qq.com` 对普通 UA 返回 200，可直接解析。

限速
----
搜狗对频率敏感（连续请求很快会进入验证码页）。这里默认 ≥3 秒/请求，
并在检测到验证码页时立刻停止本轮，而不是死磕。
"""

from __future__ import annotations

import http.cookiejar
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Iterator

from config import settings

SEARCH_URL = "https://weixin.sogou.com/weixin"

#: 搜狗跳转页把真实地址拆成这些片段
_JS_PART_RE = re.compile(r"url\s*\+=\s*'([^']*)'")
_RESULT_RE = re.compile(
    r'<li[^>]*class="news-box"[^>]*>(?P<body>.*?)</li>\s*(?=<li|</ul>)', re.S | re.I
)
_TITLE_RE = re.compile(r'<h3>\s*<a[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.S | re.I)
_ACCOUNT_RE = re.compile(r'class="account"[^>]*>(?P<name>.*?)</a>', re.S | re.I)
_DATE_RE = re.compile(r"document\.write\(timeConvert\('(?P<ts>\d+)'\)\)")
_SNIPPET_RE = re.compile(r'<p class="txt-info"[^>]*>(?P<text>.*?)</p>', re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")

#: 命中任意一条即认为被反爬拦截
BLOCK_HINTS = ("请输入验证码", "antispider", "用户您好，您的访问过于频繁", "为了您的正常访问")


def _clean(fragment: str) -> str:
    text = _TAG_RE.sub("", fragment or "")
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


#: 搜索结果的 href 里带着**未编码的空格和中文**（query 参数原样拼回去），
#: urllib 会直接抛 InvalidURL，所以发请求前必须编码一次；safe 保留 % 避免二次编码。
_URL_SAFE = "/:?&=#%+,;@[]!$'()*~"


def ascii_url(url: str) -> str:
    return urllib.parse.quote(url, safe=_URL_SAFE)


@dataclass
class ArticleStub:
    title: str
    account: str = ""
    date: str = ""
    snippet: str = ""
    link_url: str = ""
    query: str = ""
    url: str = ""            # 解析出来的 mp.weixin.qq.com 地址
    resolved: bool = False


@dataclass
class SearchSummary:
    queries: int = 0
    results: int = 0
    resolved: int = 0
    blocked: bool = False
    errors: list[str] = field(default_factory=list)


class WechatSearchClient:
    """带 Cookie、限速与反爬检测的搜狗微信搜索客户端。"""

    def __init__(self, delay: float = 3.0, timeout: int = 25) -> None:
        self.delay = max(1.5, delay)
        self.timeout = timeout
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )
        self._last = 0.0
        self._referer = ""

    # ------------------------------------------------------------------ 基础请求
    def _wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last = time.monotonic()

    def _get(self, url: str, referer: str = "") -> tuple[int, str]:
        self._wait()
        headers = {
            "User-Agent": settings.USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if referer:
            headers["Referer"] = referer
        req = urllib.request.Request(ascii_url(url), headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001 - 网络层异常交给调用方判断
            return 0, f"__ERROR__{type(exc).__name__}: {exc}"

    @staticmethod
    def is_blocked(html: str) -> bool:
        return any(hint in html for hint in BLOCK_HINTS)

    # ------------------------------------------------------------------ 搜索
    def search(self, query: str, pages: int = 1) -> list[ArticleStub]:
        out: list[ArticleStub] = []
        seen: set[str] = set()
        for page in range(1, max(1, pages) + 1):
            url = f"{SEARCH_URL}?type=2&query={urllib.parse.quote(query)}"
            if page > 1:
                url += f"&page={page}"
            status, html = self._get(url)
            if status != 200 or "__ERROR__" in html:
                break
            if self.is_blocked(html):
                raise RuntimeError("搜狗返回验证码页，本轮停止（降低频率后重试）")
            self._referer = url
            found = 0
            for m in _TITLE_RE.finditer(html):
                href = m.group("href").replace("&amp;", "&")
                if href in seen:
                    continue
                seen.add(href)
                found += 1
                # 结果块内继续找公众号名 / 日期 / 摘要
                tail = html[m.end(): m.end() + 1500]
                acc = _ACCOUNT_RE.search(tail)
                date = _DATE_RE.search(tail)
                snip = _SNIPPET_RE.search(tail)
                out.append(ArticleStub(
                    title=_clean(m.group("title")),
                    account=_clean(acc.group("name")) if acc else "",
                    date=time.strftime("%Y-%m-%d", time.localtime(int(date.group("ts"))))
                    if date else "",
                    snippet=_clean(snip.group("text")) if snip else "",
                    link_url=urllib.parse.urljoin("https://weixin.sogou.com/", href),
                    query=query,
                ))
            if found == 0:
                break
        return out

    # ------------------------------------------------------------------ 解析跳转
    def resolve(self, link_url: str) -> str | None:
        """把 /link?url=... 的跳转地址还原成 mp.weixin.qq.com 真实地址。"""
        status, page = self._get(link_url, referer=self._referer)
        if status != 200 or "__ERROR__" in page:
            return None
        parts = _JS_PART_RE.findall(page)
        if not parts:
            return None
        target = "".join(parts).replace("@", "")
        if not target.startswith("http"):
            return None
        return target

    # ------------------------------------------------------------------ 抓正文
    def fetch_article(self, url: str) -> str:
        status, html = self._get(url, referer=self._referer)
        if status != 200 or "__ERROR__" in html:
            raise RuntimeError(f"抓取失败 {url}: {status}")
        if "环境异常" in html or "去验证" in html:
            raise RuntimeError("公众号返回「环境异常」风控页")
        return html


def iter_queries(school_names: list[str], years: list[int], extra: list[str] | None = None) -> Iterator[str]:
    """生成搜索关键词。默认按「学校 + 年份 + 北京 + 录取分数」组织。"""
    for year in years:
        for name in school_names:
            yield f"{name} {year} 北京 各专业录取分数线"
    for q in extra or []:
        yield q


def article_text(html: str) -> str:
    """从公众号页面里取正文文本（复用项目已有的选择器兜底逻辑）。"""
    from crawler.wechat_articles import extract_article_body

    return extract_article_body(html)
