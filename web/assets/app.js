/* ==========================================================================
   北京高考志愿录取数据查询 —— 前端逻辑（原生 JS，无框架、无构建步骤）
   --------------------------------------------------------------------------
   数据形态：
     index.json            元信息：年份列表、列定义、难度评价、数据版本
     admissions-{年}.json  {year, columns:[...], rows:[[...], ...]}
     用「列定义 + 行数组」而不是对象数组，可省 30%~40% 体积；
     下面 toObjects() 负责还原。

   若浏览器拦截 fetch（用 file:// 双击打开 index.html 时），
   自动回落到内联包 data/data.js（window.__GAOKAO_DATA__）。
   ========================================================================== */

(function () {
  'use strict';

  var LATEST_FALLBACK = new Date().getFullYear();

  var state = {
    year: null,
    schoolQuery: '',
    groupCode: '',
    subjectReq: '',
    batch: '',
    onlyScored: false,
    expandedSchools: Object.create(null),  // school_code -> true
    expandedGroups: Object.create(null)    // school_code|groupKey -> true
  };

  var INDEX = null;      // index.json
  var YEAR_CACHE = Object.create(null);
  var COLS = null;       // {列名: 下标}
  var ROWS = [];         // 当年原始行数组
  var TREE = [];         // 分组后的学校树

  // ---------------------------------------------------------------- 工具

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function num(v, digits) {
    if (v === null || v === undefined || v === '') return null;
    var n = Number(v);
    if (!isFinite(n)) return null;
    return digits ? n.toFixed(digits) : String(n);
  }

  function thousands(v) {
    var s = num(v);
    if (s === null) return null;
    return s.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function cell(value, cls) {
    var text = (value === null || value === undefined || value === '') ? '—' : value;
    return '<td class="' + (cls || '') + '">' + esc(text) + '</td>';
  }

  function diffBadge(level) {
    if (!level) return '<span class="badge none">无</span>';
    var cls = level === '偏难' ? 'badge hard' : (level === '偏易' ? 'badge easy' : 'badge');
    return '<span class="' + cls + '">' + esc(level) + '</span>';
  }

  // ---------------------------------------------------------------- 数据加载

  function injectInlineBundle() {
    return new Promise(function (resolve, reject) {
      if (window.__GAOKAO_DATA__) return resolve(window.__GAOKAO_DATA__);
      var s = document.createElement('script');
      s.src = 'data/data.js';
      s.onload = function () {
        window.__GAOKAO_DATA__
          ? resolve(window.__GAOKAO_DATA__)
          : reject(new Error('data.js 内容为空'));
      };
      s.onerror = function () { reject(new Error('无法加载 data/data.js')); };
      document.head.appendChild(s);
    });
  }

  function loadIndex() {
    return fetch('data/index.json', { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .catch(function () {
        // file:// 打开时 fetch 会被拦，改用内联包
        return injectInlineBundle().then(function (bundle) { return bundle.index; });
      });
  }

  function loadYear(year) {
    if (YEAR_CACHE[year]) return Promise.resolve(YEAR_CACHE[year]);
    return fetch('data/admissions-' + year + '.json', { cache: 'no-store' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .catch(function () {
        return injectInlineBundle().then(function (bundle) {
          var payload = bundle.years && bundle.years[String(year)];
          if (!payload) throw new Error('内联包中缺少 ' + year + ' 年数据');
          return payload;
        });
      })
      .then(function (payload) {
        YEAR_CACHE[year] = payload;
        return payload;
      });
  }

  /** 列定义 + 行数组 → 对象数组 */
  function toObjects(payload) {
    var cols = payload.columns || [];
    var idx = {};
    cols.forEach(function (c, i) { idx[c] = i; });
    return { cols: cols, idx: idx, rows: payload.rows || [] };
  }

  // ---------------------------------------------------------------- 建树

  function pick(row, idx, name) {
    var i = idx[name];
    return i === undefined ? null : row[i];
  }

  function buildTree(rows, idx) {
    var schools = Object.create(null);
    var order = [];

    rows.forEach(function (row) {
      var code = pick(row, idx, 'school_code');
      var key = code || pick(row, idx, 'school_name');
      var school = schools[key];
      if (!school) {
        school = schools[key] = {
          key: key,
          code: code,
          name: pick(row, idx, 'school_name') || '',
          province: pick(row, idx, 'province') || '',
          sourceUrl: '',
          groups: Object.create(null),
          groupOrder: [],
          majors: 0,
          plan: 0,
          minScores: [],
          maxScores: [],
          avgScores: [],
          rankMin: null
        };
        order.push(school);
      }
      if (!school.sourceUrl) school.sourceUrl = pick(row, idx, 'source_url') || '';

      // 专业组维度：批次 + 组代码 + 选科要求 共同决定一个组
      var batch = pick(row, idx, 'batch') || '';
      var gcode = pick(row, idx, 'group_code') || '';
      var greq = pick(row, idx, 'group_label') || pick(row, idx, 'subject_req') || '';
      var gkey = batch + '\u0001' + gcode + '\u0001' + greq;

      var group = school.groups[gkey];
      if (!group) {
        group = school.groups[gkey] = {
          key: gkey, batch: batch, code: gcode, req: greq,
          majors: [], minScores: [], maxScores: [], avgScores: [],
          plan: 0, rankMin: null
        };
        school.groupOrder.push(group);
      }

      var min = pick(row, idx, 'min_score');
      var max = pick(row, idx, 'max_score');
      var avg = pick(row, idx, 'avg_score');
      var rank = pick(row, idx, 'rank_min');
      var plan = pick(row, idx, 'plan_count');

      group.majors.push(row);
      if (min !== null && min !== '') group.minScores.push(Number(min));
      if (max !== null && max !== '') group.maxScores.push(Number(max));
      if (avg !== null && avg !== '') group.avgScores.push(Number(avg));
      if (plan !== null && plan !== '') group.plan += Number(plan) || 0;
      if (rank !== null && rank !== '' &&
          (group.rankMin === null || Number(rank) < group.rankMin)) {
        group.rankMin = Number(rank);
      }

      school.majors += 1;
      school.plan += Number(plan) || 0;
      if (min !== null && min !== '') school.minScores.push(Number(min));
      if (max !== null && max !== '') school.maxScores.push(Number(max));
      if (avg !== null && avg !== '') school.avgScores.push(Number(avg));
      if (rank !== null && rank !== '' &&
          (school.rankMin === null || Number(rank) < school.rankMin)) {
        school.rankMin = Number(rank);
      }
    });

    order.forEach(function (s) {
      s.groupOrder.sort(function (a, b) {
        return String(a.code + a.batch).localeCompare(String(b.code + b.batch), 'zh');
      });
    });
    order.sort(function (a, b) { return a.name.localeCompare(b.name, 'zh'); });
    return order;
  }

  function agg(list) {
    if (!list.length) return null;
    return list.reduce(function (a, b) { return a + b; }, 0) / list.length;
  }

  // ---------------------------------------------------------------- 筛选

  function schoolMatch(school, q) {
    if (!q) return 'all';
    var n = q.toLowerCase().trim();
    if (school.name.toLowerCase().indexOf(n) >= 0) return 'all';
    if (school.code && String(school.code).toLowerCase().indexOf(n) === 0) return 'all';
    // 也支持按专业名搜索：命中则只显示命中的专业
    var hit = false;
    school.groupOrder.forEach(function (g) {
      g.majors.forEach(function (m) {
        var major = String(pick(m, COLS, 'major_name') || '').toLowerCase();
        if (major.indexOf(n) >= 0) hit = true;
      });
    });
    return hit ? 'partial' : false;
  }

  function majorVisible(row, partial, q) {
    if (state.onlyScored && (pick(row, COLS, 'min_score') === null ||
        pick(row, COLS, 'min_score') === '')) return false;
    if (state.groupCode && String(pick(row, COLS, 'group_code') || '') !== state.groupCode) return false;
    if (state.batch && String(pick(row, COLS, 'batch') || '') !== state.batch) return false;
    if (state.subjectReq) {
      var req = String(pick(row, COLS, 'group_label') || pick(row, COLS, 'subject_req') || '');
      if (req !== state.subjectReq) return false;
    }
    if (partial) {
      var major = String(pick(row, COLS, 'major_name') || '').toLowerCase();
      if (major.indexOf(q.toLowerCase().trim()) < 0) return false;
    }
    return true;
  }

  // ---------------------------------------------------------------- 渲染

  function render() {
    var body = document.getElementById('grid-body');
    var q = state.schoolQuery;
    var html = [];
    var shownSchools = 0, shownMajors = 0, shownRows = 0;

    TREE.forEach(function (school) {
      var mode = schoolMatch(school, q);

      // 先按筛选条件过滤出可见专业，再决定该学校是否显示
      var visibleGroups = [];
      school.groupOrder.forEach(function (group) {
        var majors = group.majors.filter(function (m) {
          return majorVisible(m, mode === 'partial', q);
        });
        if (majors.length) visibleGroups.push({ group: group, majors: majors });
      });

      if (!visibleGroups.length) return;
      // 'partial' 模式下即使学校名不匹配，只要专业命中就显示
      if (mode === false) return;

      shownSchools += 1;

      var sMin = [], sMax = [], sAvg = [], sPlan = 0, sRank = null, sMajors = 0;
      visibleGroups.forEach(function (item) {
        item.majors.forEach(function (m) {
          var mn = pick(m, COLS, 'min_score'), mx = pick(m, COLS, 'max_score');
          var av = pick(m, COLS, 'avg_score'), pl = pick(m, COLS, 'plan_count');
          var rk = pick(m, COLS, 'rank_min');
          if (mn !== null && mn !== '') sMin.push(Number(mn));
          if (mx !== null && mx !== '') sMax.push(Number(mx));
          if (av !== null && av !== '') sAvg.push(Number(av));
          if (pl !== null && pl !== '') sPlan += Number(pl) || 0;
          if (rk !== null && rk !== '' && (sRank === null || Number(rk) < sRank)) sRank = Number(rk);
          sMajors += 1;
        });
      });

      var schoolKey = school.key;
      var expanded = !!state.expandedSchools[schoolKey] || mode === 'partial';
      var level = visibleGroups[0].majors.length
        ? pick(visibleGroups[0].majors[0], COLS, 'difficulty') : null;

      // ---- 一级：学校 ----
      html.push(
        '<tr class="lv-school" data-school="' + esc(schoolKey) + '">' +
        cell(state.year, 'c-year') +
        '<td class="c-school">' +
          '<span class="toggle">' + (expanded ? '▾' : '▸') + '</span>' +
          esc(school.name) +
          (school.code ? ' <span class="code">' + esc(school.code) + '</span>' : '') +
          ' <span class="plan-total">(' + visibleGroups.length + ' 组 / ' + sMajors + ' 专业)</span>' +
          (school.sourceUrl
            ? ' <a class="src-link" href="' + esc(school.sourceUrl) + '" target="_blank" rel="noopener">源</a>'
            : '') +
        '</td>' +
        cell('', 'c-group') +
        cell('', 'c-major') +
        cell(sMin.length ? Math.min.apply(null, sMin) : null, 'c-num') +
        cell(sAvg.length ? agg(sAvg).toFixed(1) : null, 'c-num') +
        cell(sMax.length ? Math.max.apply(null, sMax) : null, 'c-num') +
        cell(sRank === null ? null : thousands(sRank), 'c-rank') +
        cell(sPlan || null, 'c-plan') +
        '<td class="c-diff">' + diffBadge(level) + '</td>' +
        '</tr>'
      );

      if (!expanded) return;

      // ---- 二级：专业组 ----
      visibleGroups.forEach(function (item) {
        var group = item.group;
        var gkey = schoolKey + '|' + group.key;
        var gExpanded = !!state.expandedGroups[gkey] || mode === 'partial';

        var gMin = [], gMax = [], gAvg = [], gPlan = 0, gRank = null;
        item.majors.forEach(function (m) {
          var mn = pick(m, COLS, 'min_score'), mx = pick(m, COLS, 'max_score');
          var av = pick(m, COLS, 'avg_score'), pl = pick(m, COLS, 'plan_count');
          var rk = pick(m, COLS, 'rank_min');
          if (mn !== null && mn !== '') gMin.push(Number(mn));
          if (mx !== null && mx !== '') gMax.push(Number(mx));
          if (av !== null && av !== '') gAvg.push(Number(av));
          if (pl !== null && pl !== '') gPlan += Number(pl) || 0;
          if (rk !== null && rk !== '' && (gRank === null || Number(rk) < gRank)) gRank = Number(rk);
        });

        var gLevel = pick(item.majors[0], COLS, 'difficulty');

        html.push(
          '<tr class="lv-group" data-school="' + esc(schoolKey) + '" data-group="' + esc(gkey) + '">' +
          cell('', 'c-year') +
          cell('', 'c-school') +
          '<td class="c-group"><span class="indent">' +
            '<span class="toggle">' + (gExpanded ? '▾' : '▸') + '</span>' +
            (group.code ? '<span class="group-code">' + esc(group.code) + '组</span>' : '') +
            '<span class="sub-req">' + esc(group.req || group.batch || '') + '</span>' +
          '</span></td>' +
          cell('', 'c-major') +
          cell(gMin.length ? Math.min.apply(null, gMin) : null, 'c-num') +
          cell(gAvg.length ? agg(gAvg).toFixed(1) : null, 'c-num') +
          cell(gMax.length ? Math.max.apply(null, gMax) : null, 'c-num') +
          cell(gRank === null ? null : thousands(gRank), 'c-rank') +
          cell(gPlan || null, 'c-plan') +
          '<td class="c-diff">' + diffBadge(gLevel) + '</td>' +
          '</tr>'
        );

        if (!gExpanded) return;

        // ---- 三级：专业 ----
        item.majors.forEach(function (m) {
          shownMajors += 1;
          shownRows += 1;
          var mn = pick(m, COLS, 'min_score'), av = pick(m, COLS, 'avg_score');
          var mx = pick(m, COLS, 'max_score'), rk = pick(m, COLS, 'rank_min');
          var pl = pick(m, COLS, 'plan_count');
          var mc = pick(m, COLS, 'major_code');
          var dur = pick(m, COLS, 'duration');
          var fee = pick(m, COLS, 'tuition');
          html.push(
            '<tr class="lv-major">' +
            cell('', 'c-year') +
            cell('', 'c-school') +
            cell('', 'c-group') +
            '<td class="c-major"><span class="indent">' +
              (mc ? '<span class="code">' + esc(mc) + '</span>' : '') +
              esc(pick(m, COLS, 'major_name') || '') +
              (dur ? ' <span class="sub-req">' + esc(dur) + '年</span>' : '') +
              (fee ? ' <span class="sub-req">' + esc(fee) + '元</span>' : '') +
            '</span></td>' +
            cell(num(mn), 'c-num') +
            cell(av === null || av === '' ? null : num(av, 1), 'c-num') +
            cell(num(mx), 'c-num') +
            cell(thousands(rk), 'c-rank') +
            cell(num(pl), 'c-plan') +
            '<td class="c-diff">' + diffBadge(pick(m, COLS, 'difficulty')) + '</td>' +
            '</tr>'
          );
        });
      });
    });

    body.innerHTML = html.join('');
    document.getElementById('empty').hidden = html.length > 0;
    document.getElementById('count').textContent =
      shownSchools + ' 所学校 / ' + shownMajors + ' 个专业' +
      (shownMajors === 0 ? '' : '（展开后可见 ' + shownRows + ' 行）');
  }

  // ---------------------------------------------------------------- 难度条

  function renderDifficulty() {
    var box = document.getElementById('difficulty');
    var info = INDEX.difficulty && INDEX.difficulty[String(state.year)];
    var primary = info && info.primary;

    var parts = [];

    // 示例数据必须显著提示，避免被误当成真实录取分
    if ((INDEX.demo_years || []).indexOf(Number(state.year)) >= 0) {
      parts.push(
        '<span class="warn">⚠ 该年份包含<strong>示例数据</strong>（score_source=demo），' +
        '仅用于演示页面功能，不可作为志愿填报依据。真实数据请通过考试院/高校招生网采集。</span>'
      );
    }

    if (primary) {
      var cls = primary.level === '偏难' ? 'badge hard'
              : (primary.level === '偏易' ? 'badge easy' : 'badge');
      var link = primary.source_url
        ? ' <a href="' + esc(primary.source_url) + '" target="_blank" rel="noopener">来源</a>' : '';
      var isDemo = primary.source_type === 'demo';
      parts.push(
        '<span class="lbl">' + state.year + ' 年试卷难度：</span>' +
        '<span class="' + cls + '">' + esc(primary.level) + '</span>' +
        '<span class="' + (isDemo ? 'badge none' : 'badge none') + '">' +
          (isDemo ? '示例' : '有据') + '</span>' +
        (primary.subject && primary.subject !== '全科'
          ? '<span class="lbl">（' + esc(primary.subject) + '）</span>' : '') +
        '<span>' + esc(primary.summary || '') + '</span>' +
        (primary.evidence ? '<span class="sub-req">依据：' + esc(primary.evidence) + '</span>' : '') +
        link
      );
    } else {
      parts.push(
        '<span class="lbl">' + state.year + ' 年试卷难度：</span>' +
        '<span class="badge none">暂无评价</span>' +
        '<span class="sub-req">可在 data/seed/demo/difficulty.json 增加条目，' +
        '或跑公众号抽取（scripts/run_all.py --with-wechat）后用 ' +
        'add_manual_difficulty() 人工录入。</span>'
      );
    }

    box.innerHTML = parts.join(' ');
    box.hidden = false;
  }

  // ---------------------------------------------------------------- 筛选控件

  function fillSelect(id, values, placeholder) {
    var sel = document.getElementById(id);
    sel.innerHTML = '<option value="">' + placeholder + '</option>' +
      values.map(function (v) {
        return '<option value="' + esc(v) + '">' + esc(v) + '</option>';
      }).join('');
  }

  function refreshFacets() {
    var groups = {}, subjects = {}, batches = {};
    ROWS.forEach(function (row) {
      var g = pick(row, COLS, 'group_code');
      var s = pick(row, COLS, 'group_label') || pick(row, COLS, 'subject_req');
      var b = pick(row, COLS, 'batch');
      if (g) groups[g] = 1;
      if (s) subjects[s] = 1;
      if (b) batches[b] = 1;
    });
    var collator = new Intl.Collator('zh');
    fillSelect('f-group', Object.keys(groups).sort(collator.compare), '全部');
    fillSelect('f-subject', Object.keys(subjects).sort(collator.compare), '全部');
    fillSelect('f-batch', Object.keys(batches).sort(collator.compare), '全部');
    document.getElementById('f-group').value = state.groupCode;
    document.getElementById('f-subject').value = state.subjectReq;
    document.getElementById('f-batch').value = state.batch;
  }

  // ---------------------------------------------------------------- 事件

  function bindEvents() {
    document.getElementById('f-year').addEventListener('change', function (e) {
      state.year = Number(e.target.value);
      state.expandedSchools = Object.create(null);
      state.expandedGroups = Object.create(null);
      loadYear(state.year).then(function (payload) {
        var parsed = toObjects(payload);
        COLS = parsed.idx;
        ROWS = parsed.rows;
        TREE = buildTree(ROWS, COLS);
        refreshFacets();
        renderDifficulty();
        render();
      });
    });

    var schoolInput = document.getElementById('f-school');
    var timer = null;
    schoolInput.addEventListener('input', function (e) {
      var value = e.target.value;
      clearTimeout(timer);
      timer = setTimeout(function () {
        state.schoolQuery = value;
        render();
      }, 140);   // 输入防抖，避免每敲一个字就重排整表
    });

    document.getElementById('f-group').addEventListener('change', function (e) {
      state.groupCode = e.target.value; render();
    });
    document.getElementById('f-subject').addEventListener('change', function (e) {
      state.subjectReq = e.target.value; render();
    });
    document.getElementById('f-batch').addEventListener('change', function (e) {
      state.batch = e.target.value; render();
    });
    document.getElementById('f-scored').addEventListener('change', function (e) {
      state.onlyScored = e.target.checked; render();
    });
    document.getElementById('f-reset').addEventListener('click', function () {
      state.schoolQuery = ''; state.groupCode = ''; state.subjectReq = '';
      state.batch = ''; state.onlyScored = false;
      state.expandedSchools = Object.create(null);
      state.expandedGroups = Object.create(null);
      schoolInput.value = '';
      document.getElementById('f-scored').checked = false;
      refreshFacets();
      render();
    });

    // 事件委托：三级展开/收起
    document.getElementById('grid-body').addEventListener('click', function (ev) {
      var tr = ev.target.closest('tr');
      if (!tr || !ev.target.classList.contains('toggle')) return;

      if (tr.classList.contains('lv-school')) {
        var sk = tr.getAttribute('data-school');
        state.expandedSchools[sk] = !state.expandedSchools[sk];
      } else if (tr.classList.contains('lv-group')) {
        var gk = tr.getAttribute('data-group');
        state.expandedGroups[gk] = !state.expandedGroups[gk];
      }
      render();
    });
  }

  // ---------------------------------------------------------------- 页脚

  function renderFooter() {
    var src = document.getElementById('sources');
    src.innerHTML = '数据来源：' + (INDEX.sources || []).map(function (s) {
      return '<a href="' + esc(s.url) + '" target="_blank" rel="noopener">' + esc(s.name) + '</a>';
    }).join(' · ');

    document.getElementById('rank-note').textContent = INDEX.rank_note || '';

    var c = INDEX.counts || {};
    document.getElementById('meta').textContent =
      '数据更新：' + (INDEX.generated_at || '—') +
      ' · 版本 ' + (INDEX.data_version || '—') +
      ' · 学校 ' + (c.schools || 0) +
      ' · 专业 ' + (c.programs || 0) +
      ' · 录取记录 ' + (c.admissions || 0) +
      '（含分数 ' + (c.with_scores || 0) + '，可算位次 ' + (c.with_rank || 0) + '）';
  }

  // ---------------------------------------------------------------- 启动

  function init() {
    loadIndex().then(function (index) {
      INDEX = index;
      state.year = index.latest_year || LATEST_FALLBACK;

      var years = index.years && index.years.length ? index.years : [state.year];
      document.getElementById('f-year').innerHTML = years.map(function (y) {
        return '<option value="' + y + '"' + (y === state.year ? ' selected' : '') + '>' + y + ' 年</option>';
      }).join('');

      renderFooter();
      bindEvents();

      return loadYear(state.year).then(function (payload) {
        var parsed = toObjects(payload);
        COLS = parsed.idx;
        ROWS = parsed.rows;
        TREE = buildTree(ROWS, COLS);
        refreshFacets();
        renderDifficulty();
        render();
      });
    }).catch(function (err) {
      document.getElementById('meta').textContent = '数据加载失败：' + err.message;
      document.getElementById('empty').hidden = false;
      document.getElementById('empty').textContent =
        '未能加载数据。若你是双击打开本页面，浏览器会拦截本地 fetch；' +
        '请改用 HTTP 服务访问（python scripts/serve.py 或 python -m http.server），' +
        '并确认已运行导出脚本生成 web/data/ 下的 JSON。';
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
