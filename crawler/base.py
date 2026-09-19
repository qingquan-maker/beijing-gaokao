"""采集基础设施：限速、编码、SHA-256 内容指纹、带重试的抓取。

设计要点
--------
1. **限速**：按域名维护最小请求间隔（默认 1.0s）+ 抖动，配合并发上限 5。
2. **内容指纹先归一化再哈希**。考试院页面里混着每次都变的
   `token="1789823369315"`、`JSESSIONID1=...`、页头“今天是2026年09月19日”，
   如果直接对原始字节做 SHA-256，哈希每请求都变，增量去重会彻底失效。
   所以先用 DEFAULT_NOISE 洗掉易变片段，再算哈希 —— 内容没真变就不重复入库、
   更不重复送大模型。
3. **Scrapling 优先，标准库兜底**：正常走 Scrapling 的 Fetcher（curl_cffi 指纹伪装）；
   当环境里没装 Scrapling 时退回 urllib，让 CI / 离线单测也能跑通。
"""

from __future__ import annotations

import hashlib
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from config import settings

# --------------------------------------------------------------------------- Scrapling 适配

try:
    from scrapling.fetchers import Fetcher  # type: ignore

    HAS_SCRAPLING = True
except Exception:  # pragma: no cover - 仅在未安装 Scrapling 的环境触发
    Fetcher = None  # type: ignore
    HAS_SCRAPLING = False


# --------------------------------------------------------------------------- 内容指纹

#: 易变噪声，归一化后再算哈希。顺序敏感：先长后短。
#: 这些片段实测都会变：token 是每次请求新发的防重放令牌（实测两次 GET 分别为
#: 1789823369315 / 1789823378566），页头日期与星期每天在变。
DEFAULT_NOISE: tuple[tuple[str, str], ...] = (
    (r'(?i)\s*token\s*=\s*"[^"]*"', ""),                       # pageHideDiv 防重放令牌
    (r"(?i)JSESSIONID1?=[0-9A-Za-z_\-\.!]{6,}", ""),           # 会话 ID
    (r"(?i)jsessionid=[0-9A-Za-z_\-\.!]{6,}", ""),
    # 页头「今天是2026年09月19日 星期六」——日期和星期都要抹掉
    (
        r"今天是\s*\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
        r"(?:\s*星期[一二三四五六日天])?",
        "今天是<DATE>",
    ),
    (r'"?(?:timestamp|ts|nonce|rand|random)"?\s*[:=]\s*"?\d{9,16}"?', '"ts":"<TS>"'),
    (r"<!--.*?-->", ""),                                        # 注释（含锚点注释）不影响数据
    (r"\s+", " "),
)

_COMPILED_NOISE = [(re.compile(p, re.S), r) for p, r in DEFAULT_NOISE]


def normalize_for_hash(html: str, extra_noise: tuple[tuple[str, str], ...] = ()) -> str:
    """抹掉易变片段，得到「稳定表示」，用于 SHA-256 增量判断。"""
    text = html
    for pattern, repl in _COMPILED_NOISE:
        text = pattern.sub(repl, text)
    for pattern, repl in extra_noise:
        text = re.sub(pattern, repl, text)
    return text.strip()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def content_fingerprint(html: str, extra_noise: tuple[tuple[str, str], ...] = ()) -> str:
    return sha256_text(normalize_for_hash(html, extra_noise))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- 编码

_CHARSET_RE = re.compile(rb'charset\s*=\s*["\']?\s*([A-Za-z0-9_\-]+)', re.I)
_META_RE = re.compile(rb'<meta[^>]+charset\s*=\s*["\']?\s*([A-Za-z0-9_\-]+)', re.I)


def decode_html(body: bytes, headers: Mapping[str, str] | None = None) -> str:
    """判定编码并解码。考试院老页面可能是 GBK/GB2312。"""
    candidates: list[str] = []
    if headers:
        ct = ""
        for key, value in headers.items():
            if key.lower() == "content-type":
                ct = value
                break
        m = _CHARSET_RE.search(ct.encode("latin-1", "ignore"))
        if m:
            candidates.append(m.group(1).decode("ascii", "ignore"))
    head = body[:4096]
    for regex in (_META_RE, _CHARSET_RE):
        m = regex.search(head)
        if m:
            candidates.append(m.group(1).decode("ascii", "ignore"))
    candidates.append("utf-8")

    for enc in candidates:
        if not enc:
            continue
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


# --------------------------------------------------------------------------- 限速

class RateLimiter:
    """按域名串行化 + 最小间隔。线程安全。"""

    def __init__(self, min_interval: float, jitter: float = 0.0) -> None:
        self.min_interval = min_interval
        self.jitter = jitter
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).netloc or "_"
        with self._lock:
            now = time.monotonic()
            earliest = self._last.get(host, 0.0) + self.min_interval
            sleep_for = max(0.0, earliest - now)
            if self.jitter:
                sleep_for += random.uniform(0.0, self.jitter)
            self._last[host] = max(now, earliest) + (
                sleep_for - max(0.0, earliest - now)
            )
        if sleep_for > 0:
            time.sleep(sleep_for)


# --------------------------------------------------------------------------- 响应

@dataclass
class FetchedPage:
    url: str
    status: int
    html: str
    body: bytes
    headers: dict[str, str]

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class FetchError(RuntimeError):
    def __init__(self, url: str, message: str, status: int = 0) -> None:
        super().__init__(f"{url} -> {message}")
        self.url = url
        self.status = status


# --------------------------------------------------------------------------- 客户端

class HttpClient:
    """带限速、重试、编码处理的抓取器。"""

    def __init__(
        self,
        delay: float | None = None,
        jitter: float | None = None,
        retries: int | None = None,
        timeout_ms: int | None = None,
    ) -> None:
        self.limiter = RateLimiter(
            settings.BASE_DELAY_SECONDS if delay is None else delay,
            settings.JITTER_SECONDS if jitter is None else jitter,
        )
        self.retries = settings.MAX_RETRIES if retries is None else retries
        self.timeout_ms = settings.REQUEST_TIMEOUT_MS if timeout_ms is None else timeout_ms
        self.backend = "scrapling" if HAS_SCRAPLING else "urllib"

    # -- 内部：单次请求 ---------------------------------------------------
    def _once(self, url: str, method: str, data: Mapping[str, Any] | None) -> FetchedPage:
        self.limiter.wait(url)
        if HAS_SCRAPLING:
            kwargs: dict[str, Any] = {
                "timeout": self.timeout_ms,
                "headers": {"User-Agent": settings.USER_AGENT},
            }
            if method.upper() == "POST":
                page = Fetcher.post(url, data=dict(data or {}), **kwargs)  # type: ignore[union-attr]
            else:
                page = Fetcher.get(url, **kwargs)  # type: ignore[union-attr]
            body = getattr(page, "body", b"") or b""
            if isinstance(body, str):
                body = body.encode("utf-8", "ignore")
            headers = dict(getattr(page, "headers", {}) or {})
            status = int(getattr(page, "status", 0) or 0)
            html = getattr(page, "html_content", None) or decode_html(body, headers)
            return FetchedPage(url, status, html, body, headers)

        # ---- urllib 兜底 ----
        payload = urllib.parse.urlencode(dict(data or {})).encode() if data else None
        req = urllib.request.Request(
            url,
            data=payload,
            method=method.upper(),
            headers={
                "User-Agent": settings.USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9",
                **({"Content-Type": "application/x-www-form-urlencoded"} if payload else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_ms / 1000) as resp:
                body = resp.read()
                headers = {k: v for k, v in resp.headers.items()}
                status = resp.status
        except urllib.error.HTTPError as exc:
            body = exc.read() if hasattr(exc, "read") else b""
            headers = {k: v for k, v in (exc.headers or {}).items()}
            status = exc.code
        return FetchedPage(url, status, decode_html(body, headers), body, headers)

    # -- 对外 ------------------------------------------------------------
    def fetch(
        self,
        url: str,
        method: str = "GET",
        data: Mapping[str, Any] | None = None,
        allow_status: tuple[int, ...] = (),
    ) -> FetchedPage:
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                page = self._once(url, method, data)
                if page.ok or page.status in allow_status:
                    return page
                # 4xx 里 404 重试没意义
                if 400 <= page.status < 500 and page.status != 429:
                    raise FetchError(url, f"HTTP {page.status}", page.status)
                last = FetchError(url, f"HTTP {page.status}", page.status)
            except FetchError as exc:
                if exc.status and 400 <= exc.status < 500 and exc.status != 429:
                    raise
                last = exc
            except Exception as exc:  # 网络层异常
                last = exc
            if attempt < self.retries:
                time.sleep(settings.BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
        raise last if isinstance(last, Exception) else FetchError(url, "unknown error")

    def get(self, url: str, **kw: Any) -> FetchedPage:
        return self.fetch(url, "GET", **kw)

    def post(self, url: str, data: Mapping[str, Any], **kw: Any) -> FetchedPage:
        return self.fetch(url, "POST", data, **kw)


# --------------------------------------------------------------------------- 并发

def run_parallel(items: list[Any], worker, concurrency: int | None = None) -> list[Any]:
    """有界线程池。并发上限默认取 settings.MAX_CONCURRENCY（≤5）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    limit = max(1, min(concurrency or settings.MAX_CONCURRENCY, 8))
    results: list[Any] = []
    with ThreadPoolExecutor(max_workers=limit) as pool:
        futures = {pool.submit(worker, item): item for item in items}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as exc:  # 单个任务失败不影响整体
                results.append(exc)
    return results
