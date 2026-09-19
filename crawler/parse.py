"""北京教育考试院综合查询系统 —— 表格结构化解析。

全部为**纯函数**：输入 HTML 字符串，输出 dataclass 列表。这样解析逻辑可以脱离
网络、用 tests/fixtures 下的真实页面快照做离线回归测试。

实测页面结构（2026 年招生计划查询，query.bjeea.cn/queryService/rest/plan/115）：

  列表页（按学校查询）
    <table class="case">
      <tr><td class="title_pr">序号</td>...<td class="title_pr">计划招生数</td></tr>
      <tr bgcolor=""><td class="title_p">1</td><td class="title_p">0321</td>
          <td class="title_k"><a onclick="goschool('0321','0321')">陆军工程大学</a></td>
          <td class="title_p">江苏</td><td class="title_p">本科普通批</td>
          <td class="title_p">8</td></tr>
    </table>

  学校详情页（POST /plan/115/{schoolcode}，字段 examId / schoolcode / subjectName）
    <table class="case">
      <tr><td class="title_pr">专业代码</td>...<td class="title_pr">外语语种</td></tr>
      <tr bgcolor="">
        <!-- 专业代码 -->      <td class="title_p">40</td>
        <!-- 专业名称 -->      <td class="title_p">法语</td>
        <!-- 专业组选考科目 --> <td class="title_p"> {04}不限选考科目 </td>
        <!-- 录取批次 -->      <td class="title_p">本科提前批普通A段</td>
        ...
      </tr>
    </table>

两个坑（都已处理）：
  1. 数据行 `<tr>` 与首个 `<td>` 之间夹着 HTML 注释节点，用
     `<tr>\\s*<td ...>` 之类的正则匹配会全部落空 —— 必须按元素遍历。
  2. `{04}` 是**专业组代码**，`不限选考科目` 是该组的选科要求，需要拆开。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from crawler.selector import element_text, get_selector

_WS = re.compile(r"\s+")
_GROUP = re.compile(r"^[{\[（(]\s*([0-9A-Za-z]{1,4})\s*[}\]）)]\s*(.*)$", re.S)


def clean_text(value: str | None) -> str:
    """折叠空白，去掉全角空格与零宽字符。"""
    if not value:
        return ""
    value = value.replace("\u3000", " ").replace("\u200b", "")
    return _WS.sub(" ", value).strip()


def _squeeze(value: str | None) -> str:
    """表头比对用：彻底去掉所有空白。"""
    return _WS.sub("", clean_text(value))


def to_int(value: str | None) -> int | None:
    """从 '1,234' / '8' / '' 中取出整数；空值返回 None（不是 0）。"""
    if value is None:
        return None
    digits = re.sub(r"[^\d]", "", value)
    return int(digits) if digits else None


def to_float(value: str | None) -> float | None:
    if value is None:
        return None
    m = re.search(r"\d+(?:\.\d+)?", value.replace(",", ""))
    return float(m.group()) if m else None


# --------------------------------------------------------------------------- 结构

@dataclass
class CaseTable:
    """一张 <table class="case"> 的表头 + 数据行。

    同时保留每行的单元格元素，便于取 `<a onclick>` 之类的属性。
    """

    headers: list[str]                      # 已 squeeze 的表头
    raw_headers: list[str]                  # 原始表头文本
    rows: list[list[str]]                   # 单元格文本
    row_cells: list[list[object]] = field(default_factory=list)

    def column(self, *candidates: str) -> int | None:
        """按表头名找列号，支持多个候选名与「包含」匹配。"""
        for cand in candidates:
            cand_s = _squeeze(cand)
            for idx, head in enumerate(self.headers):
                if head == cand_s:
                    return idx
        for cand in candidates:
            cand_s = _squeeze(cand)
            for idx, head in enumerate(self.headers):
                if cand_s and cand_s in head:
                    return idx
        return None


def read_case_tables(html: str) -> list[CaseTable]:
    """取出页面中所有 table.case，按表头/数据行分离。

    考试院页面固定用 `title_pr` 标表头、`title_p` 标数据（学校名用 `title_k`），
    所以用 class 判定比用 `<tr>` 位置判定稳。
    """
    page = _select(html)
    tables: list[CaseTable] = []
    for table in page.css("table.case"):
        # 注意用 element_text 而不是 .text：表头里有 <br>、校名包在 <a> 里，
        # Scrapling 的 .text 只取首个直接文本节点，会漏掉/截断内容。
        heads = [_squeeze(element_text(t)) for t in table.css("td.title_pr")]
        raw_heads = [clean_text(element_text(t)) for t in table.css("td.title_pr")]
        if not heads:
            continue

        rows: list[list[str]] = []
        row_cells: list[list[object]] = []
        for tr in table.css("tr"):
            cells = tr.css("td.title_p, td.title_k")
            if not cells:
                continue
            # 表头行本身带 title_pr，不会进入这里
            rows.append([clean_text(element_text(c)) for c in cells])
            row_cells.append(list(cells))
        tables.append(
            CaseTable(headers=heads, raw_headers=raw_heads, rows=rows, row_cells=row_cells)
        )
    return tables


def _select(html: str):
    """构造解析器（Scrapling 优先，bs4 兜底）。"""
    return get_selector(html)


# --------------------------------------------------------------------------- 结果模型

@dataclass
class SchoolRow:
    school_code: str
    name: str
    province: str = ""
    batch: str = ""
    plan_total: int | None = None
    seq: int | None = None


@dataclass
class ProgramRow:
    major_code: str
    major_name: str
    group_code: str = ""
    group_label: str = ""       # 专业组描述（选科要求原文）
    subject_req: str = ""
    batch: str = ""
    plan_count: int | None = None
    duration: str = ""
    tuition: str = ""
    language: str = ""


@dataclass
class PageInfo:
    """分页元信息，取自 <div id="pageHideDiv">。"""

    url: str = ""
    token: str = ""
    page_size: int = 50
    page_no: int = 1
    total_pages: int | None = None


# --------------------------------------------------------------------------- 具体解析

def split_group_label(label: str) -> tuple[str, str]:
    """'{04}不限选考科目' -> ('04', '不限选考科目')。

    没有花括号时（部分年份/学校直接写选科要求）group_code 留空。
    """
    text = clean_text(label)
    m = _GROUP.match(text)
    if m:
        return m.group(1).strip(), clean_text(m.group(2))
    return "", text


def parse_school_list(html: str) -> list[SchoolRow]:
    """解析「按学校查询」结果表。"""
    target: CaseTable | None = None
    for table in read_case_tables(html):
        if table.column("学校名称") is not None and table.column("学校代码") is not None:
            # 优先选带「序号」的那张（按学校查询），排除按专业查询的空表
            if table.column("序号") is not None or not target:
                target = table
                if table.column("序号") is not None:
                    break
    if target is None:
        return []

    c_code = target.column("学校代码")
    c_name = target.column("学校名称")
    c_prov = target.column("所在地区", "地区")
    c_batch = target.column("录取批次", "批次")
    c_plan = target.column("计划招生数", "计划数")
    c_seq = target.column("序号")

    out: list[SchoolRow] = []
    for row in target.rows:
        code = row[c_code].strip() if c_code is not None and c_code < len(row) else ""
        name = row[c_name].strip() if c_name is not None and c_name < len(row) else ""
        if not name:
            continue
        out.append(
            SchoolRow(
                school_code=code or name,
                name=name,
                province=row[c_prov] if c_prov is not None and c_prov < len(row) else "",
                batch=row[c_batch] if c_batch is not None and c_batch < len(row) else "",
                plan_total=to_int(row[c_plan]) if c_plan is not None and c_plan < len(row) else None,
                seq=to_int(row[c_seq]) if c_seq is not None and c_seq < len(row) else None,
            )
        )
    return out


def parse_school_programs(html: str) -> list[ProgramRow]:
    """解析学校详情页的在京招生专业计划表。"""
    target: CaseTable | None = None
    for table in read_case_tables(html):
        if table.column("专业名称") is not None and table.column("专业代码") is not None:
            target = table
            break
    if target is None:
        return []

    c_code = target.column("专业代码")
    c_name = target.column("专业名称")
    c_group = target.column("选考科目要求", "专业组")
    c_batch = target.column("录取批次", "批次")
    c_plan = target.column("计划招生数", "计划数")
    c_dur = target.column("学制")
    c_fee = target.column("收费标准", "学费")
    c_lang = target.column("外语语种", "语种")

    def cell(row: list[str], idx: int | None) -> str:
        return row[idx] if idx is not None and idx < len(row) else ""

    out: list[ProgramRow] = []
    for row in target.rows:
        name = cell(row, c_name).strip()
        if not name:
            continue
        group_code, subject_req = split_group_label(cell(row, c_group))
        out.append(
            ProgramRow(
                major_code=cell(row, c_code).strip(),
                major_name=name,
                group_code=group_code,
                group_label=subject_req,
                subject_req=subject_req,
                batch=cell(row, c_batch).strip(),
                plan_count=to_int(cell(row, c_plan)),
                duration=cell(row, c_dur).strip(),
                tuition=cell(row, c_fee).strip(),
                language=cell(row, c_lang).strip(),
            )
        )
    return out


def parse_page_info(html: str) -> PageInfo:
    """读取分页信息：下一页要 POST 同一个 url + token + pageSize + pageNo。

    注意：不能用「一串可选分组」的正则去一次性抓属性 —— 惰性量词会让所有
    可选组都匹配空串，结果 token/url 全是 None。必须先把 div 标签取出来，
    再逐个属性匹配。
    """
    info = PageInfo()
    tag_match = re.search(r'<div[^>]*id="pageHideDiv"[^>]*>', html, re.I)
    if tag_match:
        tag = tag_match.group(0)

        def attr(name: str) -> str:
            m = re.search(rf'\b{name}\s*=\s*"([^"]*)"', tag, re.I)
            return m.group(1).strip() if m else ""

        info.url = attr("url")
        info.token = attr("token")
        size = attr("pagesize") or attr("pageSize")
        if size.isdigit():
            info.page_size = int(size)
        page_no = attr("pageno") or attr("pageNo")
        if page_no.isdigit():
            info.page_no = int(page_no)

    m2 = re.search(r"共\s*(\d+)\s*页", html)
    if m2:
        info.total_pages = int(m2.group(1))
    elif "共1页" in html:
        info.total_pages = 1
    return info


def parse_exam_options(html: str) -> dict[int, str]:
    """从 <select id="examId"> 里读出 {年份: examId}。

    考试院只列当年，所以通常只返回一项；历史年份要靠存档页回溯。
    """
    options: dict[int, str] = {}
    m = re.search(r'<select[^>]*id="examId"[^>]*>(.*?)</select>', html, re.S | re.I)
    if not m:
        return options
    for opt in re.finditer(r'<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>', m.group(1), re.S | re.I):
        value, label = opt.group(1).strip(), clean_text(opt.group(2))
        ym = re.search(r"(20\d{2})", label)
        if value and ym:
            options[int(ym.group(1))] = value
    return options


def parse_title_year(html: str) -> int | None:
    """从 '<b>2026年北京大学（1021）在京招生专业计划</b>' 抽年份。"""
    m = re.search(r"<td[^>]*class=\"title_cr\"[^>]*>(.*?)</td>", html, re.S | re.I)
    if not m:
        m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
    if not m:
        return None
    ym = re.search(r"(20\d{2})\s*年", re.sub(r"<[^>]+>", "", m.group(1)))
    return int(ym.group(1)) if ym else None


# --------------------------------------------------------------------------- 一分一段表

@dataclass
class ScoreRankRow:
    score_low: int
    score_high: int
    segment_count: int | None
    cumulative_count: int


def parse_score_rank_table(html: str) -> list[ScoreRankRow]:
    """解析一分一段表（分数 | 本段人数 | 累计人数）。

    北京公布的尾部会合并区间（如 '120-129'），因此保留 [low, high]。
    支持 table 与无 table 的纯文本列表两种形态。
    """
    out: list[ScoreRankRow] = []
    page = _select(html)

    for table in page.css("table"):
        rows = table.css("tr")
        if len(rows) < 5:
            continue
        for tr in rows:
            cells = [clean_text(element_text(c)) for c in tr.css("td, th")]
            if len(cells) < 3:
                continue
            parsed = _score_row(cells[0], cells[1], cells[2])
            if parsed:
                out.append(parsed)
        if out:
            break

    if not out:  # 兜底：整页按行扫描
        for line in re.split(r"[\r\n]+", re.sub(r"<[^>]+>", "\n", html)):
            parts = [p for p in re.split(r"[\s|,，\t]+", clean_text(line)) if p]
            if len(parts) >= 3:
                parsed = _score_row(parts[0], parts[1], parts[2])
                if parsed:
                    out.append(parsed)

    # 去重 + 按分数降序
    seen: dict[int, ScoreRankRow] = {}
    for row in out:
        seen.setdefault(row.score_low, row)
    return sorted(seen.values(), key=lambda r: -r.score_high)


def _score_row(a: str, b: str, c: str) -> ScoreRankRow | None:
    m = re.match(r"^(\d{3})\s*(?:[-~—－至到]\s*(\d{3}))?$", clean_text(a))
    if not m:
        return None
    low = int(m.group(1))
    high = int(m.group(2)) if m.group(2) else low
    seg, cum = to_int(b), to_int(c)
    if cum is None:
        return None
    if low > high:
        low, high = high, low
    return ScoreRankRow(score_low=low, score_high=high, segment_count=seg, cumulative_count=cum)


def iter_school_codes(schools: Iterable[SchoolRow]) -> list[str]:
    return [s.school_code for s in schools if s.school_code]
