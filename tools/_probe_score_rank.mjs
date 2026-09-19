// One-off probe: pull the real 2025 Beijing 一分一段表 (score distribution)
// from a published mirror and write it as seed data for the pipeline.
// Writes CSV directly (no stdout piping) to avoid sandbox stdio restrictions.
import { writeFileSync, mkdirSync } from 'node:fs';

const SRC = 'https://gaokao.eol.cn/bei_jing/dongtai/202506/t20250625_2676934.shtml';
const OUT_DIR = 'data/seed';

const res = await fetch(SRC, {
  headers: {
    'User-Agent':
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
  },
});
const html = await res.text();
console.log('status', res.status, 'len', html.length);

const rows = [];
// Every table row in the article: score | count | cumulative
for (const tr of html.matchAll(/<tr[^>]*>([\s\S]*?)<\/tr>/gi)) {
  const cells = [...tr[1].matchAll(/<t[dh][^>]*>([\s\S]*?)<\/t[dh]>/gi)].map((c) =>
    c[1]
      .replace(/<[^>]+>/g, '')
      .replace(/&nbsp;/g, ' ')
      .replace(/\s+/g, '')
      .trim()
  );
  if (cells.length < 3) continue;
  const [rawScore, rawCount, rawCum] = cells;
  // Accept "697" or a range like "698-750"
  const m = /^(\d{3})(?:\s*[-~—]\s*(\d{3}))?$/.exec(rawScore);
  if (!m) continue;
  if (!/^\d+$/.test(rawCount) || !/^\d+$/.test(rawCum)) continue;
  rows.push({
    score_low: Number(m[1]),
    score_high: m[2] ? Number(m[2]) : Number(m[1]),
    segment: Number(rawCount),
    cumulative: Number(rawCum),
  });
}

console.log('parsed rows', rows.length);
if (rows.length < 50) {
  console.error('Too few rows parsed; aborting without writing.');
  process.exit(2);
}

mkdirSync(OUT_DIR, { recursive: true });
const csv = [
  'year,score_low,score_high,segment_count,cumulative_count',
  ...rows.map((r) => `2025,${r.score_low},${r.score_high},${r.segment},${r.cumulative}`),
].join('\n');
writeFileSync(`${OUT_DIR}/score_rank_2025.csv`, csv + '\n', 'utf8');

console.log('first', JSON.stringify(rows[0]), 'last', JSON.stringify(rows[rows.length - 1]));
console.log('total candidates at last row', rows[rows.length - 1].cumulative);
