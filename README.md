# 北京高考志愿录取数据查询

一个低成本、零运维的北京高考录取数据采集与查询站点：**Scrapling 采集 → SQLite 存储 → 静态 JSON → 纯前端展示 → GitHub Actions 每日更新 + GitHub Pages 托管**。

页面按「学校 → 专业组 → 专业」三级展开，展示历年录取最低分/平均分/最高分、位次、招生计划数、专业组选科要求，以及当年的试卷难度舆情标注（偏难 / 适中 / 偏易）。

---

## 目录

- [一、关键实测结论](#一关键实测结论先看这里)
- [二、项目目录结构](#二项目目录结构)
- [三、数据库设计](#三数据库设计)
- [四、采集层](#四采集层)
- [五、DeepSeek Flash 信息抽取](#五deepseek-flash-信息抽取)
- [六、前端](#六前端)
- [七、定时更新](#七定时更新)
- [八、部署到 GitHub Pages](#八部署到-github-pages)
- [九、测试](#九测试)
- [十、成本核算](#十成本核算)
- [十一、数据来源与已知限制](#十一数据来源与已知限制)
- [十二、合规与免责](#十二合规与免责)

---

## 一、关键实测结论（先看这里）

这些是**实际请求过目标站点后得到的结论**，不是猜测。它们决定了代码为什么长这样：

| # | 结论 | 影响 |
|---|------|------|
| 1 | 学校详情页**只 GET 拿不到数据**。`GET /queryService/rest/plan/115/1021` 返回 200 但只有表头、0 行（10 KB）；必须 `POST` 带 `examId`/`schoolcode`/`subjectName` 才返回真实专业计划（46 KB、40+ 行） | `crawler/bjeea_plan.py` 用 POST 抓详情 |
| 2 | 数据行的 `<tr>` 与首个 `<td>` 之间夹着 **HTML 注释**（`<!-- 专业代码 -->`），用 `<tr>\s*<td ...>` 之类正则会全部落空 | 一律按元素遍历，不用行级正则 |
| 3 | `{04}不限选考科目` 里 **`{04}` 是专业组代码**，后面才是选科要求 | `parse.split_group_label()` 拆成 `group_code` + `subject_req` |
| 4 | 列表页的 `token="1789823369315"` 是**每次请求都变**的防重放令牌，页头日期与星期每天在变 | 直接对原始字节做 SHA-256 会让「增量去重」完全失效；必须先归一化（`base.normalize_for_hash`） |
| 5 | **Scrapling 的 `Selector.text` 只返回元素的首个直接文本节点**，不含子节点文本。校名在 `<td class="title_k"><a>陆军工程大学</a></td>` 中，`.text` 返回空串（整个校名列表报废）；表头 `收费标准<br />（元/年）` 只剩 `收费标准` | 统一用 `get_all_text()`（封装为 `selector.element_text()`），已在两个解析后端上回归 |
| 6 | Scrapling 的依赖比文档印象里重：本体（0.4.15）只装解析器依赖，`from scrapling.fetchers import Fetcher` 会依次因缺 `curl_cffi`、再因缺 `playwright` 而 ImportError —— `scrapling.engines.toolbelt.convertor` 在**模块级**就 import 了 `playwright._impl._errors`，即**只做静态 HTTP 抓取也必须装 playwright 这个 Python 包**（但无需 `playwright install` 下载浏览器内核） | `requirements.txt` 已写明完整依赖；代码同时保留 urllib + bs4 兜底，两条路径都有测试 |
| 7 | `queryService/rest/score/250717` 与 `/admission/791612` 是**个人成绩/录取结果查询**（要准考证号），不是公开数据表 | 已排除，不再尝试；一分一段改走公告页/种子 CSV |
| 8 | 2026 年一分一段**以 PDF 附件发布**（公告正文只有一行附件链接） | 公告页 HTML 解析常拿不到数据；`bjeea_score_rank.py` 会明确报出「数据在附件中」而不是假装解析失败 |
| 9 | 历史年份在**不同的 service id** 下：`plan/115` = 2026 招生计划，`plan/134` = 2024 选考科目查询 | 不要硬编学年 ID；从 bjeea 高考高招首页发现 service id，并用页面年份交叉校验 |
| 10 | SQLite 连接**默认不能跨线程**（`check_same_thread=True`）。采集用线程池，worker 里的写库操作会抛 `ProgrammingError`，任务卡死在 `running` 且不记录错误 | `connect(check_same_thread=False)` + `db.write_lock()` 串行化；并有 `recover_running()` 启动清扫 |
| 11 | GitHub Actions 的 `cron` **一律按 UTC 解释**。写 `"0 0 * * *"` 实际是北京时间 08:00，不是 00:00 | 工作流用 `"0 16 * * *"`（UTC 16:00 = 北京次日 00:00） |

---

## 二、项目目录结构

```
高考报志愿/
├── README.md
├── requirements.txt
├── .gitignore
│
├── config/
│   └── settings.py                # 路径/年份/限速/数据源/LLM 配置（环境变量可覆盖）
│
├── db/
│   ├── __init__.py                # 连接、建表、通用 UPSERT、位次换算
│   └── schema.sql                 # 建表 SQL（学校/专业/录取/一分一段/难度/采集状态）
│
├── crawler/                       # 采集层
│   ├── base.py                    # 限速、编码、SHA-256 指纹、带重试抓取、有界线程池
│   ├── selector.py                # 解析器适配：Scrapling 优先，bs4 兜底 + element_text()
│   ├── parse.py                   # 考试院表格结构化解析（纯函数，可离线回归）
│   ├── store.py                   # 内容指纹台账 + 三阶段任务队列（断点续爬）
│   ├── bjeea_plan.py              # 考试院招生计划（列表分页 + 学校详情 POST）★核心
│   ├── bjeea_score_rank.py        # 一分一段表（位次基准）
│   ├── school_sites.py            # 高校本科招生网（源注册表 + 通用表头别名映射）
│   └── wechat_articles.py         # 公众号文章采集 + 大模型抽取（唯一 LLM 链路）
│
├── extract/                       # 大模型信息抽取
│   ├── prompts.py                 # system prompt 与 JSON 样例（字节级稳定以命中缓存）
│   └── deepseek.py                # DeepSeek Flash 客户端（JSON Output、重试、成本台账）
│
├── pipeline/
│   ├── normalize.py               # 名称归一化、分数校验、位次换算、标签富化
│   ├── build_db.py                # 入库编排、派生重算、示例数据装载
│   └── export_json.py             # 导出前端 JSON（列定义 + 行数组，含内联包）
│
├── web/                           # 前端（纯静态，无构建步骤）
│   ├── index.html
│   ├── assets/
│   │   ├── style.css              # 白底、细边框、紧凑表格
│   │   └── app.js                 # 三级展开、筛选、位次展示（原生 JS）
│   └── data/                      # ← 由 export_json.py 生成
│       ├── index.json
│       ├── admissions-{年}.json
│       ├── score_rank-{年}.json
│       └── data.js                # 全量内联包（供 file:// 直接打开）
│
├── scripts/
│   ├── run_all.py                 # 三阶段编排入口（采集→入库→派生→导出）
│   └── serve.py                   # 本地预览静态服务器
│
├── data/
│   ├── gaokao.db                  # SQLite（增量状态载体，故意纳入版本控制）
│   ├── raw/                       # 原始 HTML 快照（gitignore）
│   ├── seed/
│   │   ├── score_rank_2025.csv    # 真实一分一段（345 段）
│   │   ├── school_sources.csv     # 高校源注册表（可选）
│   │   ├── school_tags.csv        # 985/211 标签（可选）
│   │   └── demo/                  # ★ 示例数据（非官方，见下文）
│   └── archive/                   # 人工整理的录取数据（可选，导入格式见 build_db.import_plan_json）
│
├── tests/
│   ├── fixtures/                  # 真实抓取的页面快照，供解析器离线回归
│   │   ├── bjeea_plan_list_2026.html
│   │   └── bjeea_plan_school_1021_2026.html
│   ├── test_parse.py              # 12 个解析用例（两种解析后端都跑通）
│   └── data_contract.test.mjs     # 20 个前端数据契约用例
│
├── tools/
│   └── _probe_score_rank.mjs      # 一次性脚本：抽取镜像站的一分一段表为种子 CSV
│
└── .github/workflows/
    └── update-data.yml            # 每日增量 → 提交 → 发布 Pages
```

---

## 三、数据库设计

见 `db/schema.sql`（含注释）。核心表：

| 表 | 说明 | 自然键 |
|----|------|--------|
| `schools` | 学校代码、名称、归一化名、所在地区、标签、招生网 | `school_code` |
| `programs` | **按年版本化**的专业目录：专业组、专业代码/名称、选科要求、学制、学费 | `(year, school_code, batch, group_code, major_code, major_name)` |
| `admissions` | 录取数据：最低/平均/最高分、位次、计划数、录取数、来源 | 同 programs |
| `score_rank` | 一分一段表：`score_low/score_high`（尾部为合并区间）、本段人数、累计人数 | `(year, score_low)` |
| `difficulty_notes` | 年度难度评价：`偏难/适中/偏易` + 依据 + 来源链接 + 置信度 | `(year, subject, source_url)` |
| `crawl_hashes` | SHA-256 内容指纹台账（增量去重） | `url` |
| `crawl_tasks` | 三阶段任务队列（断点续爬） | `(stage, target, task_key)` |
| `raw_documents` / `llm_usage` | 快照索引 / 大模型调用台账（成本核算） | — |

两个设计决策值得单独说明：

**① 位次不落地存外部文本，统一由分数换算。**
不同来源给的位次口径不一致（有的是最低分位次，有的是投档位次）。本项目只信任一分一段表，用 `score_rank` 由分数反查累计人数：

```sql
SELECT cumulative_count FROM score_rank
 WHERE year = ? AND score_low <= ? AND ? <= score_high
 ORDER BY score_high DESC LIMIT 1;
```

结果缓存在 `admissions.rank_min/rank_avg/rank_max` 供前端直读。北京公布的尾部是合并区间（如 `120-129`），落在区间内的分数只能取该段累计人数，属**官方口径下的近似值** —— 前端页脚如实标注。

**② 招生计划侧与录取分数侧共写同一自然键。**
计划数据只带 `plan_count`，录取数据只带分数。UPSERT 用 `COALESCE(excluded.col, table.col)` 实现「有值的覆盖、没值的保留」：

```sql
INSERT INTO admissions (...) VALUES (...)
ON CONFLICT (year, school_code, batch, group_code, major_code, major_name)
DO UPDATE SET plan_count = COALESCE(excluded.plan_count, admissions.plan_count), ...
```

于是两个来源可以任意顺序、任意次数增量写入而互不破坏 —— 这是「按学校分批采集 + 断点续爬」能成立的前提。

---

## 四、采集层

### 三阶段编排

| 阶段 | 做什么 | 幂等键 |
|------|--------|--------|
| `discover` | 抓列表页，登记每所学校为独立任务 | `fetch/bjeea_plan/plan:{year}:{school_code}` |
| `fetch` | 逐校抓详情，先算内容指纹，**变了才解析入库** | 同上 |
| `extract` | 对非结构化内容调大模型抽取 | `extract/article/{url}` |

`crawl_tasks` 持久化任务状态，进程被杀后重跑只会继续 `pending`/`failed` 的任务。每次 run 开头会 `recover_running()`，把上次异常退出留下的 `running` 任务放回队列（否则队列会慢慢卡死）。

### SHA-256 增量去重

```
原始 HTML → normalize_for_hash() → SHA-256 → 与 crawl_hashes 比对 → 变了才落快照/才可能调 LLM
```

`normalize_for_hash()` 抹掉易变片段（防重放 token、JSESSIONID、页头日期与星期、时间戳、HTML 注释、空白）。**不归一化直接哈希是无效的** —— 考试院页面每次请求 token 都不同（实测两次 GET 分别是 `1789823369315` / `1789823378566`），哈希会每次都变。

### 限速

- 并发上限 `MAX_CONCURRENCY = 5`（`ThreadPoolExecutor`）
- 按域名最小请求间隔 `BASE_DELAY_SECONDS = 1.0` + 抖动 0.8s，由 `RateLimiter` 串行化
- 单请求最多重试 3 次，指数退避；4xx（除 429）不重试
- 列表页翻页额外降并发到 2

### 抓取后端

优先 Scrapling 的 `Fetcher`（curl_cffi 指纹伪装）；未安装时自动退回 `urllib`，保证 CI/离线环境也能跑。`scripts/run_all.py` 启动时会打印当前用的是哪个后端。

```bash
# 完整安装（采集层全部能力）
pip install -r requirements.txt          # 含 scrapling[fetchers] + playwright（模块）
# 只有装不上时才需要它：解析退回 bs4、抓取退回 urllib，功能不变、反爬能力下降
```

本次开发过程中，`query.bjeea.cn` 的真实抓取是用 **urllib 兜底路径**跑通的（620 所学校列表 + 2,799 条专业计划），所以兜底路径不是纸上备用，而是经过实测的。

### 高校招生网：源注册表

各校发布节奏与页面结构都不统一，因此不做全站扫描，改为**注册表驱动**（`data/seed/school_sources.csv`）：

```csv
school_code,school_name,admissions_url,strategy,year_hint,note
1028,北京邮电大学,https://zsb.bupt.edu.cn/xxx.html,table,2025,本科普通批录取分数
1021,北京大学,https://www.gotopku.cn/xxx.html,llm,2025,只有新闻稿
```

- `strategy=table`：用**表头别名映射**解析（`最低分/录取最低分/投档最低分` …），零 API 成本
- `strategy=llm`：只有新闻稿/富文本，交 DeepSeek Flash 抽取

---

## 五、DeepSeek Flash 信息抽取

只有**非结构化内容**（公众号文章、学校新闻）才调大模型；考试院与高校官网的表格一律 CSS/XPath 解析，零 API 成本。

调用要点（`extract/deepseek.py`）：

```python
resp = client.chat.completions.create(
    model="deepseek-flash",
    messages=[
        {"role": "system", "content": prompts.DIFFICULTY_SYSTEM},
        {"role": "user",   "content": prompts.difficulty_user(text, year)},
    ],
    response_format={"type": "json_object"},   # ← JSON Output
    max_tokens=2048,                            # ← 给足，否则 JSON 被截断
    temperature=0.0,
)
```

对官方文档中几个坑的处理：

1. **prompt 里必须出现 "json" 字样并给出样例** —— 否则可能返回非 JSON。
2. **API 有概率返回空 content** —— 连空串一起判为失败并退避重试。
3. 输出做容错解析（`parse_json_loose`）：剥离 ` ```json ` 围栏、截取首个平衡的 `{...}`。
4. **每类任务的 system prompt 是字节级稳定的常量**（不插变量），从而让多篇文章共享同一前缀、命中上下文缓存 —— 缓存命中输入价 $0.003/百万，比未命中的 $0.15/百万低 50 倍。

prompt 设计（`extract/prompts.py`，节选）：

```
你是高考录取数据抽取器。从给定的北京高考文章中抽取院校专业组的录取分数。
只输出 json，不要解释、不要 markdown 代码块。
字段：
- year: 整数年份
- school_name: 学校全称
- group_code: 专业组代码，如"01"；没有就 null
- min_score / avg_score / max_score: 分数（0~750）；没有就 null
- rank / plan_count / source_quote
规则：
1. 只抽取文中**明确写出**的数字，严禁推测或换算。
2. 分数必须在 100~750 之间，超范围一律填 null。
3. 找不到任何录取数据时返回 {"records": []}。
json 样例：
{"records":[{"year":2025,"school_name":"北京邮电大学","group_code":"01",
 "major_name":"通信工程","batch":"本科普通批","min_score":652,"avg_score":656.5,
 "max_score":668,"rank":2456,"plan_count":68,"source_quote":"01专业组最低分652分"}]}
```

设计意图：字段全可选、缺失给 `null`、要求 `source_quote` 便于人工核验；再叠加 `pipeline.normalize.sanitize_scores()` 的硬校验（分数越界丢弃、`min ≤ avg ≤ max`），把幻觉挡在入库之前。

成本台账落在 `llm_usage` 表，`monthly_cost_report()` 按当月汇总美元成本；`LLM_DAILY_CALL_LIMIT` 提供每日调用硬上限。

---

## 六、前端

### 数据契约

`export_json.py` 产出「**列定义 + 行数组**」而不是对象数组 —— 一行 20 个字段的对象会把键名重复写几万遍，列式可省 30%~40% 体积：

```json
{
  "year": 2026,
  "columns": ["year","school_code","school_name","...","score_source"],
  "rows": [[2026,"0321","陆军工程大学","北京","本科普通批","01","物理(必须选考)", ...]]
}
```

- `index.json`：年份列表、列定义、难度评价、数据版本、计数、数据来源
- `admissions-{年}.json`：**按年分片**，前端只加载选中那一年（`latest_year` 默认选中最近一年）
- `data.js`：全量内联包。双击打开 `index.html` 时浏览器会拦截 `fetch`，`app.js` 会自动回落到它

### 页面结构

- **筛选栏**：年份 / 学校搜索（防抖 140ms）/ 专业组 / 选科要求 / 录取批次 / 只看有分数 / 重置
- **难度条**：当年难度徽标 + 概括 + 依据 + 来源链接
- **主表格**：`年份 | 学校 | 专业组 | 专业 | 最低分 | 平均分 | 最高分 | 位次 | 计划数 | 难度`
  - 学校 → 专业组 → 专业三级展开，默认折叠，只渲染展开的行（几万行数据也不会卡）
  - 专业名搜索命中时自动展开到命中专业
  - 学校/专业组行展示聚合值（最低分取最小、最高分取最大、平均分为均值、计划数求和）
- **难度配色**：偏难=橙、适中=灰、偏易=绿；无数据=虚线灰

### 样式

白底、1px 细边框、12.5px 紧凑表格、数字列右对齐 + `tabular-nums`（便于竖向比分数）、表头 sticky、窄屏隐藏次要列。

### 本地预览

```bash
python scripts/serve.py            # http://127.0.0.1:8000
python scripts/serve.py --port 8080 --open
```

> 直接双击 `index.html` 也能看（走 `data.js` 内联包），但**推荐用 HTTP 服务** —— 否则享受不到按年分片的按需加载。

---

## 七、定时更新

`.github/workflows/update-data.yml`：

```yaml
on:
  schedule:
    - cron: "0 16 * * *"     # UTC 16:00 == 北京时间次日 00:00
  workflow_dispatch:          # 支持手动指定年份/学校/force/with_wechat
```

> ⚠️ **时区提醒**：GitHub Actions 的 `cron` 按 **UTC** 解释。需求里写的 `"0 0 * * *"` 实际是北京时间 08:00；要北京时间 00:00 必须写 `"0 16 * * *"`。

流程：检出 → 装 Python 与依赖 → **跑解析器回归测试（页面结构哨兵）** → 采集+入库+导出 → 提交数据回仓库 → 打包 `web/` → 发布 Pages。

几个要点：

- **解析器回归测试前置**：`tests/test_parse.py` 基于真实页面快照。这一步失败说明考试院页面结构变了，后面的采集必然解析不到数据 —— 与其静默产出空数据，不如立刻失败。
- **数据产物回写仓库**：`data/gaokao.db` 是增量状态载体，提交它，每天的定时任务才只抓变化部分，而不是全量重抓。
- **`timeout-minutes: 30`** 兜底，避免异常卡死烧光免费额度。
- 想按学校分批，把 `crawl` 改成 matrix 即可（工作流文件末尾附了示例）。
- 定时任务在仓库 **60 天无活动后会被自动停用**，注意保持提交或手动触发。

免费额度：每月 2000 分钟。单次约 1~3 分钟，每天一次 ≈ 30~90 分钟/月。

---

## 八、部署到 GitHub Pages

### 1. 推到 GitHub

```bash
cd 高考报志愿
git init
git add .
git commit -m "feat: 北京高考志愿录取数据查询站点"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

> **一键脚本**：`scripts/deploy_github.py` 把上面这些步骤 + 开启 Pages + 设置工作流权限 + 触发首次运行全部自动完成：
>
> ```powershell
> $env:GH_TOKEN = "ghp_xxx"        # classic token，需勾选 repo + workflow
> python scripts/deploy_github.py --repo-name beijing-gaokao
> ```
>
> 它会打印最终网址。token 只从环境变量读取，不会写进任何文件。
>
> 该脚本用 **GitHub REST API**（blobs → tree → commit → ref）上传，而不是 `git push`。
> 原因是开发环境里 git 的认证链路不可用（受限沙箱禁止 git 自带的 `sh.exe` 创建管道：
> `couldn't create signal pipe, Win32 error 5`），而 Python 访问 `api.github.com` 正常。
> 顺带一个好处：token 完全不会落到 `.git/config` 里。

### 2. 打开 Pages 并选择 GitHub Actions 作为源

**Settings → Pages → Build and deployment → Source** 选 **GitHub Actions**（不要选 "Deploy from a branch"，工作流用的是 `actions/deploy-pages`）。

### 3. 允许工作流写仓库

**Settings → Actions → General → Workflow permissions** 选 **Read and write permissions**（工作流要提交数据变更）。工作流里已声明 `permissions: contents: write / pages: write / id-token: write`，但仓库级开关也要打开。

### 4. 配置 LLM 密钥（可选）

**Settings → Secrets and variables → Actions → New repository secret**：

- Name: `DEEPSEEK_API_KEY`，Value: 你的 DeepSeek API Key

不配也能跑 —— 公众号抽取链路会自动跳过（考试院与高校官网的表格采集不需要任何密钥）。

### 5. 先手动跑一次

**Actions → update-data → Run workflow**，可填：

| 输入 | 说明 |
|------|------|
| `years` | 年份，逗号分隔，如 `2026` |
| `schools` | 学校代码，逗号分隔，如 `1021,1023,1028`（**建议按学校分批**） |
| `force` | 忽略内容哈希强制重抓 |
| `with_wechat` | 同时跑公众号抽取（需上一步的密钥） |

跑完后 `https://<你的用户名>.github.io/<仓库名>/` 即可访问。

### 6. 绑定自定义域名（可选）

**Settings → Pages → Custom domain** 填域名，然后在 DNS 处添加 CNAME 记录指向 `<你的用户名>.github.io`，并勾选 **Enforce HTTPS**。

### 备选：Cloudflare Pages / Vercel

本项目前端是纯静态且数据已在仓库里，也可以直接把 **Build command 留空、Output directory 设为 `web`** 接到 Cloudflare Pages 或 Vercel，无需改代码。

---

## 九、测试

```bash
# 1) 解析器回归（真实页面快照；12 个用例）
python tests/test_parse.py
#    装了 Scrapling 就会用 Scrapling 的 Selector，
#    PYTHONPATH=.deps 可切换后端对比，两种后端都应 12/12 通过

# 2) 前端数据契约（20 个用例，校验导出 JSON 满足 app.js 的假设）
node tests/data_contract.test.mjs

# 3) 查看采集队列与数据统计
python scripts/run_all.py --status
```

`tests/test_parse.py` 里有两个用例专门守护上面第 4 条结论：篡改 token/日期/星期后内容指纹必须不变，篡改真实数据后必须变。

---

## 十、成本核算

| 项 | 成本 |
|----|------|
| 考试院 / 高校官网表格采集 | **$0**（CSS/XPath 解析，不调模型） |
| SQLite 存储 | $0 |
| GitHub Actions | 免费额度 2000 分钟/月，实际用 30~90 分钟 |
| GitHub Pages 托管 | $0 |
| DeepSeek Flash 抽取 | 见下 |

DeepSeek Flash 非高峰价：输入 $0.15/百万 token，输出 $0.6/百万，**缓存命中输入 $0.003/百万**。
每天几十篇公众号文章（按每篇 6000 字 ≈ 9k 输入 + 0.3k 输出，system prompt 命中缓存）估算：

```
输入(未命中)  9k × $0.15/M  ≈ $0.00135
输出          0.3k × $0.6/M ≈ $0.00018
单篇 ≈ $0.0015  →  每天 30 篇 ≈ $0.045  →  每月 ≈ $1.4
```

若 system prompt 全部命中缓存，实际会显著低于此值。用 `monthly_cost_report()` 或 `python scripts/run_all.py` 末尾的输出查看真实花费；`LLM_DAILY_CALL_LIMIT` 兜底防意外。

**省钱的三条原则**（代码里都落实了）：

1. **爬虫优先，大模型兜底** —— 表格数据零成本解析
2. **SHA-256 增量判断** —— 只有新出现或发生变更的内容才可能送模型
3. **prompt 字节级稳定 + 输入截断 + token 台账** —— 命中缓存、控住单次成本、能核算

---

## 十一、数据来源与已知限制

### 数据来源

| 来源 | 用途 | 现状 |
|------|------|------|
| `query.bjeea.cn/queryService/rest/plan/115` | 招生计划（学校、专业组、选科要求、计划数） | ✅ 已实测跑通，**当前库中 2,799 条真实记录（2026 年，约 130 所学校）** |
| 北京教育考试院 高考高招 › 通知公告 | 一分一段表、投档线 | ⚠️ 2026 一分一段以 PDF 附件发布，HTML 解析拿不到 |
| 一分一段表（镜像整理） | 位次基准 | ✅ `data/seed/score_rank_2025.csv`，**345 个真实分段（698→100 分，65434 人）** |
| 各高校本科招生网 | 录取分数 | ⏳ 需自行在 `data/seed/school_sources.csv` 注册学校源 |
| 阳光高考平台 / 公众号 | 难度舆情、录取数据补充 | ⏳ 走 DeepSeek Flash 抽取 |

### 当前库中的真实数据

- **620 所学校**（考试院 2026 招生计划列表，13 页全量）
- **2,799 条专业计划**（2026 年，约 130 所学校；含真实专业组代码与选科要求）
- **345 个一分一段分段**（2025 年，实测 697 分 → 136 位、694 → 186、693 → 201、692 → 228、691 → 256，与官方表完全一致）

### ⚠️ 关于 `data/seed/demo/` 里的示例数据

为了让前端的**分数、位次、难度标注、年份切换**等功能可以在没有联网采集的情况下被完整演示，仓库里放了 19 条示例记录（`data/seed/demo/*.json`）。

- 学校代码与名称取自考试院真实数据；**分数为演示用示意值**
- 全部标记 `score_source: "demo"` / `source_type: "demo"`
- 导出时 `index.json` 会列出 `demo_years`，**页面顶部会显示醒目的橙色警示**
- 只在你显式加 `--demo` 时才载入，`--clear-demo` 可一键清除

```bash
python scripts/run_all.py --demo          # 载入示例数据（离线，不联网）
python scripts/run_all.py --clear-demo    # 清除示例数据
```

**这些数字不可作为志愿填报依据。** 真实分数的获取路径见下方「补齐真实数据」。

### 已知限制与后续工作

1. **历史年份（2021—2024）的招生计划**：考试院只保留当年入口，历史数据在不同 service id 下（实测 `plan/134` = 2024 年选考科目查询，字段名为 `院校代号/院校名称/专业（类）`）。可从 bjeea 高考高招首页发现历史 service id 后逐个接入 —— `parse.CaseTable.column()` 的别名匹配已支持这些命名变体，但历史入口需要人工确认。
2. **一分一段表的 PDF 解析**：2026 年起部分公告只有 PDF 附件。可引入 `pdfplumber` 解析，或继续用镜像/公众号来源。代码已能明确识别并报出「数据在附件中」。
3. **投档线抓取**：北京在录取期间发布各批次投档线公告（如 `tzgg/2026/0723/88288.html`），是获取**真实最低分**的最佳公开来源。目前已具备通用表头映射解析器，缺的是各年份公告 URL 的登记。
4. **`score/250717`、`admission/791612` 是个人查询**（要准考证号），不可用于批量采集 —— 已在代码中排除，避免浪费时间。
5. **未接入的高校源**：`school_sources.csv` 默认为空。各校发布节奏不统一，建议先少量接入并观察，而不是一次性铺开。

### 补齐真实数据

```bash
# A. 招生计划（真实、立即可用；按学校分批）
python scripts/run_all.py --years 2026 --schools 1021,1023,1028,1047,1049

# B. 全量抓取（600+ 所学校，约 15 分钟以上；必须显式确认）
python scripts/run_all.py --years 2026 --all

# C. 高校招生网录取分数：先编辑 data/seed/school_sources.csv 再运行
python scripts/run_all.py --years 2025 --all   # school_sites 阶段读取注册表

# D. 公众号难度舆情与录取数据（需要 DEEPSEEK_API_KEY）
export DEEPSEEK_API_KEY=sk-xxx
python scripts/run_all.py --with-wechat

# E. 人工整理的数据：按 data/seed/demo/*.json 的格式放入 data/archive/ 即可被自动导入

# F. 手工录入某年难度评价（有权威来源时优于让模型猜）
python -c "
import sys; sys.path.insert(0,'.')
from db import connect
from crawler.wechat_articles import add_manual_difficulty
add_manual_difficulty(connect(), 2025, '偏难',
    'https://www.bjeea.cn/html/gkgz/tzgg/', summary='…', evidence='…')
"
```

---

## 十二、合规与免责

- **请求节流**：并发 ≤ 5、单域名基础延迟 ≥ 1s、指数退避重试。请勿调高 `GAOKAO_CONCURRENCY` 或调低 `GAOKAO_DELAY`。
- **仅采集公开数据**：只访问无需登录的公开页面；`score/250717`、`admission/791612` 这类需要准考证号的个人查询接口**不在采集范围内**。
- **尊重版权**：考试院明确声明「版权所有，未经同意，请勿转载」。本项目落盘的是原始页面快照用于增量比对，对外展示的是聚合后的结构化字段，并保留来源链接。若要公开分发，请自行评估授权与合规。
- **数据准确性**：页面已标注「数据仅供参考，请以北京教育考试院与各高校官方发布为准」。位次为按一分一段表换算的近似值（尾部合并区间尤其如此）。示例数据不得作为填报依据。
- **robots 与条款**：采集前请自行确认目标站点的 robots.txt 与使用条款。

---

## 附：命令速查

```bash
# 环境
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt

# 采集 → 入库 → 导出
python scripts/run_all.py --years 2026 --schools 1021,1023,1028
python scripts/run_all.py --offline                  # 不联网，只重建派生数据 + 导出
python scripts/run_all.py --demo                     # 载入示例数据（离线演示）
python scripts/run_all.py --clear-demo               # 清除示例数据
python scripts/run_all.py --status                   # 队列与统计

# 测试
python tests/test_parse.py
node tests/data_contract.test.mjs

# 预览
python scripts/serve.py --open
```
