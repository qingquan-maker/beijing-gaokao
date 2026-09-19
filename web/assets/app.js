/* ==========================================================================
   北京高考志愿数据 —— 前端逻辑（原生 JS，无框架、无构建步骤）
   --------------------------------------------------------------------------
   数据形态（由 pipeline/export_json.py 产出）：
     index.json            元信息：年份、列定义、计数、来源、位次口径说明
     admissions-{年}.json  columns/rows   专业级（招生计划目录）
                           groups        专业组级（考试院公布的投档线）★主视角
     score_rank-{年}.json  一分一段表，用来把位次换算成等效分

   为什么以「专业组」为主视角：
   北京实行院校专业组投档，每个专业组单独划线，志愿也是填到专业组。
   官方公布的投档线本身就是专业组级的，所以列表主体是专业组，专业是展开后的细节。

   若浏览器拦截 fetch（file:// 双击打开），自动回落到 data.js 内联包。
   ========================================================================== */

(function () {
  'use strict';

  const PAGE_SIZE = 60;
  const TIER = { CHONG: 'chong', WEN: 'wen', BAO: 'bao', RISK: 'risk' };
  const TIER_LABEL = { chong: '冲', wen: '稳', bao: '保', risk: '搏', none: '·' };

  //: 分档阈值（分）。delta = 我的分数 − 该专业组投档最低分
  const BAND_BAO = 15;        // 高 15 分以上 → 保
  const BAND_CHONG = -15;     // 低 15 分以内 → 冲
  const HIDE_TOO_HIGH = -15;  // 低于此差值：希望很小，默认隐藏
  const HIDE_TOO_LOW = 45;    // 高于此差值：过于保底，默认隐藏

  const state = {
    year: null,
    score: null,        // 用户输入的总分
    rank: null,         // 用户输入的位次
    effectiveScore: null, // 由分数或位次换算出的等效分
    subject: '',
    batch: '',
    query: '',
    sort: 'score_desc',
    onlyOpen: true,
    expanded: Object.create(null),
    shown: PAGE_SIZE,
  };

  let INDEX = null;
  let PAYLOAD = null;     // 当前年份的原始数据
  let GROUPS = [];        // 归一化后的专业组（含 majors）
  let FACETS = { subjects: [], batches: [] };
  let RANKS = null;       // 当前年份一分一段表（原始行数组）
  let SUMMARIES = Object.create(null);  // school_code -> 院校录取概况（含控制线）
  let CONTROL_LINES = [];               // 去重后的批次控制线
  let filtered = [];

  // ---------------------------------------------------------------- 工具

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function idxOf(columns) {
    const map = {};
    (columns || []).forEach((c, i) => { map[c] = i; });
    return map;
  }

  function intOrNull(v) {
    if (v === null || v === undefined || v === '') return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }

  function fmt(v) {
    const n = intOrNull(v);
    return n === null ? '—' : n.toLocaleString('zh-CN');
  }

  function sortValue(v) {
    const n = intOrNull(v);
    return n === null ? -Infinity : n;
  }

  const $ = (id) => document.getElementById(id);

  // ---------------------------------------------------------------- 数据加载

  function injectInlineBundle() {
    return new Promise((resolve, reject) => {
      if (window.__GAOKAO_DATA__) return resolve(window.__GAOKAO_DATA__);
      const s = document.createElement('script');
      s.src = 'data/data.js';
      s.onload = () => window.__GAOKAO_DATA__
        ? resolve(window.__GAOKAO_DATA__)
        : reject(new Error('data.js 内容为空'));
      s.onerror = () => reject(new Error('无法加载 data/data.js'));
      document.head.appendChild(s);
    });
  }

  function loadJson(path, pick) {
    return fetch(path, { cache: 'no-store' })
      .then((r) => {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .catch(() => injectInlineBundle().then(pick));
  }

  const loadIndex = () => loadJson('data/index.json', (b) => b.index);

  const yearCache = Object.create(null);
  function loadYear(year) {
    if (yearCache[year]) return Promise.resolve(yearCache[year]);
    return loadJson('data/admissions-' + year + '.json', (b) => {
      const p = b.years && b.years[String(year)];
      if (!p) throw new Error('内联包中缺少 ' + year + ' 年数据');
      return p;
    }).then((payload) => {
      yearCache[year] = payload;
      return payload;
    });
  }

  const rankCache = Object.create(null);
  function loadRank(year) {
    if (rankCache[year] !== undefined) return Promise.resolve(rankCache[year]);
    return loadJson('data/score_rank-' + year + '.json',
                    (b) => (b.rank && b.rank[String(year)]) || null)
      .catch(() => null)
      .then((payload) => {
        rankCache[year] = payload && payload.rows
          ? payload.rows.slice().sort((a, b) => b[1] - a[1])   // 按 score_high 降序
          : null;
        return rankCache[year];
      });
  }

  // ---------------------------------------------------------------- 建模

  function buildModel(payload) {
    const groups = [];
    const byKey = Object.create(null);
    const keyOf = (code, batch, gcode) => code + '|' + (batch || '') + '|' + (gcode || '');

    const gc = idxOf(payload.groups ? payload.groups.columns : []);

    function makeGroup(base) {
      const g = Object.assign({
        schoolCode: '', schoolName: '', batch: '', groupCode: '', subjectReq: '',
        minScore: null, rank: null, prevScore: null, plan: 0, majorCount: 0,
        subScores: '', note: '', url: '', majors: [],
      }, base);
      groups.push(g);
      return g;
    }

    ((payload.groups && payload.groups.rows) || []).forEach((r) => {
      const g = makeGroup({
        schoolCode: r[gc.school_code],
        schoolName: r[gc.school_name],
        batch: r[gc.batch] || '',
        groupCode: r[gc.group_code] || '',
        subjectReq: r[gc.subject_req] || '',
        minScore: intOrNull(r[gc.min_score]),
        rank: intOrNull(r[gc.rank_min]),
        prevScore: intOrNull(r[gc.prev_min_score]),
        plan: intOrNull(r[gc.plan_count]) || 0,
        majorCount: intOrNull(r[gc.major_count]) || 0,
        subScores: r[gc.sub_scores] || '',
        note: r[gc.note] || '',
        url: r[gc.source_url] || '',
      });
      byKey[keyOf(g.schoolCode, g.batch, g.groupCode)] = g;
    });

    const ac = idxOf(payload.columns);
    (payload.rows || []).forEach((r) => {
      const code = r[ac.school_code];
      const batch = r[ac.batch] || '';
      const gcode = r[ac.group_code] || '';
      const key = keyOf(code, batch, gcode);
      let g = byKey[key];
      if (!g) {
        // 只有招生计划、没有投档线的专业组（如提前批部分段），也要能看到
        g = makeGroup({
          schoolCode: code,
          schoolName: r[ac.school_name],
          batch: batch,
          groupCode: gcode,
          subjectReq: r[ac.subject_req] || r[ac.group_label] || '',
        });
        byKey[key] = g;
      }
      if (!g.schoolName) g.schoolName = r[ac.school_name];
      if (!g.subjectReq) g.subjectReq = r[ac.subject_req] || r[ac.group_label] || '';
      g.majors.push({
        code: r[ac.major_code] || '',
        name: r[ac.major_name] || '',
        plan: intOrNull(r[ac.plan_count]),
        duration: r[ac.duration] || '',
        tuition: r[ac.tuition] || '',
        minScore: intOrNull(r[ac.min_score]),
      });
    });

    groups.forEach((g) => {
      if (!g.majorCount) g.majorCount = g.majors.length;
      if (!g.plan) g.plan = g.majors.reduce((a, m) => a + (m.plan || 0), 0);
    });
    return groups;
  }

  /** 院校录取概况：院校/科类级，含当年批次控制线（来自高校招生网）。 */
  function buildSummaries(payload) {
    SUMMARIES = Object.create(null);
    CONTROL_LINES = [];
    const sc = idxOf((payload.summaries && payload.summaries.columns) || []);
    const seenControl = new Set();
    ((payload.summaries && payload.summaries.rows) || []).forEach((r) => {
      const code = r[sc.school_code];
      const item = {
        province: r[sc.province] || '',
        subjectType: r[sc.subject_type] || '',
        batchType: r[sc.batch_type] || '',
        min: intOrNull(r[sc.min_score]),
        avg: intOrNull(r[sc.avg_score]),
        control: intOrNull(r[sc.control_line]),
        url: r[sc.source_url] || '',
      };
      (SUMMARIES[code] || (SUMMARIES[code] = [])).push(item);
      if (item.control !== null && item.province.indexOf('北京') >= 0) {
        const key = item.subjectType + '|' + item.control;
        if (!seenControl.has(key)) {
          seenControl.add(key);
          CONTROL_LINES.push(item);
        }
      }
    });
  }

  function summaryLine(group) {
    const list = SUMMARIES[group.schoolCode];
    if (!list || !list.length) return '';
    const bj = list.filter((s) => s.province.indexOf('北京') >= 0);
    const pick = bj[0] || list[0];
    const bits = [];
    if (pick.min !== null) bits.push('最低 ' + pick.min);
    if (pick.avg !== null) bits.push('平均 ' + pick.avg);
    const where = [pick.province, pick.subjectType, pick.batchType].filter(Boolean).join(' · ');
    return '<div class="summary-line">' +
      '<span class="sl-tag">官方录取概况</span>' +
      esc(where) + (bits.length ? ' — ' + esc(bits.join(' / ')) : '') +
      '<span class="sl-note">院校级数据，不等于专业组投档线</span>' +
      '</div>';
  }

  function buildFacets(groups) {
    const subjects = new Set();
    const batches = new Set();
    groups.forEach((g) => {
      if (g.subjectReq) subjects.add(g.subjectReq);
      if (g.batch) batches.add(g.batch);
    });
    const collator = new Intl.Collator('zh');
    FACETS = {
      subjects: [...subjects].sort(collator.compare),
      batches: [...batches].sort(collator.compare),
    };
  }

  // ---------------------------------------------------------------- 位次换算

  /** 位次 → 等效分：在当年一分一段表里找累计人数刚好覆盖该位次的分数段。 */
  function scoreFromRank(rank) {
    if (!RANKS || !rank) return null;
    for (let i = 0; i < RANKS.length; i += 1) {
      if (RANKS[i][3] >= rank) return RANKS[i][1];
    }
    return null;
  }

  // ---------------------------------------------------------------- 分档

  function tierOf(group) {
    if (state.effectiveScore === null || group.minScore === null) return '';
    const delta = state.effectiveScore - group.minScore;
    if (delta >= BAND_BAO) return TIER.BAO;
    if (delta >= 0) return TIER.WEN;
    return TIER.CHONG;
  }

  function inOpenRange(group) {
    if (state.effectiveScore === null || group.minScore === null) return true;
    const delta = state.effectiveScore - group.minScore;
    return delta >= HIDE_TOO_HIGH && delta <= HIDE_TOO_LOW;
  }

  function matchesQuery(group) {
    const q = state.query.trim().toLowerCase();
    if (!q) return true;
    if (group.schoolName.toLowerCase().includes(q)) return true;
    if (String(group.schoolCode).toLowerCase().startsWith(q)) return true;
    return group.majors.some((m) => m.name.toLowerCase().includes(q));
  }

  // ---------------------------------------------------------------- 筛选 + 排序

  function compute() {
    filtered = GROUPS.filter((g) => {
      if (state.subject && g.subjectReq !== state.subject) return false;
      if (state.batch && g.batch !== state.batch) return false;
      if (state.onlyOpen && state.effectiveScore !== null && !inOpenRange(g)) return false;
      return matchesQuery(g);
    });

    const byScoreDesc = (a, b) => {
      const d = sortValue(b.minScore) - sortValue(a.minScore);
      return d !== 0 ? d : a.schoolName.localeCompare(b.schoolName, 'zh');
    };
    const collator = new Intl.Collator('zh');

    const comparators = {
      score_desc: byScoreDesc,
      score_asc: (a, b) => sortValue(a.minScore) - sortValue(b.minScore),
      rank_asc: (a, b) => {
        // 位次越小越好；没有位次的排最后
        const av = a.rank === null ? Infinity : a.rank;
        const bv = b.rank === null ? Infinity : b.rank;
        return av - bv;
      },
      plan_desc: (a, b) => (b.plan || 0) - (a.plan || 0),
      name: (a, b) => collator.compare(a.schoolName, b.schoolName)
        || collator.compare(a.groupCode, b.groupCode),
    };
    filtered.sort(comparators[state.sort] || byScoreDesc);
  }

  // ---------------------------------------------------------------- 渲染

  function metricsHtml(g) {
    const parts = [];
    parts.push(card('投档最低分', fmt(g.minScore), g.minScore === null));
    parts.push(card('位次', fmt(g.rank), g.rank === null));
    parts.push(card('计划', g.plan ? fmt(g.plan) : '—', !g.plan));
    parts.push(card('专业', g.majorCount ? fmt(g.majorCount) : '—', !g.majorCount));

    if (g.prevScore !== null && g.minScore !== null) {
      const d = g.minScore - g.prevScore;
      const cls = d > 0 ? 'delta-up' : (d < 0 ? 'delta-down' : '');
      const text = (d > 0 ? '+' : '') + d;
      parts.push(
        '<div class="metric"><dt>较去年</dt>' +
        '<dd class="' + cls + '">' + esc(text) + '</dd></div>'
      );
    }
    return parts.join('');
  }

  function card(label, value, muted) {
    return '<div class="metric"><dt>' + esc(label) + '</dt>' +
      '<dd class="' + (muted ? 'muted' : '') + '">' + esc(value) + '</dd></div>';
  }

  function detailHtml(g) {
    if (!g.majors.length) {
      return '<div class="detail"><div class="none">' +
        '该年份未采集到专业目录明细（当前仅覆盖 2026 年招生计划）。</div></div>';
    }
    const rows = g.majors.slice().sort((a, b) =>
      sortValue(b.minScore) - sortValue(a.minScore)
      || a.name.localeCompare(b.name, 'zh'));

    const body = rows.map((m) => {
      const tuition = String(m.tuition || '').replace(/\.0+$/, '');
      const meta = [
        m.duration ? m.duration + '年' : '',
        tuition ? tuition + '元/年' : '',
      ].filter(Boolean).join(' · ');
      return '<tr>' +
        '<td class="name">' +
          (m.code ? '<span class="mcode">' + esc(m.code) + '</span>' : '') +
          esc(m.name) +
        '</td>' +
        '<td class="num">' + (m.plan === null ? '—' : esc(fmt(m.plan))) + '</td>' +
        '<td class="num">' + (m.minScore === null ? '—' : esc(fmt(m.minScore))) + '</td>' +
        '<td class="meta">' + esc(meta) + '</td>' +
        '</tr>';
    }).join('');

    return '<div class="detail">' +
      '<table>' +
      '<caption>该专业组下的招生专业（专业级分数由高校另行公布，未采集时显示 —）</caption>' +
      '<thead><tr><th>专业</th><th class="num">计划</th>' +
      '<th class="num">最低分</th><th>学制 / 学费</th></tr></thead>' +
      '<tbody>' + body + '</tbody></table></div>';
  }

  function cardHtml(g) {
    const tier = tierOf(g);
    const key = g.schoolCode + '|' + g.batch + '|' + g.groupCode;
    const open = !!state.expanded[key];
    const chip = tier ? TIER_LABEL[tier] : TIER_LABEL.none;
    const req = g.subjectReq || '不限选考科目';
    const sub = [req, g.batch].filter(Boolean).join(' · ');

    return '<article class="card" data-tier="' + esc(tier) + '">' +
      '<div class="card-head">' +
        '<div class="card-title">' +
          '<span class="tier-chip" title="冲/稳/保参考">' + esc(chip) + '</span>' +
          '<h2>' + esc(g.schoolName) +
            (g.groupCode ? '<span class="gc">' + esc(g.groupCode) + '组</span>' : '') +
          '</h2>' +
          '<span class="card-sub">' + esc(sub) + '</span>' +
        '</div>' +
        '<button class="expand" type="button" data-key="' + esc(key) + '" ' +
          'aria-expanded="' + (open ? 'true' : 'false') + '">' +
          (open ? '收起专业' : '展开专业') + '</button>' +
      '</div>' +
      '<dl class="metrics">' + metricsHtml(g) + '</dl>' +
      summaryLine(g) +
      (open ? detailHtml(g) : '') +
      '</article>';
  }

  function render() {
    compute();

    const list = $('list');
    const empty = $('empty');
    const more = $('more');
    const total = filtered.length;

    let html = '';
    let hiddenByCap = 0;

    if (state.effectiveScore !== null) {
      // 有参考分时按「冲 / 稳 / 保」分区：考生最需要的是先看清每一档各有什么。
      const buckets = { chong: [], wen: [], bao: [], other: [] };
      filtered.forEach((g) => {
        const t = tierOf(g);
        buckets[t === TIER.CHONG || t === TIER.WEN || t === TIER.BAO ? t : 'other'].push(g);
      });
      const sections = [
        ['chong', '冲', '投档线略高于你的参考分，可以冲一冲'],
        ['wen', '稳', '已经够到投档线，重点考虑'],
        ['bao', '保', '高出投档线较多，用来兜底'],
        ['other', '暂无投档线', '只有招生计划、没有官方投档线数据'],
      ];
      sections.forEach(([key, label, hint]) => {
        const items = buckets[key];
        if (!items.length) return;
        const slice = items.slice(0, state.shown);
        hiddenByCap += items.length - slice.length;
        html += '<div class="section-head" data-tier="' + key + '">' +
          '<span class="section-chip">' + esc(label) + '</span>' +
          '<span class="section-title">' + esc(hint) + '</span>' +
          '<span class="section-count">' + items.length + ' 个</span>' +
          '</div>' + slice.map(cardHtml).join('');
      });
    } else {
      const slice = filtered.slice(0, state.shown);
      hiddenByCap = total - slice.length;
      html = slice.map(cardHtml).join('');
    }

    list.innerHTML = html;
    empty.hidden = total > 0;
    if (total === 0) {
      empty.innerHTML = '<strong>没有匹配的院校专业组</strong>' +
        '试试放宽条件：清除分数、切换批次，或取消「只看可报范围」。';
    }
    more.hidden = hiddenByCap <= 0;

    const withScore = filtered.filter((g) => g.minScore !== null).length;
    const schools = new Set(filtered.map((g) => g.schoolCode)).size;
    let text = '共 <b>' + total + '</b> 个院校专业组 · <b>' + schools + '</b> 所院校';
    if (withScore) text += ' · 其中 <b>' + withScore + '</b> 个有官方投档线';
    $('count').innerHTML = text;
  }

  function renderLegend() {
    const box = $('tier-legend');
    const control = CONTROL_LINES.length
      ? '<div class="control-line">' + esc(state.year) + ' 年批次控制线（来自高校招生网）：' +
        CONTROL_LINES.map((c) =>
          esc(c.subjectType || '普通类') + ' <b>' + c.control + '</b> 分').join(' · ') +
        '<span class="sl-note">控制线是投档/录取的最低资格线，不是院校录取线</span></div>'
      : '';
    if (state.effectiveScore === null) {
      const hints = [];
      hints.push('填写分数或位次后，卡片会按 <b class="k-chong">冲</b>（差 15 分以内）' +
                 ' / <b class="k-wen">稳</b>（已达线） / <b class="k-bao">保</b>（高出 15 分以上）分档。');
      if (state.rank && state.effectiveScore === null) {
        hints.push('当前年份缺少一分一段表，无法把位次换算成等效分。');
      }
      box.innerHTML = control + hints.join(' ');
      return;
    }
    const bits = [];
    bits.push('参考分：<b>' + state.effectiveScore + '</b> 分');
    if (state.rank) {
      bits.push('（由位次 ' + state.rank + ' 换算的等效分）');
    }
    bits.push('· 分档规则：投档线比你低 ' + BAND_BAO + ' 分以上为保，' +
              '比你低 0～' + (BAND_BAO - 1) + ' 分为稳，' +
              '比你高 ' + Math.abs(BAND_CHONG) + ' 分以内为冲。');
    bits.push('投档线是「最后一名被投档考生」的分数，达到线不等于一定录取。');
    box.innerHTML = control + bits.join(' ');
  }

  function fillSelect(id, values, placeholder) {
    const sel = $(id);
    sel.innerHTML = '<option value="">' + esc(placeholder) + '</option>' +
      values.map((v) => '<option value="' + esc(v) + '">' + esc(v) + '</option>').join('');
  }

  function refreshFacets() {
    fillSelect('f-subject', FACETS.subjects, '全部选科');
    fillSelect('f-batch', FACETS.batches, '全部批次');
    $('f-subject').value = state.subject;
    $('f-batch').value = state.batch;
  }

  function renderFooter() {
    $('sources').innerHTML = '数据来源：' + (INDEX.sources || []).map((s) =>
      '<a href="' + esc(s.url) + '" target="_blank" rel="noopener">' + esc(s.name) + '</a>'
    ).join(' · ');
    $('rank-note').textContent = INDEX.rank_note || '';

    const c = INDEX.counts || {};
    $('meta').textContent =
      '更新于 ' + (INDEX.generated_at || '—') +
      ' · 投档线 ' + (c.group_scores || 0) +
      ' 条 · 招生专业 ' + (c.programs || 0) +
      ' 个 · 院校 ' + (c.schools || 0) + ' 所';
  }

  // ---------------------------------------------------------------- URL 状态

  function syncUrl() {
    const p = new URLSearchParams();
    if (state.year) p.set('y', state.year);
    if (state.score !== null) p.set('s', state.score);
    if (state.rank !== null) p.set('r', state.rank);
    if (state.subject) p.set('sub', state.subject);
    if (state.batch) p.set('b', state.batch);
    if (state.query) p.set('q', state.query);
    if (state.sort !== 'score_desc') p.set('sort', state.sort);
    const qs = p.toString();
    history.replaceState(null, '', qs ? '?' + qs : location.pathname);
  }

  function readUrl() {
    const p = new URLSearchParams(location.search);
    const out = {};
    if (p.get('y')) out.year = Number(p.get('y'));
    if (p.get('s')) out.score = Number(p.get('s'));
    if (p.get('r')) out.rank = Number(p.get('r'));
    if (p.get('sub')) out.subject = p.get('sub');
    if (p.get('b')) out.batch = p.get('b');
    if (p.get('q')) out.query = p.get('q');
    if (p.get('sort')) out.sort = p.get('sort');
    return out;
  }

  // ---------------------------------------------------------------- 事件

  function applyInputs() {
    state.score = intOrNull($('f-score').value);
    state.rank = intOrNull($('f-rank').value);
    state.subject = $('f-subject').value;
    state.batch = $('f-batch').value;
    state.query = $('f-query').value;
    state.sort = $('f-sort').value;
    state.onlyOpen = $('f-only-open').checked;
    state.shown = PAGE_SIZE;

    // 分数优先；只填位次时用一分一段表换算等效分
    state.effectiveScore = state.score !== null
      ? state.score
      : scoreFromRank(state.rank);
  }

  function refresh() {
    applyInputs();
    renderLegend();
    render();
    syncUrl();
  }

  function bindEvents() {
    $('query-form').addEventListener('submit', (e) => {
      e.preventDefault();
      refresh();
    });

    // 分数与位次互斥：填了一个就清掉另一个，避免"以哪个为准"的困惑
    $('f-score').addEventListener('input', () => {
      if ($('f-score').value) $('f-rank').value = '';
    });
    $('f-rank').addEventListener('input', () => {
      if ($('f-rank').value) $('f-score').value = '';
    });

    ['f-subject', 'f-batch', 'f-sort'].forEach((id) => {
      $(id).addEventListener('change', refresh);
    });
    $('f-only-open').addEventListener('change', refresh);

    let timer = null;
    $('f-query').addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(refresh, 180);
    });

    $('f-reset').addEventListener('click', () => {
      $('f-score').value = '';
      $('f-rank').value = '';
      $('f-query').value = '';
      $('f-subject').value = '';
      $('f-batch').value = '';
      $('f-sort').value = 'score_desc';
      $('f-only-open').checked = true;
      state.expanded = Object.create(null);
      refresh();
    });

    // 事件委托：展开/收起专业
    $('list').addEventListener('click', (ev) => {
      const btn = ev.target.closest('.expand');
      if (!btn) return;
      const key = btn.getAttribute('data-key');
      state.expanded[key] = !state.expanded[key];
      render();
    });

    $('more').addEventListener('click', () => {
      state.shown += PAGE_SIZE;
      render();
    });

    $('f-year').addEventListener('change', (e) => {
      state.year = Number(e.target.value);
      state.expanded = Object.create(null);
      loadYearInto(state.year).then(refresh);
    });
  }

  // ---------------------------------------------------------------- 启动

  function loadYearInto(year) {
    return Promise.all([loadYear(year), loadRank(year)]).then(([payload, ranks]) => {
      PAYLOAD = payload;
      RANKS = ranks;
      GROUPS = buildModel(payload);
      buildSummaries(payload);
      buildFacets(GROUPS);
      refreshFacets();
      state.shown = PAGE_SIZE;
    });
  }

  function init() {
    const url = readUrl();
    loadIndex().then((index) => {
      INDEX = index;
      renderFooter();

      const years = (index.years && index.years.length) ? index.years : [];
      const startYear = url.year && years.includes(url.year)
        ? url.year
        : (index.latest_year || years[0]);

      $('f-year').innerHTML = years.map((y) =>
        '<option value="' + y + '"' + (y === startYear ? ' selected' : '') + '>' +
        y + ' 年</option>').join('');

      $('f-score').value = url.score !== undefined ? url.score : '';
      $('f-rank').value = url.rank !== undefined ? url.rank : '';
      $('f-query').value = url.query || '';
      $('f-sort').value = url.sort || 'score_desc';
      state.subject = url.subject || '';
      state.batch = url.batch || '';

      state.year = startYear;
      return loadYearInto(startYear);
    }).then(() => {
      // 批次/选科的下拉值是数据相关，回填要等到模型建好之后
      if (state.subject && FACETS.subjects.includes(state.subject)) {
        $('f-subject').value = state.subject;
      }
      if (state.batch && FACETS.batches.includes(state.batch)) {
        $('f-batch').value = state.batch;
      }
      bindEvents();
      refresh();
    }).catch((err) => {
      $('count').textContent = '数据加载失败：' + err.message;
      const empty = $('empty');
      empty.hidden = false;
      empty.innerHTML = '<strong>未能加载数据</strong>' +
        '若你是双击打开本页面，浏览器会拦截本地读取；' +
        '请改用 HTTP 服务访问（python scripts/serve.py），' +
        '并确认已运行导出脚本生成 web/data/ 下的 JSON。';
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
