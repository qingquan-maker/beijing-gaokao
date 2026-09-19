"""解析器适配层：Scrapling 优先，BeautifulSoup 兜底。

生产环境用 Scrapling 的 `Selector`（自带 lxml + 自适应选择器）。
但在受限环境里（例如无法安装 curl_cffi/playwright 的 CI、离线开发机）
装上 Scrapling 有时不现实 —— 这时退回 BeautifulSoup(lxml) 提供的同名接口。

两者只需要覆盖本项目用到的极小接口面：
    Selector(html).css(sel) -> [Element]
    Element.css(sel)        -> [Element]
    Element.text            -> str
    Element.get_all_text()  -> str   （可选）
这样解析逻辑（crawler/parse.py）不必关心底层是谁，
并且可以用 tests/fixtures 的真实页面快照做离线回归。
"""

from __future__ import annotations

from typing import Any

SCRAPLING_AVAILABLE = False
_BACKEND = "beautifulsoup"

try:  # pragma: no cover - 取决于运行环境
    from scrapling.parser import Selector as _ScraplingSelector  # type: ignore

    SCRAPLING_AVAILABLE = True
    _BACKEND = "scrapling"
except Exception:  # noqa: BLE001
    try:
        from scrapling import Selector as _ScraplingSelector  # type: ignore

        SCRAPLING_AVAILABLE = True
        _BACKEND = "scrapling"
    except Exception:  # noqa: BLE001
        _ScraplingSelector = None  # type: ignore


class _SoupElement:
    """把 bs4 的 Tag 包装成 Scrapling 风格的最小接口。"""

    __slots__ = ("_tag",)

    def __init__(self, tag: Any) -> None:
        self._tag = tag

    def css(self, selector: str) -> list["_SoupElement"]:
        return [_SoupElement(t) for t in self._tag.select(selector)]

    @property
    def text(self) -> str:
        return self._tag.get_text(" ", strip=True)

    def get_all_text(self) -> str:
        return self._tag.get_text("\n", strip=True)

    @property
    def attrib(self) -> dict[str, str]:
        return dict(self._tag.attrs)


class _SoupSelector:
    """BeautifulSoup 兜底解析器。"""

    def __init__(self, html: str) -> None:
        from bs4 import BeautifulSoup

        try:
            self._soup = BeautifulSoup(html, "lxml")
        except Exception:  # lxml 不可用时退到标准库解析器
            self._soup = BeautifulSoup(html, "html.parser")

    def css(self, selector: str) -> list[_SoupElement]:
        return [_SoupElement(t) for t in self._soup.select(selector)]

    @property
    def text(self) -> str:
        return self._soup.get_text(" ", strip=True)

    def get_all_text(self) -> str:
        return self._soup.get_text("\n", strip=True)


def get_selector(html: str) -> Any:
    """构造解析器。Scrapling 可用就用它，否则用 bs4。"""
    if SCRAPLING_AVAILABLE:
        try:
            return _ScraplingSelector(html)
        except TypeError:  # 个别版本签名差异
            return _ScraplingSelector(body=html)
    return _SoupSelector(html)


def element_text(element: Any) -> str:
    """取元素的**全部**文本（递归含子节点）。

    为什么必须有这个帮助函数：Scrapling 的 `Selector.text` 只返回元素自身的
    首个直接文本节点，不含子节点文本。实测两个坑：
      * 学校列表里校名在 `<td class="title_k"><a>陆军工程大学</a></td>` 中，
        `.text` 返回空串 → 解析出的校名全是空，整个列表报废；
      * 表头 `收费标准<br />（元/年）` 用 `.text` 只剩 "收费标准"，丢掉 "（元/年）"。
    因此统一走 `get_all_text()`；两个后端都提供它，语义一致。
    """
    getter = getattr(element, "get_all_text", None)
    if callable(getter):
        try:
            text = getter()
            if text:
                return str(text)
        except Exception:  # noqa: BLE001 - 退回到 .text
            pass
    return str(getattr(element, "text", "") or "")


def backend() -> str:
    return _BACKEND
