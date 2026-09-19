"""真实浏览器渲染（Playwright + 系统已安装的 Edge / Chrome）。

为什么必须有这一层
------------------
高校招生网的录取分数页大多是 **JS 单页应用**：静态抓取只能拿到空壳 HTML，
表格要等前端把数据取回来才渲染出来。实测北航录取查询页就是这种结构，
静态请求 0 行数据，浏览器渲染后能拿到完整表格。

实测：**用系统自带的 Edge 即可**，不必下载 Playwright 的 Chromium 内核
（`playwright install chromium` 在国内网络下会反复 ECONNRESET）。

线程安全
--------
Playwright 的同步 API 不是线程安全的，而本项目用线程池并发采集。
这里用一把可重入锁把渲染串行化 —— 渲染本来就比静态请求贵一个数量级，
并发它没有意义。
"""

from __future__ import annotations

import threading
from typing import Any

from config import settings

#: 依次尝试：系统 Edge → 系统 Chrome。都不行就报错让调用方回退静态抓取。
CHANNELS: tuple[str | None, ...] = ("msedge", "chrome")

_LOCK = threading.RLock()
_playwright: Any = None
_browser: Any = None
_channel: str | None = None


def available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


def _ensure_browser():
    """惰性启动浏览器；同一进程内复用。"""
    global _playwright, _browser, _channel
    if _browser is not None:
        return _browser
    from playwright.sync_api import sync_playwright

    _playwright = sync_playwright().start()
    last: Exception | None = None
    for channel in CHANNELS:
        try:
            _browser = _playwright.chromium.launch(
                channel=channel, headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            _channel = channel
            return _browser
        except Exception as exc:  # 没装这个浏览器就试下一个
            last = exc
    raise RuntimeError(f"启动浏览器失败（试过 {CHANNELS}）：{last}")


def current_channel() -> str | None:
    return _channel


def render(
    url: str,
    *,
    wait_ms: int = 2500,
    timeout_ms: int | None = None,
    wait_until: str = "domcontentloaded",
) -> str:
    """打开页面、等 JS 渲染完，返回渲染后的 HTML。"""
    timeout = timeout_ms or settings.REQUEST_TIMEOUT_MS
    with _LOCK:
        browser = _ensure_browser()
        context = browser.new_context(
            user_agent=settings.USER_AGENT,
            locale="zh-CN",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout)
            page.wait_for_timeout(wait_ms)
            try:
                return page.content()
            except Exception:
                # 页面还在跳转时会取不到内容，等一下再取一次
                page.wait_for_timeout(2000)
                return page.content()
        finally:
            context.close()


def shutdown() -> None:
    global _playwright, _browser, _channel
    with _LOCK:
        try:
            if _browser is not None:
                _browser.close()
            if _playwright is not None:
                _playwright.stop()
        except Exception:
            pass
        _browser = None
        _playwright = None
        _channel = None
