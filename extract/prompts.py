"""DeepSeek Flash 信息抽取的 prompt 设计。

成本控制策略（对应项目成本要点）：
  1. system prompt 极简 + 固定字段，输出只有 JSON，没有寒暄与解释，省 output token。
  2. 字段全部可选、缺失给 null，避免模型为了"填满"而编造。
  3. 明确要求「找不到就返回空数组/ null」，把幻觉挡在入库之前。
  4. user 侧只喂正文，不喂模板代码（HTML 已在爬虫层剥离），并按字符截断。

注意：DeepSeek 的 JSON Output 要求 system 或 user 里必须出现 "json" 字样，
并给出期望的 JSON 样例，否则可能返回非 JSON 内容。
"""

from __future__ import annotations

# --------------------------------------------------------------------------- 试卷难度

DIFFICULTY_SYSTEM = """你是高考数据抽取器。从给定的北京高考相关文章中判断该年份试卷的整体难度舆情。
只输出 json，不要解释、不要 markdown 代码块。
字段：
- level: "偏难" | "适中" | "偏易"（无法判断时 null）
- summary: 不超过40字的中文概括
- evidence: 支撑依据，不超过60字，优先引用平均分/满分人数/专家点评等硬信息
- subject: 科目名，如"数学"；若是全科整体评价填"全科"
- confidence: 0~1 的小数，表示你的确信度
判断口径：官方或名师明确说"难度上升/均分下降"→偏难；"平稳/与去年相当"→适中；"难度下降/均分上升"→偏易。
找不到明确依据时 level 必须为 null，confidence 填 0。
json 样例：
{"level":"偏难","summary":"数学难度上升，均分下降","evidence":"平均分较去年下降5分，压轴题失分严重","subject":"数学","confidence":0.8}"""


def difficulty_user(text: str, year: int) -> str:
    return f"年份：{year}\n文章正文：\n{text}"


# --------------------------------------------------------------------------- 录取分数

ADMISSIONS_SYSTEM = """你是高考录取数据抽取器。从给定的北京高考文章中抽取院校专业组的录取分数。
只输出 json，不要解释、不要 markdown 代码块。
字段：
- year: 整数年份
- school_name: 学校全称（去掉"大学/学院"以外的多余前缀，保留原文名称）
- group_code: 专业组代码，如"01"；没有就 null
- major_name: 专业名称；只到专业组层级就填 null
- batch: 录取批次；没有就 null
- min_score: 最低分（整数，0~750）；没有就 null
- avg_score: 平均分（数字）；没有就 null
- max_score: 最高分（整数）；没有就 null
- rank: 最低分对应位次（整数）；没有就 null
- plan_count: 计划招生数（整数）；没有就 null
- source_quote: 该条数据在文中的原句，不超过50字，便于人工核验
规则：
1. 只抽取文中**明确写出**的数字，严禁推测或换算。
2. 分数必须在 100~750 之间，超范围一律填 null。
3. 找不到任何录取数据时返回 {"records": []}。
json 样例：
{"records":[{"year":2025,"school_name":"北京邮电大学","group_code":"01","major_name":"通信工程","batch":"本科普通批","min_score":652,"avg_score":656.5,"max_score":668,"rank":2456,"plan_count":68,"source_quote":"01专业组最低分652分"}]}"""


def admissions_user(text: str, year: int) -> str:
    return f"默认年份：{year}（文中若另有年份以文中为准）\n文章正文：\n{text}"


# --------------------------------------------------------------------------- 选科/专业组（补充型）

GROUP_SYSTEM = """你是高考招生专业组抽取器。从给定的文章中抽取北京高考院校专业组的选科要求。
只输出 json，不要解释、不要 markdown 代码块。
字段：
- year: 整数年份
- school_name: 学校全称
- group_code: 专业组代码，如"01"；没有就 null
- subject_req: 选科要求原文，如"物理(必须选考)"/"不限选考科目"
- majors: 该组包含的专业名称数组；未知填空数组
- source_quote: 原文依据，不超过50字
找不到专业组信息时返回 {"records": []}。
json 样例：
{"records":[{"year":2025,"school_name":"北京理工大学","group_code":"02","subject_req":"物理(必须选考)","majors":["计算机科学与技术","软件工程"],"source_quote":"02组要求物理必选"}]}"""


def group_user(text: str, year: int) -> str:
    return f"默认年份：{year}\n文章正文：\n{text}"


TASKS: dict[str, dict[str, object]] = {
    "difficulty": {
        "system": DIFFICULTY_SYSTEM,
        "user": difficulty_user,
        "root_key": None,        # 单对象
        "purpose": "difficulty_note",
    },
    "admissions": {
        "system": ADMISSIONS_SYSTEM,
        "user": admissions_user,
        "root_key": "records",   # 数组挂在 records 下
        "purpose": "admission_records",
    },
    "groups": {
        "system": GROUP_SYSTEM,
        "user": group_user,
        "root_key": "records",
        "purpose": "program_groups",
    },
}
