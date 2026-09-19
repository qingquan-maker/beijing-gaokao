/**
 * 前端数据契约测试（Node，无需浏览器）
 *
 * 验证 web/data 下导出的 JSON 满足 app.js 的假设：
 *   - index.json 必备字段齐全
 *   - 每个年份分片的 columns 与 index.columns 一致，且每行长度与列数相等
 *   - app.js 建树依赖的字段（school_code / school_name / group_code / …）存在
 *   - data.js 内联包内容与分片一致
 *   - score_rank 分片可解析
 *
 * 运行：node tests/data_contract.test.mjs
 */
import { readFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const DATA = join(ROOT, 'web', 'data');

let pass = 0;
const failures = [];

function check(name, fn) {
  try {
    fn();
    pass += 1;
    console.log(`  PASS  ${name}`);
  } catch (err) {
    failures.push([name, err]);
    console.log(`  FAIL  ${name}: ${err.message}`);
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

function readJson(rel) {
  const p = join(DATA, rel);
  assert(existsSync(p), `缺少文件 ${rel}`);
  return JSON.parse(readFileSync(p, 'utf8'));
}

if (!existsSync(join(DATA, 'index.json'))) {
  console.error('web/data/index.json 不存在，请先运行：python scripts/run_all.py --demo');
  process.exit(2);
}

const index = readJson('index.json');

// ------------------------------------------------------------------ index.json
check('index.json 必备字段齐全', () => {
  for (const key of ['generated_at', 'data_version', 'latest_year', 'years', 'columns', 'counts']) {
    assert(key in index, `缺少字段 ${key}`);
  }
  assert(Array.isArray(index.years) && index.years.length > 0, 'years 不能为空');
  assert(index.years.includes(index.latest_year), 'latest_year 必须在 years 里');
});

check('index.json 的 latest_year 是最大年份（优先展示最近一年）', () => {
  assert(index.latest_year === Math.max(...index.years),
    `latest_year=${index.latest_year} 不是最大值 ${Math.max(...index.years)}`);
});

check('index.json 提供位次口径说明', () => {
  assert(typeof index.rank_note === 'string' && index.rank_note.length > 10, '缺 rank_note');
});

// ------------------------------------------------------------------ 各年份分片
for (const year of index.years) {
  const payload = readJson(`admissions-${year}.json`);

  check(`admissions-${year}.json 列定义与 index 一致`, () => {
    assert(Array.isArray(payload.columns), 'columns 缺失');
    assert(payload.columns.join(',') === index.columns.join(','),
      '列定义与 index.columns 不一致');
    assert(Number(payload.year) === Number(year), 'year 字段不匹配');
  });

  check(`admissions-${year}.json 每行长度与列数相等`, () => {
    const n = payload.columns.length;
    payload.rows.forEach((row, i) => {
      assert(row.length === n, `第 ${i} 行有 ${row.length} 列，应为 ${n}`);
    });
  });

  check(`admissions-${year}.json 建树必需字段非空`, () => {
    const idx = Object.fromEntries(payload.columns.map((c, i) => [c, i]));
    for (const col of ['school_code', 'school_name', 'major_name']) {
      assert(col in idx, `缺列 ${col}`);
    }
    const missing = payload.rows.filter((r) => !r[idx.school_name] || !r[idx.major_name]);
    assert(missing.length === 0, `有 ${missing.length} 行缺校名或专业名`);
  });

  check(`admissions-${year}.json 分数在合理区间内`, () => {
    const idx = Object.fromEntries(payload.columns.map((c, i) => [c, i]));
    for (const r of payload.rows) {
      for (const col of ['min_score', 'avg_score', 'max_score']) {
        const v = r[idx[col]];
        if (v === null || v === '') continue;
        assert(Number(v) >= 100 && Number(v) <= 750,
          `${col}=${v} 越界（应 100~750）`);
      }
    }
  });
}

// 年份必须降序（前端下拉与"优先最近一年"依赖它）
check('years 为降序', () => {
  for (let i = 1; i < index.years.length; i += 1) {
    assert(index.years[i - 1] > index.years[i], 'years 不是降序');
  }
});

// ------------------------------------------------------------------ 位次与一分一段
const rankYears = index.years.filter((y) => existsSync(join(DATA, `score_rank-${y}.json`)));
check('至少有一年的一分一段分片', () => {
  assert(rankYears.length > 0, '没有任何 score_rank-*.json');
});

for (const year of rankYears) {
  check(`score_rank-${year}.json 结构正确且累计人数单调递增`, () => {
    const payload = readJson(`score_rank-${year}.json`);
    const idx = Object.fromEntries(payload.columns.map((c, i) => [c, i]));
    assert(payload.rows.length > 50, `分段过少：${payload.rows.length}`);
    // 行按分数降序，累计人数应随之递增
    let prev = 0;
    for (const r of payload.rows) {
      const cum = Number(r[idx.cumulative_count]);
      const low = Number(r[idx.score_low]);
      const high = Number(r[idx.score_high]);
      assert(low <= high, `score_low > score_high: ${low}/${high}`);
      assert(cum >= prev, `累计人数在第 ${low} 段出现回退`);
      prev = cum;
    }
  });
}

// ------------------------------------------------------------------ 难度标注
check('难度标注取值合法', () => {
  for (const [year, info] of Object.entries(index.difficulty || {})) {
    for (const note of info.all || []) {
      assert(['偏难', '适中', '偏易'].includes(note.level),
        `${year} 年出现非法难度值：${note.level}`);
      assert(typeof note.source_url === 'string' && note.source_url,
        `${year} 年难度条目缺 source_url`);
    }
  }
});

// ------------------------------------------------------------------ 内联包
if (existsSync(join(DATA, 'data.js'))) {
  check('data.js 内联包与分片内容一致', () => {
    const src = readFileSync(join(DATA, 'data.js'), 'utf8');
    // data.js 是 `window.__GAOKAO_DATA__ = {...};`，用一个假的 window 求值
    const bundle = new Function('window', `${src}\nreturn window.__GAOKAO_DATA__;`)({});
    assert(bundle && bundle.index, 'data.js 未注入 __GAOKAO_DATA__.index');
    assert(bundle.index.data_version === index.data_version, 'data_version 不一致');
    for (const year of index.years) {
      const shard = readJson(`admissions-${year}.json`);
      const inlined = bundle.years[String(year)];
      assert(inlined, `内联包缺 ${year} 年`);
      assert(inlined.rows.length === shard.rows.length,
        `${year} 年内联行数 ${inlined.rows.length} != 分片 ${shard.rows.length}`);
    }
  });
}

console.log(`\n通过 ${pass}/${pass + failures.length}`);
process.exit(failures.length ? 1 : 0);
