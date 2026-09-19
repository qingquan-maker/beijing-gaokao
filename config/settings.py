"""全局配置：路径、采集节流、目标年份、数据源入口。

所有配置集中在此处，环境变量可覆盖，便于 GitHub Actions 与本地共用一套代码。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 路径

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"                 # 原始 HTML 快照（按内容哈希落盘）
SEED_DIR = DATA_DIR / "seed"               # 人工/半自动整理的种子数据（CSV）
DB_PATH = Path(os.getenv("GAOKAO_DB", DATA_DIR / "gaokao.db"))
WEB_DIR = ROOT / "web"
WEB_DATA_DIR = WEB_DIR / "data"            # 导出给前端的 JSON

# ---------------------------------------------------------------- 数据范围

#: 目标年份：优先展示最近一年，前端默认选中该年
TARGET_YEARS: tuple[int, ...] = (2021, 2022, 2023, 2024, 2025)
LATEST_YEAR = max(TARGET_YEARS)

#: 2026 年的招生计划查询已上线（examId=6232）。考试院只保留当年入口，
#: 历史年份需在 bjeea 存档页/公众号文章里回溯，故此处仅作为“当年抓取”开关。
PLAN_QUERY_YEARS: tuple[int, ...] = (2025, 2026)

# ---------------------------------------------------------------- 采集节流

#: 并发上限（注意事项要求 ≤ 5）
MAX_CONCURRENCY = int(os.getenv("GAOKAO_CONCURRENCY", "5"))

#: 单域名基础延迟（秒），注意事项要求 ≥ 1.0
BASE_DELAY_SECONDS = float(os.getenv("GAOKAO_DELAY", "1.0"))

#: 抖动上限，避免固定周期被识别
JITTER_SECONDS = 0.8

#: 单次请求重试次数与退避基数
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 2.0

#: 请求超时（毫秒，Scrapling 约定）
REQUEST_TIMEOUT_MS = 30_000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------- 数据源

#: 北京教育考试院综合查询系统（HTTP，非 HTTPS）
BJEAA_QUERY_BASE = "http://query.bjeea.cn/queryService/rest"

#: 招生计划查询入口。路径末段 {service_id} 是查询服务编号，年度参数为 examId。
#: 2026 年入口实测为 /plan/115，页面内 <select id="examId"> 仅列当年。
BJEAA_PLAN_SERVICE_ID = os.getenv("BJEAA_PLAN_SERVICE_ID", "115")

#: 考试院「高考高招」栏目首页（导航用，聚合各子栏目）
BJEAA_GKGZ_INDEX = "https://www.bjeea.cn/html/gkgz/index.html"

#: 「高考高招 > 通知公告」子栏目 —— 一分一段表、各批次投档线都发在这里。
#: 实测：2026 年一分一段（专科）是以 **PDF 附件**形式发布的
#: （如 /html/gkgz/tzgg/2026/0723/88288.html 正文只有一行附件链接），
#: 因此 HTML 表格解析往往拿不到数据，需要 PDF 解析或改用镜像/公众号来源。
BJEAA_TZGG_INDEX = "https://www.bjeea.cn/html/gkgz/tzgg/index.html"

#: 已知的年度 examId 兜底映射；未命中时由 select 选项自动发现
BJEAA_EXAM_ID_FALLBACK: dict[int, str] = {
    2026: "6232",
}

#: 阳光高考平台
CHSI_BASE = "https://gaokao.chsi.com.cn"

#: 公众号文章源（用 DeepSeek Flash 抽取），按学校/主题分别配置
WECHAT_SOURCES: tuple[dict[str, str], ...] = (
    # 例：{"name": "北京考试报", "list_url": "https://mp.weixin.qq.com/..."},
)

# ---------------------------------------------------------------- 大模型

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")

#: 抽取任务的 token 预算（JSON Output 必须留足，否则 JSON 被截断）
LLM_MAX_TOKENS = 2048
LLM_TEMPERATURE = 0.0

#: 摘要内容上限，控制单次调用成本
LLM_INPUT_CHAR_LIMIT = 6000

#: 每日 LLM 调用硬上限，防止意外烧钱
LLM_DAILY_CALL_LIMIT = int(os.getenv("GAOKAO_LLM_DAILY_LIMIT", "200"))


@dataclass
class CrawlPlan:
    """按学校分别设置采集计划，避免一次性全量抓取。"""

    #: 本次要采集的学校代码；为空表示全量（不推荐）
    school_codes: tuple[str, ...] = ()
    #: 本次要采集的年份
    years: tuple[int, ...] = TARGET_YEARS
    #: 只跑哪些阶段：discover / fetch / extract
    stages: tuple[str, ...] = field(default_factory=lambda: ("discover", "fetch", "extract"))
    #: 是否强制忽略内容哈希（全量重抓）
    force: bool = False


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, SEED_DIR, WEB_DATA_DIR, DB_PATH.parent):
        d.mkdir(parents=True, exist_ok=True)
