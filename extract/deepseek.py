"""DeepSeek Flash 信息抽取客户端（JSON Output 模式）。

仅用于**非结构化**内容（公众号文章、学校新闻）。考试院/高校官网的表格数据
一律走 CSS/XPath 解析，零 API 成本 —— 这是成本控制的第二原则。

DeepSeek JSON Output 的实测注意事项（官方文档）：
  * response_format={'type':'json_object'}
  * prompt 里必须出现 "json" 字样，并给出目标 JSON 样例
  * 需要给足 max_tokens，否则 JSON 被中途截断
  * **API 有概率返回空 content**，必须重试 —— 这里连空串一起判为失败并退避重试

成本控制：
  * 内容哈希在调用前完成增量判断，只有新内容才会走到这里
  * 每日调用次数硬上限
  * 使用 token 台账落库，便于核算月成本
  * 缓存命中价极低，因此 system prompt 保持**字节级稳定**（不要插入变量），
    这样多篇文章共享同一前缀，可命中上下文缓存
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from config import settings
from extract import prompts

try:
    from openai import OpenAI  # DeepSeek 兼容 OpenAI 协议
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore


class LLMUnavailable(RuntimeError):
    pass


class DailyLimitReached(RuntimeError):
    pass


@dataclass
class ExtractionResult:
    task: str
    ok: bool
    root: dict[str, Any] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)
    raw: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def parse_json_loose(text: str) -> dict[str, Any] | None:
    """容错解析：剥离 ```json 围栏、截取首个平衡的 {...}。"""
    if not text:
        return None
    candidate = _FENCE.sub("", text.strip())
    try:
        data = json.loads(candidate)
        return data if isinstance(data, dict) else None
    except ValueError:
        pass
    start = candidate.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for idx in range(start, len(candidate)):
        ch = candidate[idx]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(candidate[start : idx + 1])
                    return data if isinstance(data, dict) else None
                except ValueError:
                    return None
    return None


class DeepSeekExtractor:
    def __init__(
        self,
        conn: sqlite3.Connection | None = None,
        api_key: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self.conn = conn
        self.api_key = api_key or settings.DEEPSEEK_API_KEY
        self.model = model or settings.DEEPSEEK_MODEL
        self.max_tokens = max_tokens or settings.LLM_MAX_TOKENS
        self._client = None

    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return bool(self.api_key) and OpenAI is not None

    @property
    def client(self):
        if self._client is None:
            if OpenAI is None:
                raise LLMUnavailable("未安装 openai 包：pip install openai")
            if not self.api_key:
                raise LLMUnavailable("未设置 DEEPSEEK_API_KEY")
            self._client = OpenAI(api_key=self.api_key, base_url=settings.DEEPSEEK_BASE_URL)
        return self._client

    # ------------------------------------------------------------------ 限额台账
    def calls_today(self) -> int:
        if not self.conn:
            return 0
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM llm_usage WHERE date(called_at) = ?",
            (date.today().isoformat(),),
        ).fetchone()
        return int(row["n"]) if row else 0

    def _record(
        self, purpose: str, ok: bool, usage: Any = None, note: str = ""
    ) -> None:
        if not self.conn:
            return
        self.conn.execute(
            """INSERT INTO llm_usage
                 (model, purpose, prompt_tokens, completion_tokens, cache_hit_tokens, ok, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                self.model,
                purpose,
                int(getattr(usage, "prompt_tokens", 0) or 0),
                int(getattr(usage, "completion_tokens", 0) or 0),
                int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0),
                1 if ok else 0,
                note[:200],
            ),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ 主入口
    def extract(
        self,
        task: str,
        text: str,
        year: int,
        retries: int = 3,
        temperature: float | None = None,
    ) -> ExtractionResult:
        """按任务类型抽取。task 取 prompts.TASKS 的键：difficulty / admissions / groups。"""
        spec = prompts.TASKS.get(task)
        if not spec:
            raise KeyError(f"未知抽取任务：{task}")

        text = (text or "").strip()
        if not text:
            return ExtractionResult(task=task, ok=False, error="empty input")

        if not self.available:
            return ExtractionResult(task=task, ok=False, error="LLM 不可用（缺少 API Key 或 openai 包）")

        if self.calls_today() >= settings.LLM_DAILY_CALL_LIMIT:
            raise DailyLimitReached(
                f"今日 LLM 调用已达上限 {settings.LLM_DAILY_CALL_LIMIT} 次，停止以控制成本"
            )

        body = text[: settings.LLM_INPUT_CHAR_LIMIT]
        user_prompt = spec["user"](body, year)  # type: ignore[operator]
        messages = [
            {"role": "system", "content": spec["system"]},
            {"role": "user", "content": user_prompt},
        ]

        last_error = ""
        for attempt in range(1, retries + 1):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    max_tokens=self.max_tokens,
                    temperature=settings.LLM_TEMPERATURE if temperature is None else temperature,
                )
                usage = getattr(resp, "usage", None)
                content = (resp.choices[0].message.content or "").strip()

                # 官方已知问题：JSON Output 有概率返回空 content → 当失败重试
                if not content:
                    last_error = "empty content"
                    self._record(str(spec["purpose"]), False, usage, "empty content")
                    time.sleep(1.5 * attempt)
                    continue

                data = parse_json_loose(content)
                if data is None:
                    last_error = f"unparsable json: {content[:120]}"
                    self._record(str(spec["purpose"]), False, usage, last_error)
                    time.sleep(1.5 * attempt)
                    continue

                root_key = spec.get("root_key")
                records = data.get(root_key, []) if root_key else []
                if root_key and not isinstance(records, list):
                    records = []

                self._record(str(spec["purpose"]), True, usage)
                return ExtractionResult(
                    task=task,
                    ok=True,
                    root=data,
                    records=[r for r in records if isinstance(r, dict)],
                    raw=content,
                    prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                    completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                )
            except Exception as exc:  # 网络/限流/服务端错误
                last_error = f"{type(exc).__name__}: {exc}"
                self._record(str(spec["purpose"]), False, None, last_error)
                if attempt < retries:
                    time.sleep(2.0 * attempt)

        return ExtractionResult(task=task, ok=False, error=last_error)

    # ------------------------------------------------------------------ 便捷方法
    def extract_difficulty(self, text: str, year: int) -> dict[str, Any] | None:
        res = self.extract("difficulty", text, year)
        if not res.ok:
            return None
        level = (res.root.get("level") or "").strip()
        if level not in ("偏难", "适中", "偏易"):
            return None
        try:
            confidence = float(res.root.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence <= 0:
            return None
        return {
            "year": year,
            "level": level,
            "subject": (res.root.get("subject") or "全科").strip() or "全科",
            "summary": (res.root.get("summary") or "").strip(),
            "evidence": (res.root.get("evidence") or "").strip(),
            "confidence": confidence,
        }

    def extract_admissions(self, text: str, year: int) -> list[dict[str, Any]]:
        res = self.extract("admissions", text, year)
        return res.records if res.ok else []


def estimate_cost(prompt_tokens: int, completion_tokens: int, cache_hit_tokens: int = 0) -> float:
    """按 DeepSeek Flash 非高峰价估算单次调用成本（美元）。

    输入 $0.15/百万（缓存命中 $0.003/百万），输出 $0.6/百万。
    """
    miss = max(0, prompt_tokens - cache_hit_tokens)
    return (
        miss / 1_000_000 * 0.15
        + cache_hit_tokens / 1_000_000 * 0.003
        + completion_tokens / 1_000_000 * 0.6
    )


def monthly_cost_report(conn: sqlite3.Connection) -> dict[str, float]:
    """按 llm_usage 台账核算当月成本。"""
    rows = conn.execute(
        """SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) c,
                  COALESCE(SUM(cache_hit_tokens),0) h, COUNT(*) n
             FROM llm_usage
            WHERE strftime('%Y-%m', called_at) = strftime('%Y-%m', 'now', 'localtime')"""
    ).fetchone()
    cost = estimate_cost(rows["p"], rows["c"], rows["h"])
    return {
        "calls": float(rows["n"]),
        "prompt_tokens": float(rows["p"]),
        "completion_tokens": float(rows["c"]),
        "usd": round(cost, 4),
    }
