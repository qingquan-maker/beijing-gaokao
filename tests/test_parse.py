"""解析器离线回归测试。

用的是 tests/fixtures 下**真实抓取**的页面快照（2026 年招生计划查询），
所以这些用例能真实反映考试院页面结构是否变了，而不只是自测自的。

运行：
    python tests/test_parse.py          # 无需 pytest
    pytest tests/test_parse.py          # 也可以
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crawler import parse  # noqa: E402
from crawler.base import content_fingerprint  # noqa: E402
from crawler.selector import backend  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LIST_HTML = (FIXTURES / "bjeea_plan_list_2026.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "bjeea_plan_school_1021_2026.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- 学校列表

def test_school_list_parses_paginated_rows():
    schools = parse.parse_school_list(LIST_HTML)
    assert len(schools) == 50, f"列表页每页 50 条，实际 {len(schools)}"
    first = schools[0]
    assert first.school_code == "0321"
    assert first.name == "陆军工程大学"
    assert first.province == "江苏"
    assert first.batch == "本科普通批"
    assert first.plan_total == 8
    assert first.seq == 1


def test_school_list_contains_expected_schools():
    by_code = {s.school_code: s for s in parse.parse_school_list(LIST_HTML)}
    assert "1021" in by_code and by_code["1021"].name == "北京大学"
    assert by_code["1023"].name == "清华大学"
    # 带括号限定词的校名不能被截断（华电北京 != 华电保定）
    assert by_code["1040"].name == "华北电力大学(北京)"


def test_page_info_exposes_paging_token():
    info = parse.parse_page_info(LIST_HTML)
    assert info.url == "/queryService/rest/plan/115"
    assert info.token, "分页 token 必须解析出来，否则翻页会失败"
    assert info.page_size == 50
    assert info.total_pages == 13


def test_exam_options_discovers_year_id():
    options = parse.parse_exam_options(LIST_HTML)
    assert options.get(2026) == "6232", f"examId 解析错误: {options}"


# --------------------------------------------------------------------------- 专业计划

def test_school_programs_parse_through_html_comments():
    """数据行 <tr> 后有 <!-- 注释 -->，正则匹配会漏，必须按元素解析。"""
    programs = parse.parse_school_programs(DETAIL_HTML)
    assert len(programs) > 20, f"北京大学应解析出多条专业，实际 {len(programs)}"

    first = programs[0]
    assert first.major_code == "40"
    assert first.major_name == "法语"
    assert first.group_code == "04", "专业组代码要从 {04} 里拆出来"
    assert first.subject_req == "不限选考科目"
    assert first.batch == "本科提前批普通A段"
    assert first.plan_count == 2
    assert first.duration == "4"
    assert first.tuition == "5000.0"
    assert first.language == "英语"


def test_title_year_parsed():
    assert parse.parse_title_year(DETAIL_HTML) == 2026


def test_split_group_label_variants():
    assert parse.split_group_label("{04}不限选考科目") == ("04", "不限选考科目")
    assert parse.split_group_label("{02}物理(必须选考)") == ("02", "物理(必须选考)")
    # 没有花括号时不应吞掉内容
    assert parse.split_group_label("物理必选") == ("", "物理必选")
    assert parse.split_group_label("") == ("", "")


# --------------------------------------------------------------------------- 数字清洗

def test_number_parsing():
    assert parse.to_int("8") == 8
    assert parse.to_int("1,234") == 1234
    assert parse.to_int("") is None
    assert parse.to_int(None) is None
    assert parse.to_float("5000.0") == 5000.0
    assert parse.clean_text("  {04}不限选考科目 \n") == "{04}不限选考科目"


# --------------------------------------------------------------------------- 增量去重

def test_fingerprint_ignores_volatile_fields():
    """增量去重的核心：只有防重放令牌/日期/星期在变时，指纹必须保持不变。

    否则哈希每次都变 → 每次都被判定为"内容已更新" → 重复入库、重复烧 token。
    实测同一页面两次 GET 的 token 分别是 1789823369315 / 1789823378566。
    """
    import re as _re

    before = _re.sub(r'token="\d+"', 'token="1111111111111"', LIST_HTML)
    after = _re.sub(
        r"今天是\s*\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日(?:\s*星期[一二三四五六日天])?",
        "今天是2027年01月07日 星期四",
        LIST_HTML,
    )
    assert before != after, "构造的两次快照应当不同，否则用例没意义"
    assert content_fingerprint(before) == content_fingerprint(after)


def test_fingerprint_detects_real_change():
    changed = LIST_HTML.replace("陆军工程大学", "陆军工程大学（改名）")
    assert content_fingerprint(LIST_HTML) != content_fingerprint(changed)


# --------------------------------------------------------------------------- 一分一段表

def test_score_rank_table_with_merged_tail():
    html = """
    <table>
      <tr><th>分数</th><th>人数</th><th>累计人数</th></tr>
      <tr><td>698-750</td><td>113</td><td>113</td></tr>
      <tr><td>697</td><td>23</td><td>136</td></tr>
      <tr><td>696</td><td>12</td><td>148</td></tr>
      <tr><td>120-129</td><td>8</td><td>65400</td></tr>
      <tr><td>100-109</td><td>24</td><td>65434</td></tr>
    </table>
    """
    rows = parse.parse_score_rank_table(html)
    assert len(rows) == 5
    assert rows[0].score_low == 698 and rows[0].score_high == 750
    assert rows[0].cumulative_count == 113
    assert rows[-1].score_low == 100 and rows[-1].score_high == 109
    assert rows[-1].cumulative_count == 65434


def test_score_rank_ignores_non_score_rows():
    html = "<table><tr><td>合计</td><td>-</td><td>-</td></tr></table>"
    assert parse.parse_score_rank_table(html) == []


# --------------------------------------------------------------------------- 运行器

def _run_all() -> int:
    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failures: list[tuple[str, BaseException]] = []
    print(f"解析后端: {backend()}   用例数: {len(tests)}")
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except BaseException as exc:  # noqa: BLE001
            failures.append((name, exc))
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n通过 {len(tests) - len(failures)}/{len(tests)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
