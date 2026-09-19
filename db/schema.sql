-- ============================================================================
--  北京高考志愿录取数据查询 —— SQLite 建表脚本
--  设计原则：
--    1. 单文件、零运维；WAL 模式兼顾并发读写。
--    2. 全部业务表使用「自然键 UNIQUE」，配合 UPSERT 实现幂等增量入库：
--       招生计划侧与录取分数侧共写 admissions 的同一自然键，先写计划数、
--       后补分数，互不覆盖（见 pipeline/build_db.py 的 upsert 语句）。
--    3. 位次不落地存"排名"字符串，而是通过 score_rank 由分数换算，
--       避免数据源口径不一致；换算结果缓存在 admissions.rank_* 便于前端直读。
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------------ 学校
CREATE TABLE IF NOT EXISTS schools (
    school_code TEXT PRIMARY KEY,              -- 北京考试院 4 位院校代码
    name        TEXT NOT NULL,                 -- 学校名称（官方口径）
    name_norm   TEXT NOT NULL,                 -- 归一化名称，用于跨源匹配
    province    TEXT,                          -- 所在地区（省/直辖市）
    city        TEXT,
    tags        TEXT,                          -- 985/211/双一流 等标签，逗号分隔
    site_url    TEXT,                          -- 本科招生网
    source_url  TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_schools_name_norm ON schools (name_norm);
CREATE INDEX IF NOT EXISTS idx_schools_province  ON schools (province);

-- ------------------------------------------------------------------ 专业（招生计划目录，按年版本化）
-- 选科要求、专业组划分逐年变化，因此 year 属于自然键的一部分。
CREATE TABLE IF NOT EXISTS programs (
    program_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    year        INTEGER NOT NULL,
    school_code TEXT    NOT NULL REFERENCES schools (school_code) ON DELETE CASCADE,
    batch       TEXT    NOT NULL DEFAULT '',   -- 录取批次
    group_code  TEXT    NOT NULL DEFAULT '',   -- 专业组代码，如 '04'
    group_label TEXT    NOT NULL DEFAULT '',   -- 专业组描述，如 '不限选考科目'
    subject_req TEXT    NOT NULL DEFAULT '',   -- 选科要求原文
    major_code  TEXT    NOT NULL DEFAULT '',   -- 专业代码
    major_name  TEXT    NOT NULL,              -- 专业名称
    duration    TEXT,                          -- 学制（年）
    tuition     TEXT,                          -- 收费标准（元/年）
    language    TEXT,                          -- 外语语种
    source_url  TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (year, school_code, batch, group_code, major_code, major_name)
);
CREATE INDEX IF NOT EXISTS idx_programs_school_year ON programs (school_code, year);
CREATE INDEX IF NOT EXISTS idx_programs_group       ON programs (year, school_code, group_code);
CREATE INDEX IF NOT EXISTS idx_programs_major_name  ON programs (major_name);

-- ------------------------------------------------------------------ 录取数据
-- 年份、学校、专业组、专业、最低分、平均分、最高分、位次、计划数
CREATE TABLE IF NOT EXISTS admissions (
    admission_id INTEGER PRIMARY KEY AUTOINCREMENT,
    year         INTEGER NOT NULL,
    school_code  TEXT    NOT NULL REFERENCES schools (school_code) ON DELETE CASCADE,
    batch        TEXT    NOT NULL DEFAULT '',
    group_code   TEXT    NOT NULL DEFAULT '',
    major_code   TEXT    NOT NULL DEFAULT '',
    major_name   TEXT    NOT NULL DEFAULT '',
    program_id   INTEGER REFERENCES programs (program_id) ON DELETE SET NULL,

    min_score    INTEGER,                      -- 最低分
    avg_score    REAL,                         -- 平均分
    max_score    INTEGER,                      -- 最高分

    rank_min     INTEGER,                      -- 最低分对应位次（前端主展示"位次"）
    rank_avg     INTEGER,                      -- 平均分对应位次
    rank_max     INTEGER,                      -- 最高分对应位次

    plan_count   INTEGER,                       -- 计划招生数
    admit_count  INTEGER,                       -- 实际录取人数

    score_source TEXT,                          -- official | school | wechat | seed
    source_url   TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (year, school_code, batch, group_code, major_code, major_name)
);
CREATE INDEX IF NOT EXISTS idx_adm_year        ON admissions (year);
CREATE INDEX IF NOT EXISTS idx_adm_school_year ON admissions (school_code, year);
CREATE INDEX IF NOT EXISTS idx_adm_group       ON admissions (year, school_code, group_code);
CREATE INDEX IF NOT EXISTS idx_adm_min         ON admissions (year, min_score DESC);

-- ------------------------------------------------------------------ 专业组投档线
-- 考试院公布的「录取投档线」是**专业组级**的（含语数外+三科单科成绩），
-- 不是专业级。硬塞进 admissions 的专业级自然键，会让同一份分数在组内每个
-- 专业上重复一次，页面上看起来就像"每个专业都考了同一个分"。
-- 因此单独建表，前端在「专业组」这一层展示它 —— 这也正是官方口径。
CREATE TABLE IF NOT EXISTS group_admissions (
    year         INTEGER NOT NULL,
    school_code  TEXT    NOT NULL REFERENCES schools (school_code) ON DELETE CASCADE,
    batch        TEXT    NOT NULL DEFAULT '',
    group_code   TEXT    NOT NULL DEFAULT '',
    subject_req  TEXT,                          -- 投档线文件里的选考要求（官方原文）
    min_score    INTEGER,                       -- 投档最低分（总分）
    rank_min     INTEGER,                       -- 由一分一段表换算的位次
    sub_scores   TEXT,                          -- 语文,数学,外语,三科选考
    note         TEXT,                          -- 备注（如同分排序成绩）
    source_kind  TEXT,                          -- official_pdf | archive | demo
    source_url   TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (year, school_code, batch, group_code)
);
CREATE INDEX IF NOT EXISTS idx_group_adm_year  ON group_admissions (year, min_score DESC);
CREATE INDEX IF NOT EXISTS idx_group_adm_score ON group_admissions (year, school_code, group_code);

-- ------------------------------------------------------------------ 院校录取概况
-- 高校招生网普遍只公布到「院校 / 科类 / 招生类型」这一层，并且**顺带给出当年
-- 的批次控制线**（实测北航：2025 北京 综合改革 最低分 665 / 平均分 670.47 /
-- 控制线 519）。这类数据既不是专业组级也不是专业级，塞进哪张表都会错位，
-- 因此单独建表。
--
-- 注意：这是**院校级**最低分，不等于某个专业组的投档线，两者不可混用。
CREATE TABLE IF NOT EXISTS school_summaries (
    year         INTEGER NOT NULL,
    school_code  TEXT    NOT NULL REFERENCES schools (school_code) ON DELETE CASCADE,
    province     TEXT    NOT NULL DEFAULT '',   -- 生源省市，如「北京」
    subject_type TEXT    NOT NULL DEFAULT '',   -- 科类，如「综合改革」
    batch_type   TEXT    NOT NULL DEFAULT '',   -- 招生类型，如「统招（限选物理、化学）」
    min_score    INTEGER,                       -- 院校录取最低分
    avg_score    REAL,                          -- 院校录取平均分
    max_score    INTEGER,
    control_line INTEGER,                       -- 当年该科类批次控制线
    source_url   TEXT,
    source_kind  TEXT,                          -- school_site
    updated_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (year, school_code, province, subject_type, batch_type)
);
CREATE INDEX IF NOT EXISTS idx_school_sum_year ON school_summaries (year, school_code);

-- ------------------------------------------------------------------ 一分一段表
-- 北京公布的尾部区间会合并（如 "120-129"），故用 [score_low, score_high] 表示一段。
-- cumulative_count 为该段末尾对应的累计人数，即位次。
CREATE TABLE IF NOT EXISTS score_rank (
    year             INTEGER NOT NULL,
    score_low        INTEGER NOT NULL,
    score_high       INTEGER NOT NULL,
    segment_count    INTEGER,                  -- 本段人数
    cumulative_count INTEGER NOT NULL,         -- 累计人数（位次）
    source_url       TEXT,
    PRIMARY KEY (year, score_low)
);
CREATE INDEX IF NOT EXISTS idx_rank_score ON score_rank (year, score_high DESC);

-- ------------------------------------------------------------------ 试卷难度评价
CREATE TABLE IF NOT EXISTS difficulty_notes (
    note_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    year         INTEGER NOT NULL,
    level        TEXT    NOT NULL CHECK (level IN ('偏难', '适中', '偏易')),
    subject      TEXT    NOT NULL DEFAULT '全科',
    summary      TEXT,                          -- 一句话概括
    evidence     TEXT,                          -- 支撑依据（均分/满分人数/专家点评）
    source_title TEXT,
    source_url   TEXT    NOT NULL,
    source_type  TEXT,                          -- wechat | news | school | forum
    confidence   REAL,                          -- 0~1，来自大模型抽取
    created_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (year, subject, source_url)
);
CREATE INDEX IF NOT EXISTS idx_diff_year ON difficulty_notes (year, level);

-- ------------------------------------------------------------------ 采集基础设施
-- SHA-256 内容指纹：判断"新出现或发生变更"，只把变更内容送大模型
CREATE TABLE IF NOT EXISTS crawl_hashes (
    url           TEXT PRIMARY KEY,
    content_hash  TEXT NOT NULL,
    byte_size     INTEGER,
    http_status   INTEGER,
    change_count  INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_seen     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_changed  TEXT
);

-- 三阶段任务编排 + 断点续爬：discover → fetch → extract
CREATE TABLE IF NOT EXISTS crawl_tasks (
    task_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    stage      TEXT NOT NULL,                   -- discover | fetch | extract
    target     TEXT NOT NULL,                   -- bjeea_plan | score_rank | school_site | wechat
    task_key   TEXT NOT NULL,                   -- 幂等键，如 "plan:2026:1021"
    payload    TEXT,                            -- JSON 附加参数
    status     TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'running', 'done', 'failed', 'skipped')),
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (stage, target, task_key)
);
CREATE INDEX IF NOT EXISTS idx_tasks_pickup ON crawl_tasks (status, stage, target);

-- 原始文档快照索引（文件本身按哈希存 data/raw/<hash>.html）
CREATE TABLE IF NOT EXISTS raw_documents (
    doc_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    fetched_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    content_type TEXT,
    path         TEXT,
    UNIQUE (url, content_hash)
);

-- LLM 调用台账，用于成本核算与每日限额
CREATE TABLE IF NOT EXISTS llm_usage (
    usage_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    model            TEXT,
    purpose          TEXT,
    prompt_tokens    INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cache_hit_tokens INTEGER DEFAULT 0,
    ok               INTEGER DEFAULT 1,
    note             TEXT
);

-- ------------------------------------------------------------------ 视图：前端导出直接读它
DROP VIEW IF EXISTS v_admission_export;
CREATE VIEW v_admission_export AS
SELECT
    a.year,
    a.school_code,
    s.name              AS school_name,
    s.province,
    s.tags,
    a.batch,
    a.group_code,
    p.group_label,
    p.subject_req,
    a.major_code,
    a.major_name,
    a.min_score,
    a.avg_score,
    a.max_score,
    a.rank_min,
    a.rank_avg,
    a.rank_max,
    a.plan_count,
    a.admit_count,
    a.score_source,
    p.duration,
    p.tuition,
    COALESCE(a.source_url, p.source_url) AS source_url
FROM admissions a
JOIN schools  s ON s.school_code = a.school_code
LEFT JOIN programs p ON p.program_id = a.program_id;

DROP VIEW IF EXISTS v_school_year_groups;
CREATE VIEW v_school_year_groups AS
SELECT
    p.year,
    p.school_code,
    s.name AS school_name,
    p.batch,
    p.group_code,
    p.group_label,
    p.subject_req,
    COUNT(*)                        AS major_count,
    SUM(COALESCE(a.plan_count, 0))  AS plan_total
FROM programs p
JOIN schools s ON s.school_code = p.school_code
LEFT JOIN admissions a
       ON a.year = p.year AND a.school_code = p.school_code
      AND a.batch = p.batch AND a.group_code = p.group_code
      AND a.major_code = p.major_code AND a.major_name = p.major_name
GROUP BY p.year, p.school_code, s.name, p.batch, p.group_code, p.group_label, p.subject_req;
