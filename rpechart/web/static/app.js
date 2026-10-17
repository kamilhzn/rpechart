/* rpechart studio —— 参数 → 谱面 → 画面。
   无构建步骤：这个文件就是全部前端逻辑。

   结构
   ----
   util        小工具
   api         HTTP（统一错误语义）
   store       S：唯一状态对象
   form        左栏参数表单（写回 spec）
   editor      左栏 JSON 编辑器（权威输入）
   inspect     右栏校验 / 分线 / 概览
   stage       中间预览（帧缓存 + 预取）
   timeline    底部时间轴（刻度 / 胶片 / 音符轨 / 拖拽定位）
   playback    播放循环（限速 + 帧序号丢弃过期帧）
   exporter    导出 MP4（后台任务 + 进度轮询）
   快捷键与启动
*/
'use strict';

(function () {

/* ───────────────────────────── util ───────────────────────────── */

function $(id) { return document.getElementById(id); }
function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
function num(v, dflt) { var n = Number(v); return (v === '' || v === null || v === undefined || isNaN(n)) ? dflt : n; }
function mk(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = String(text);
  return e;
}
function clear(e) { while (e.firstChild) e.removeChild(e.firstChild); return e; }
function fmtTime(t) {
  // 先按毫秒取整再拆分钟/秒：59.9996 直接 toFixed 会得到「00:60.000」。
  var total = Math.max(0, Math.round((t || 0) * 1000));
  var m = Math.floor(total / 60000), s = (total % 60000) / 1000;
  return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s.toFixed(3);
}
function fmtBytes(n) {
  if (!n && n !== 0) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1048576).toFixed(2) + ' MB';
}
function getPath(o, path) {
  var cur = o, parts = path.split('.'), i, k;
  for (i = 0; i < parts.length; i++) {
    k = parts[i];
    if (cur === null || cur === undefined) return undefined;
    if (/^[0-9]+$/.test(k) && Object.prototype.toString.call(cur) === '[object Array]') cur = cur[Number(k)];
    else cur = cur[k];
  }
  return cur;
}
function setPath(o, path, value) {
  var parts = path.split('.'), cur = o, i, k;
  for (i = 0; i < parts.length - 1; i++) {
    k = parts[i];
    var key = /^[0-9]+$/.test(k) ? Number(k) : k;
    if (cur[key] === null || cur[key] === undefined || typeof cur[key] !== 'object') {
      cur[key] = /^[0-9]+$/.test(parts[i + 1]) ? [] : {};
    }
    cur = cur[key];
  }
  var last = parts[parts.length - 1];
  cur[/^[0-9]+$/.test(last) ? Number(last) : last] = value;
}

/* ───────────────────────────── 状态 ───────────────────────────── */

var S = {
  spec: null,          // 服务器权威 spec
  caps: null,          // /api/capabilities
  stats: null,         // /api/stats
  timeline: null,      // /api/timeline
  duration: 0,
  lastBeat: 0,
  bpm: 120,
  time: 0,
  fps: 60,
  playing: false,
  loading: 0,          // 进行中的请求数（状态栏）
  dirty: false,
  frameSeq: 0,
  frameKey: '',
  frameCache: {},      // key -> blob url（LRU 只标记不回收对象，见 trimFrameCache）
  frameOrder: [],
  inflight: {},
  scrubbing: false,
  filmKey: '',
  filmUrl: '',
  filmImg: null,
  filmView: null,      // 当前胶片覆盖的时间窗口（放大后按视野重取）
  filmError: '',
  filmSeq: 0,
  filmTimer: 0,
  seEnabled: true,     // 预览播放时是否叠加打击音
  seNotes: [],         // [{t, kind}]，按时间升序
  sePos: 0,
  seLast: 0,
  seVoices: [],
  issues: [],
  sev: { ERROR: true, WARNING: true, CAUTION: true },
  exportTimer: null,
  exportJob: null,
  fpsSamples: [],
  hover: null,
  lastError: '',
  jsonOk: true,
  /* 时间轴视野。zoom=1 且 start=0 时就是「整条铺满」。
     长谱面（几百拍）在整条视野里每拍不到一个像素，缩放是必需品而不是装饰。 */
  view: { zoom: 1, start: 0 },
  panning: null
};

/* 画布不是 CSS，取色只能自己来。但色值必须**只有一个来源**：
   app.css 的 :root 令牌。以前这里抄了一遍十六进制，改主题时 canvas
   会留在旧配色里 —— 那种不一致最难查。 */
var _TOKEN_CACHE = {};
function cssVar(name, fallback) {
  if (Object.prototype.hasOwnProperty.call(_TOKEN_CACHE, name)) return _TOKEN_CACHE[name];
  var v = '';
  try {
    v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  } catch (e) { v = ''; }
  _TOKEN_CACHE[name] = v || fallback;
  return _TOKEN_CACHE[name];
}
function dropTokenCache() { _TOKEN_CACHE = {}; }

/* canvas 没有 color-mix()，半透明色只能从令牌 + alpha 拼出来。
   好处同 cssVar：主题令牌是唯一来源。 */
function _rgba(hex, alpha) {
  var h = String(hex || '').trim().replace('#', '');
  if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
  if (h.length !== 6) return hex;
  var n = parseInt(h, 16);
  if (isNaN(n)) return hex;
  return 'rgba(' + ((n >> 16) & 255) + ', ' + ((n >> 8) & 255) + ', ' + (n & 255) + ', ' + alpha + ')';
}
function signalLine(alpha) { return _rgba(cssVar('--c-signal', '#4ee0f2'), alpha); }
function fgLine(alpha, token) { return _rgba(cssVar(token || '--c-fg', '#e6e9ec'), alpha); }

function noteColor(type) {
  var byType = {
    tap: cssVar('--c-tap', '#4ec9ff'),
    hold: cssVar('--c-hold', '#ffd166'),
    flick: cssVar('--c-flick', '#ff6b81'),
    drag: cssVar('--c-drag', '#b48cff')
  };
  return byType[type] || cssVar('--c-fg-4', '#4a5259');
}

var FRAME_CACHE_MAX = 360;

/* ───────────────────────────── api ───────────────────────────── */

async function api(path, opts) {
  var res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new Error('连不上后端：' + (e && e.message ? e.message : e));
  }
  var ctype = res.headers.get('content-type') || '';
  if (ctype.indexOf('application/json') >= 0) {
    var data;
    try { data = await res.json(); } catch (e2) { throw new Error('返回不是合法 JSON (HTTP ' + res.status + ')'); }
    if (!res.ok || data.ok === false) throw new Error(data.error || ('HTTP ' + res.status));
    return data;
  }
  if (!res.ok) {
    var msg = 'HTTP ' + res.status;
    try { var j = await res.json(); if (j && j.error) msg = j.error; } catch (e3) { /* 非 JSON 错误体 */ }
    throw new Error(msg);
  }
  return res;
}

function busy(on) {
  S.loading += on ? 1 : -1;
  if (S.loading < 0) S.loading = 0;
  paintStatus();
}

/* ───────────────────────────── 状态栏 / 吐司 ───────────────────────────── */

var STAT = { ok: 'ok', busy: 'busy', err: 'err', idle: '' };
function paintStatus(mode, text) {
  var dot = $('status-dot');
  var t = text;
  var m = mode;
  if (!m) {
    if (S.lastError) { m = 'err'; t = S.lastError; }
    else if (S.loading > 0) { m = 'busy'; t = '处理中…'; }
    else { m = 'ok'; t = S.playing ? '播放中' : '就绪'; }
  }
  dot.className = 'dot ' + (STAT[m] || '');
  $('status-text').textContent = t || '就绪';
}

function toast(text, kind, ms) {
  var box = $('toasts');
  var el = mk('div', 'toast' + (kind ? ' ' + kind : ''), text);
  box.appendChild(el);
  setTimeout(function () {
    el.style.opacity = '0';
    el.style.transition = 'opacity 160ms';
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 200);
  }, ms || 3200);
}

function banner(text, kind) {
  var b = $('banner');
  if (!text) { b.hidden = true; b.textContent = ''; return; }
  b.hidden = false;
  b.className = 'banner ' + (kind || 'err');
  b.textContent = text;
}

/* ───────────────────────────── 左栏：表单 ───────────────────────────── */

function group(title, hint) {
  var g = mk('div', 'group');
  var h = mk('div', 'group-head');
  h.appendChild(mk('span', null, title));
  if (hint) h.appendChild(mk('span', 'n', hint));
  g.appendChild(h);
  return g;
}

function fieldInto(host, label, path, opts) {
  opts = opts || {};
  var wrap = mk('label', 'fld');
  wrap.appendChild(mk('span', null, label));
  var input;
  if (opts.type === 'select') {
    input = mk('select');
    (opts.options || []).forEach(function (o) {
      var op = mk('option', null, o.label);
      op.value = o.value;
      input.appendChild(op);
    });
  } else if (opts.type === 'check') {
    input = mk('input');
    input.type = 'checkbox';
  } else {
    input = mk('input');
    input.type = opts.type || 'text';
    if (input.type === 'number') input.step = opts.step || 'any';
    if (opts.placeholder) input.placeholder = opts.placeholder;
  }
  var cur = getPath(S.spec, path);
  if (opts.type === 'check') input.checked = !!cur;
  else input.value = (cur === null || cur === undefined) ? (opts.dflt || '') : cur;
  function commit() {
    var v;
    if (opts.type === 'check') v = input.checked;
    else if (opts.type === 'number') {
      var raw = input.value.trim();
      if (raw === '') { input.className = 'bad'; return; }
      v = Number(raw);
      if (isNaN(v)) { input.className = 'bad'; return; }
      input.className = '';
    } else v = input.value;
    setPath(S.spec, path, v);
    markDirty();
    mirrorSpecToJson();
    if (opts.after) opts.after();
  }
  input.addEventListener('change', commit);
  if (opts.type === 'text') input.addEventListener('input', commit);
  wrap.appendChild(input);
  host.appendChild(wrap);
  return input;
}

function renderForm() {
  var box = clear($('form-fields'));
  var spec = S.spec || {};

  var g = group('元信息', '写入 META');
  var meta = mk('div', 'grid2');
  fieldInto(meta, '曲名 name', 'name');
  fieldInto(meta, '谱师 charter', 'charter');
  fieldInto(meta, '曲师 composer', 'composer');
  fieldInto(meta, '难度 level', 'level');
  g.appendChild(meta);
  box.appendChild(g);

  g = group('资源', '文件名，不是路径');
  var res = mk('div', 'grid2');
  fieldInto(res, '音频 song', 'song');
  fieldInto(res, '背景 background', 'background');
  fieldInto(res, '插画 illustrator', 'illustrator');
  fieldInto(res, '输出 out', 'out');
  g.appendChild(res);
  box.appendChild(g);

  g = group('时间', '秒 / 拍');
  var time = mk('div', 'grid2');
  fieldInto(time, 'BPM', 'bpm', { type: 'number' });
  fieldInto(time, '末拍 last_beat', 'last_beat', { type: 'number' });
  fieldInto(time, '时长 duration (s)', 'duration', { type: 'number' });
  fieldInto(time, '相位 phase (s)', 'phase', { type: 'number' });
  g.appendChild(time);
  box.appendChild(g);

  renderLineGroup(box);
  renderSectionGroup(box);

  $('form-note').textContent = '表单只改 spec，改完点右上角「生成」才会重算谱面与画面。'
    + '权威输入始终是 JSON 页签里的那一份文本。';
}

function renderLineGroup(box) {
  var lines = S.spec && S.spec.lines ? S.spec.lines : [];
  var g = group('判定线', lines.length + ' 条');
  lines.forEach(function (ln, i) {
    var card = mk('div', 'linecard');
    var head = mk('div', 'linecard-head');
    var sw = mk('span', 'swatch');
    var laneTokens = ['--c-tap', '--c-hold', '--c-flick', '--c-drag'];
    sw.style.background = cssVar(laneTokens[i % laneTokens.length], '#4a5259');
    head.appendChild(sw);
    head.appendChild(mk('span', null, '线 ' + (i + 1)));
    if (ln && ln.playable === false) head.appendChild(mk('span', 'n', '纯演出'));
    var sp = mk('span', 'tab-flex');
    head.appendChild(sp);
    var del = mk('button', 'btn tiny ghost', '移除');
    del.type = 'button';
    del.addEventListener('click', function () {
      S.spec.lines.splice(i, 1);
      markDirty(); mirrorSpecToJson(); renderForm();
    });
    head.appendChild(del);
    card.appendChild(head);
    var grid = mk('div', 'grid2');
    fieldInto(grid, '名称', 'lines.' + i + '.name');
    fieldInto(grid, 'y', 'lines.' + i + '.y', { type: 'number' });
    fieldInto(grid, 'x', 'lines.' + i + '.x', { type: 'number' });
    fieldInto(grid, 'z_order', 'lines.' + i + '.z_order', { type: 'number', step: '1' });
    card.appendChild(grid);
    var grid2 = mk('div', 'grid2');
    fieldInto(grid2, 'group', 'lines.' + i + '.group', { type: 'number', step: '1' });
    fieldInto(grid2, '参与落键', 'lines.' + i + '.playable', { type: 'check' });
    card.appendChild(grid2);
    g.appendChild(card);
  });
  var add = mk('button', 'btn tiny ghost', '+ 加一条判定线');
  add.type = 'button';
  add.addEventListener('click', function () {
    if (!S.spec.lines) S.spec.lines = [];
    S.spec.lines.push({ name: '线 ' + (S.spec.lines.length + 1), y: 0 });
    markDirty(); mirrorSpecToJson(); renderForm();
  });
  g.appendChild(add);
  box.appendChild(g);
}

function renderSectionGroup(box) {
  var secs = S.spec && S.spec.sections ? S.spec.sections : [];
  var g = group('段落', secs.length ? secs.length + ' 段' : '自动切段');
  secs.forEach(function (s, i) {
    var card = mk('div', 'linecard');
    var head = mk('div', 'linecard-head');
    head.appendChild(mk('span', null, '第 ' + (i + 1) + ' 段'));
    var sp = mk('span', 'tab-flex');
    head.appendChild(sp);
    var del = mk('button', 'btn tiny ghost', '移除');
    del.type = 'button';
    del.addEventListener('click', function () {
      S.spec.sections.splice(i, 1);
      markDirty(); mirrorSpecToJson(); renderForm();
    });
    head.appendChild(del);
    card.appendChild(head);
    var grid = mk('div', 'grid2');
    fieldInto(grid, '起拍', 'sections.' + i + '.start', { type: 'number' });
    fieldInto(grid, '止拍', 'sections.' + i + '.end', { type: 'number' });
    fieldInto(grid, '角色', 'sections.' + i + '.role', {
      type: 'select', options: [{ value: 'N', label: 'N 常规' }, { value: 'R', label: 'R 红' },
                                { value: 'B', label: 'B 蓝' }, { value: 'RB', label: 'RB 红蓝' }] });
    fieldInto(grid, '密度', 'sections.' + i + '.density', { type: 'number' });
    card.appendChild(grid);
    g.appendChild(card);
  });
  var add = mk('button', 'btn tiny ghost', '+ 加一段');
  add.type = 'button';
  add.addEventListener('click', function () {
    if (!S.spec.sections) S.spec.sections = [];
    var lastEnd = 0;
    S.spec.sections.forEach(function (s) { lastEnd = Math.max(lastEnd, num(s.end, 0)); });
    S.spec.sections.push({ start: lastEnd, end: lastEnd + 32, role: 'N' });
    markDirty(); mirrorSpecToJson(); renderForm();
  });
  g.appendChild(add);
  box.appendChild(g);
}

/* ───────────────────────────── 左栏：JSON 编辑器 ───────────────────────────── */

function mirrorSpecToJson() {
  var ta = $('spec');
  ta.value = JSON.stringify(S.spec, null, 2);
  paintGutter();
  paintJsonState(true);
}

function paintGutter() {
  var ta = $('spec');
  var lines = ta.value.split('\n').length;
  var txt = '', i;
  for (i = 1; i <= lines; i++) txt += i + '\n';
  var g = $('gutter');
  g.textContent = txt;
  g.scrollTop = ta.scrollTop;
}

function paintJsonState(parsedOk) {
  var st = $('json-state');
  var raw = $('spec').value;
  if (!parsedOk) {
    st.className = 'json-state bad';
    st.textContent = 'JSON 语法错误';
    return;
  }
  st.className = 'json-state' + (S.dirty ? '' : ' ok');
  st.textContent = S.dirty ? '有未生成的改动' : '与服务器一致';
}

function markDirty() {
  S.dirty = true;
  $('btn-discard').disabled = false;
  paintJsonState(true);
}

function parseEditor() {
  try {
    var v = JSON.parse($('spec').value);
    if (!v || typeof v !== 'object' || Object.prototype.toString.call(v) === '[object Array]') {
      throw new Error('顶层必须是对象');
    }
    return v;
  } catch (e) {
    paintJsonState(false);
    $('json-state').textContent = 'JSON 错误：' + e.message;
    throw e;
  }
}

/* ───────────────────────────── 右栏：检查器 ───────────────────────────── */

function renderIssues() {
  var box = clear($('issues'));
  var shown = S.issues.filter(function (it) { return S.sev[it.severity] !== false; });
  var pill = $('pill-issues');
  pill.textContent = String(S.issues.length);
  pill.className = 'pill' + (S.issues.some(function (i) { return i.severity === 'ERROR'; }) ? ' hot' : '');
  if (!S.issues.length) {
    box.appendChild(mk('div', 'empty ok', '没有校验问题。'));
    return;
  }
  if (!shown.length) {
    box.appendChild(mk('div', 'empty', '当前筛选下没有问题。'));
    return;
  }
  var order = { ERROR: 0, WARNING: 1, CAUTION: 2 };
  shown.slice().sort(function (a, b) {
    // ERROR 的序号是 0；用 ?? 而不是 ||，否则错误反而排到最后。
    return (order[a.severity] ?? 9) - (order[b.severity] ?? 9);
  }).forEach(function (it) {
    var row = mk('div', 'issue');
    row.appendChild(mk('span', 'sev ' + it.severity, it.severity));
    var col = mk('div');
    col.appendChild(mk('span', 'msg', it.message));
    var bits = [it.code];
    if (it.line !== null && it.line !== undefined) bits.push('线 ' + it.line);
    if (it.time) bits.push(it.time);
    col.appendChild(mk('span', 'code', bits.join('  ·  ')));
    row.appendChild(col);
    box.appendChild(row);
  });
}

function renderLines() {
  var box = clear($('lines'));
  var per = (S.stats && S.stats.per_line) || [];
  if (!per.length) { box.appendChild(mk('div', 'empty', '还没有判定线。')); return; }
  per.forEach(function (ln) {
    var row = mk('div', 'lrow');
    row.appendChild(mk('span', 'idx', ln.index + 1));
    row.appendChild(mk('span', 'nm', ln.name || ('线 ' + (ln.index + 1))));
    row.appendChild(mk('span', 'idx', ln.notes + ' 键'));
    var kinds = mk('div', 'kinds');
    var names = { tap: 'Tap', hold: 'Hold', flick: 'Flick', drag: 'Drag' };
    Object.keys(ln.kinds || {}).forEach(function (k) {
      kinds.appendChild(mk('span', 'kind ' + k, (names[k] || k) + ' ' + ln.kinds[k]));
    });
    row.appendChild(kinds);
    box.appendChild(row);
  });
}

function renderInfo() {
  var dl = clear($('info'));
  var s = S.stats || {};
  var cap = S.caps || {};
  var rows = [
    ['谱面名', s.name],
    ['物量', s.notes],
    ['判定线', s.lines],
    ['BPM', s.bpm != null ? String(s.bpm) : '—'],
    ['末拍', s.last_beat != null ? String(s.last_beat) : '—'],
    ['时长', s.duration != null ? s.duration.toFixed(3) + ' s' : '—'],
    ['校验', s.report ? (s.report.counts.error + ' 错误 / ' + s.report.counts.warning + ' 警告 / ' + s.report.counts.caution + ' 提示') : '—'],
    ['chart.json', s.chart_path],
    ['Pillow', cap.pillow ? '可用' : '缺失'],
    ['ffmpeg', cap.ffmpeg ? (cap.ffmpeg_path || '可用') : '缺失（无法导出 MP4）'],
    ['官方素材', cap.assets ? (cap.assets_path || '已找到') : '缺失 · 预览为内置键形'],
    ['打击音素材', cap.se && cap.se.length ? cap.se.join(' / ') + '（' + (cap.se_dir || '') + '）' : '缺失 · 导出时不加 SE'],
    ['Python', cap.python],
  ];
  rows.forEach(function (r) {
    dl.appendChild(mk('dt', null, r[0]));
    var dd = mk('dd', null, r[1] === null || r[1] === undefined ? '—' : String(r[1]));
    if (r[0] === 'ffmpeg' && cap.ffmpeg === false) dd.className = 'warn';
    if (r[0] === '官方素材' && cap.assets === false) dd.className = 'warn';
    if (r[0] === '打击音素材' && !(cap.se && cap.se.length)) dd.className = 'warn';
    dl.appendChild(dd);
  });
}

function renderCaps() {
  var box = clear($('caps'));
  var c = S.caps;
  if (!c) return;
  function add(label, state, title) {
    var e = mk('span', 'cap ' + state, label);
    if (title) e.title = title;
    box.appendChild(e);
  }
  add('pillow', c.pillow ? 'on' : 'bad', c.pillow ? 'Pillow 可用' : '缺 Pillow：预览不可用');
  add('ffmpeg', c.ffmpeg ? 'on' : 'bad', c.ffmpeg ? ('ffmpeg: ' + (c.ffmpeg_path || '可用')) : '缺 ffmpeg：导出 MP4 不可用');
  add('贴图', c.assets ? 'on' : 'off', c.assets ? ('官方素材: ' + (c.assets_path || '')) : '未找到官方素材，预览走内置键形');
  var se = c.se || [];
  add('SE', se.length ? 'on' : 'off',
      se.length ? ('打击音: ' + se.join('/') + ' @ ' + (c.se_dir || ''))
                : '未找到打击音素材（tap/flick/drag），导出时不叠音效');
}

/* ───────────────────────────── 舞台 ───────────────────────────── */

function stageSize() {
  var v = $('size').value.split('x');
  return [Number(v[0]), Number(v[1])];
}

function frameKey(t) {
  var wh = stageSize();
  var idx = Math.round(t * S.fps);
  // fps 必须进键：换帧率后同一个 idx 对应的秒数不同，旧键不能复用。
  return idx + '|' + wh[0] + 'x' + wh[1] + '|' + ($('hud').checked ? 1 : 0) + '|'
    + ($('grid').checked ? 1 : 0) + '|' + S.fps;
}

function frameUrl(key) {
  var p = key.split('|');
  var idx = Number(p[0]);
  var fps = Number(p[4]) || S.fps;
  var wh = p[1].split('x');
  return '/api/frame.jpg?t=' + (idx / fps).toFixed(4) + '&w=' + wh[0] + '&h=' + wh[1]
    + '&hud=' + p[2] + '&grid=' + p[3];
}

/* 命中缓存时把键移到队尾：frameOrder 是淘汰顺序，不刷新就退化成 FIFO。 */
function touchFrame(key) {
  var i = S.frameOrder.indexOf(key);
  if (i >= 0) S.frameOrder.splice(i, 1);
  S.frameOrder.push(key);
}

function trimFrameCache() {
  while (S.frameOrder.length > FRAME_CACHE_MAX) {
    var k = S.frameOrder.shift();
    if (S.frameCache[k]) {
      try { URL.revokeObjectURL(S.frameCache[k]); } catch (e) { /* 已回收 */ }
      delete S.frameCache[k];
    }
  }
}

function fetchFrame(key) {
  if (S.frameCache[key]) { touchFrame(key); return Promise.resolve(S.frameCache[key]); }
  if (S.inflight[key]) return S.inflight[key];
  var p = fetch(frameUrl(key)).then(function (res) {
    if (!res.ok) throw new Error('帧渲染失败 HTTP ' + res.status);
    return res.blob();
  }).then(function (blob) {
    var url = URL.createObjectURL(blob);
    S.frameCache[key] = url;
    S.frameOrder.push(key);
    trimFrameCache();
    delete S.inflight[key];
    return url;
  }).catch(function (e) {
    delete S.inflight[key];
    throw e;
  });
  S.inflight[key] = p;
  return p;
}

var frameTimer = 0;
function showFrame(t, opts) {
  opts = opts || {};
  var key = frameKey(t);
  if (key === S.frameKey && !opts.force) return;
  var seq = ++S.frameSeq;
  var t0 = (window.performance && performance.now) ? performance.now() : Date.now();
  if (!S.frameKey) $('stage-load').hidden = false;
  fetchFrame(key).then(function (url) {
    // 只有最新一次请求能落地；force 只用来绕过「同 key 早退」，不能绕过时序。
    if (seq !== S.frameSeq) return;
    var img = $('frame');
    img.src = url;
    S.frameKey = key;
    $('stage-empty').hidden = true;
    $('stage-load').hidden = true;
    var dt = ((window.performance && performance.now) ? performance.now() : Date.now()) - t0;
    frameTimer = dt;
    $('frame-stat').textContent = (dt < 1 ? '<1' : dt.toFixed(0)) + ' ms · ' + (S.caps && S.caps.assets ? '官方贴图' : '内置键形兜底');
    if (S.playing) recordFps(dt);
  }).catch(function (e) {
    if (seq !== S.frameSeq) return;
    $('stage-load').hidden = true;
    if (S.playing) { /* 一张帧失败不打断播放 */ }
    else banner('预览失败：' + e.message, 'err');
  });
}

function recordFps(dt) {
  S.fpsSamples.push(dt);
  if (S.fpsSamples.length > 24) S.fpsSamples.shift();
  var avg = 0;
  S.fpsSamples.forEach(function (x) { avg += x; });
  avg /= S.fpsSamples.length;
  $('tl-clock').textContent = '端到端 ' + (avg > 0 ? (1000 / avg).toFixed(1) : '0') + ' fps';
}

function prefetchAround(t) {
  var limit = S.playing ? 10 : 3;
  var i, key, n = 0;
  for (i = 1; i <= limit; i++) {
    if (Object.keys(S.inflight).length > 6) return;
    key = frameKey(t + i / S.fps);
    if (!S.frameCache[key] && !S.inflight[key]) {
      fetchFrame(key).catch(function () { /* 预取失败无所谓 */ });
      n++;
    }
  }
  return n;
}

/* ───────────────────────────── 时间轴 ───────────────────────────── */

var TL = { left: 52, right: 12, filmH: 40, rulerH: 18, rowGap: 3, padTop: 4 };

function tlDuration() {
  var d = S.duration;
  if (!d && S.timeline && S.timeline.duration_sec) d = S.timeline.duration_sec;
  return Math.max(1, d || 1);
}

function tlLayout() {
  var cv = $('tl');
  var w = cv.clientWidth, h = cv.clientHeight;
  var lines = (S.timeline && S.timeline.lines) || [];
  var n = Math.max(1, lines.length);
  var avail = h - TL.padTop - TL.rulerH - TL.filmH - 6;
  var rowH = clamp(avail / n - TL.rowGap, 8, 26);
  return {
    w: w, h: h, lines: lines,
    x0: TL.left, x1: Math.max(TL.left + 20, w - TL.right),
    filmTop: TL.padTop + TL.rulerH,
    rowTop: TL.padTop + TL.rulerH + TL.filmH + 4,
    rowH: rowH
  };
}

var VIEW_MAX_ZOOM = 400;

function viewSpan() {
  return tlDuration() / clamp(S.view.zoom, 1, VIEW_MAX_ZOOM);
}

/* 视野不许跑出谱面之外：span 大于全长时对齐到 0，否则把 start 夹进
   [0, duration - span]。这样缩放后画面里永远有内容，不会停在空白上。 */
function clampView() {
  var d = tlDuration();
  var span = viewSpan();
  if (span >= d) { S.view.zoom = 1; S.view.start = 0; return; }
  S.view.start = clamp(S.view.start, 0, d - span);
}

function timeToX(t, L) {
  var span = viewSpan();
  return L.x0 + ((t - S.view.start) / span) * (L.x1 - L.x0);
}
function xToTime(x, L) {
  var span = viewSpan();
  return S.view.start + clamp((x - L.x0) / Math.max(1, L.x1 - L.x0), 0, 1) * span;
}

/* 以 anchor（秒）为不动点改缩放：滚轮悬停在哪，哪一点就留在鼠标下面。 */
function zoomTo(z, anchor) {
  var before = viewSpan();
  S.view.zoom = clamp(z, 1, VIEW_MAX_ZOOM);
  var after = viewSpan();
  if (after >= tlDuration()) { S.view.zoom = 1; S.view.start = 0; }
  else S.view.start += (anchor - S.view.start) * (1 - after / before);
  clampView();
  scheduleFilmstrip();
}

/* 键盘缩放/翻页的公共动作：画布聚焦与全局快捷键走同一条路径。 */
function nudgeZoom(factor) {
  zoomTo(S.view.zoom * factor, S.time);
  paintTimeline();
  paintZoom();
}
function panView(fraction) {
  S.view.start += viewSpan() * fraction;
  clampView();
  paintTimeline();
  paintZoom();
  scheduleFilmstrip();
}

function tickStep(spanPx) {
  // 刻度间距按「视野里显示的秒数」挑，不是按全长 —— 放大后要出更细的刻度。
  var d = viewSpan();
  var steps = [1 / 30, 1 / 10, 1 / 4, 1 / 2, 1, 2, 5, 10, 15, 30, 60, 120, 300];
  for (var i = 0; i < steps.length; i++) {
    if (spanPx * steps[i] / d >= 62) return steps[i];
  }
  return steps[steps.length - 1];
}

var dpr = 1;
var hoverRaf = 0;
/* 悬停时 pointermove 可能一帧来十几次；合并成一次重绘。 */
function scheduleTimelinePaint() {
  if (hoverRaf) return;
  hoverRaf = requestAnimationFrame(function () { hoverRaf = 0; paintTimeline(); });
}
function paintTimeline() {
  var cv = $('tl');
  var wrap = cv.parentNode;
  var w = wrap.clientWidth, h = wrap.clientHeight;
  if (w <= 0 || h <= 0) return;
  dpr = Math.min(2, window.devicePixelRatio || 1);
  var cw = Math.round(w * dpr), ch = Math.round(h * dpr);
  // 只有尺寸真的变了才重设 width/height：赋值会清空并重新分配 backing store。
  if (cv.width !== cw || cv.height !== ch) {
    cv.width = cw;
    cv.height = ch;
  }
  var g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  var L = tlLayout();
  paintTicks(g, L);
  paintFilm(g, L);
  paintTracks(g, L);
  paintPlayhead(g, L);
}

function paintTicks(g, L) {
  var span = viewSpan();
  var step = tickStep(L.x1 - L.x0);
  // 只画视野内的刻度：放大到几百倍时从 0 开始遍历会白跑几万个点。
  var first = Math.floor(S.view.start / step) * step;
  var last = S.view.start + span;
  var t = first;
  g.font = '10px ui-monospace, Consolas, monospace';
  g.textBaseline = 'middle';
  g.strokeStyle = cssVar('--c-line', '#22272c');
  g.fillStyle = cssVar('--c-fg-4', '#4a5259');
  g.beginPath();
  while (t <= last + 1e-6) {
    var x = Math.round(timeToX(t, L)) + 0.5;
    g.moveTo(x, TL.padTop + 12);
    g.lineTo(x, TL.padTop + TL.rulerH);
    var label = step < 1 ? t.toFixed(2) + 's' : (t >= 60 ? fmtTime(t) : (t % 1 === 0 ? t + 's' : t.toFixed(2) + 's'));
    g.fillText(label, x + 3, TL.padTop + 6);
    t += step;
  }
  g.stroke();
}

function paintFilm(g, L) {
  var top = L.filmTop;
  var h = TL.filmH;
  var w = L.x1 - L.x0;
  if (w <= 0) return;
  if (S.filmImg && S.filmImg.width > 0) {
    // 胶片现在只覆盖它自己的时间窗口 filmView；放大/平移后若窗口还没换，
    // 先按比例裁旧图，等 debounce 后的视野胶片回来再整张铺满。
    var fv = S.filmView || { start: 0, end: tlDuration() };
    var img = S.filmImg;
    var fvSpan = Math.max(1e-6, fv.end - fv.start);
    var sx = clamp(((S.view.start - fv.start) / fvSpan) * img.width, 0, img.width);
    var sw = Math.min(img.width - sx, (viewSpan() / fvSpan) * img.width);
    g.globalAlpha = 0.92;
    if (sw > 0.5) g.drawImage(img, sx, 0, sw, img.height, L.x0, top, w, h);
    else g.fillRect(L.x0, top, w, h);
    g.globalAlpha = 1;
  } else {
    g.fillStyle = cssVar('--c-surface', '#101315');
    g.fillRect(L.x0, top, w, h);
    g.fillStyle = cssVar('--c-fg-4', '#4a5259');
    g.font = '10px ui-monospace, Consolas, monospace';
    g.textBaseline = 'middle';
    // 失败要留在画布上，不能永远显示「生成中」让人等一个不会来的结果。
    var filmMsg = S.filmError
      ? ('胶片不可用：' + String(S.filmError).slice(0, 42) + ' · 按 F 重试')
      : '胶片生成中…';
    g.fillText(filmMsg, L.x0 + 8, top + h / 2);
  }
  g.strokeStyle = cssVar('--c-line-2', '#2f353b');
  g.strokeRect(L.x0 + 0.5, top + 0.5, w - 1, h - 1);
  // 段落带：用角色给胶片下方加一条语义色带。
  // 这是谱面自带信息，和官方贴图是否找到无关，不能拿 S.caps 当门槛。
  var secs = (S.spec && S.spec.sections) ? S.spec.sections : null;
  if (secs && secs.length) {
    var bpm = S.bpm || 120;
    var band = 3;
    secs.forEach(function (s) {
      var a = timeToX(num(s.start, 0) * 60 / bpm, L);
      var b = timeToX(num(s.end, 0) * 60 / bpm, L);
      if (b < L.x0 || a > L.x1) return;   // 视野外不画，省得被自己挤出画布
      var colors = {
        N: cssVar('--c-tap', '#4ec9ff'), R: cssVar('--c-flick', '#ff6b81'),
        B: cssVar('--c-hold', '#ffd166'), RB: cssVar('--c-drag', '#b48cff')
      };
      g.fillStyle = colors[s.role] || cssVar('--c-fg-4', '#4a5259');
      g.globalAlpha = 0.75;
      g.fillRect(a, top + h - band, Math.max(1, b - a), band);
      g.globalAlpha = 1;
    });
  }
}

function paintTracks(g, L) {
  var lines = L.lines;
  g.font = '10px ui-monospace, Consolas, monospace';
  g.textBaseline = 'middle';
  if (!lines.length) {
    g.fillStyle = cssVar('--c-fg-4', '#4a5259');
    g.fillText('没有判定线', L.x0 + 8, L.rowTop + 10);
    return;
  }
  lines.forEach(function (ln, i) {
    var y = L.rowTop + i * (L.rowH + TL.rowGap);
    if (y > L.h - 4) return;
    g.fillStyle = i % 2 ? cssVar('--c-bg', '#0a0c0e') : cssVar('--c-surface', '#101315');
    g.fillRect(L.x0, y, L.x1 - L.x0, L.rowH);
    g.strokeStyle = cssVar('--c-surface-3', '#1d2125');
    g.beginPath();
    g.moveTo(L.x0, Math.round(y + L.rowH) + 0.5);
    g.lineTo(L.x1, Math.round(y + L.rowH) + 0.5);
    g.stroke();
    // 行标
    g.fillStyle = cssVar('--c-fg-3', '#6f7880');
    g.fillText((ln.index + 1) + ' ' + String(ln.name || '').slice(0, 6), 4, y + L.rowH / 2);
    // 音符。视野外的直接跳过：放大到 50 倍时可见区间只是全长的 2%，
    // 还去遍历全部音符就是白花时间（长谱面上万音符时肉眼可见地卡）。
    var notes = ln.notes || [];
    var bar = Math.max(2, L.rowH * 0.62);
    var t0 = S.view.start, t1 = S.view.start + viewSpan();
    for (var k = 0; k < notes.length; k++) {
      var nt = notes[k];
      if (nt.end < t0 || nt.start > t1) continue;
      var x = timeToX(nt.start, L);
      var xEnd = nt.hold ? timeToX(Math.min(nt.end, t1), L) : x + 2;
      var wpx = Math.max(2, xEnd - x);
      g.fillStyle = noteColor(nt.type);
      if (nt.fake) g.globalAlpha = 0.42;
      g.fillRect(x, y + (L.rowH - bar) / 2, wpx, bar);
      g.globalAlpha = 1;
    }
    // 事件轨：速度 / 旋转 / alpha 的高低线，只画一条速度线（最有信息量）
    var sp = (ln.events && ln.events.speed) || [];
    if (sp.length > 1) {
      // 归一化用的 maxV 必须按全部事件算，否则缩放后纵轴刻度会随视野跳变
      var maxV = 0.001;
      sp.forEach(function (e) { maxV = Math.max(maxV, Math.abs(e[2]), Math.abs(e[3])); });
      g.strokeStyle = signalLine(0.55);
      g.beginPath();
      var started = false;
      for (var m = 0; m < sp.length; m++) {
        var e = sp[m];
        // 区间与视野窗口「和之前」的才画；此时坐标已在 L.x0/L.x1 的映射下越界，
        // 交给 canvas 裁剪即可，省掉的是折线段本身的点数。
        if (e[1] < t0 || e[0] > t1) continue;
        var xs = timeToX(e[0], L), xe = timeToX(e[1], L);
        var ys = y + L.rowH - 1 - clamp(Math.abs(e[2]) / maxV, 0, 1) * (L.rowH - 2);
        var ye = y + L.rowH - 1 - clamp(Math.abs(e[3]) / maxV, 0, 1) * (L.rowH - 2);
        if (!started) { g.moveTo(xs, ys); started = true; } else { g.lineTo(xs, ys); }
        g.lineTo(xe, ye);
      }
      if (started) g.stroke();
    }
  });
}

function paintPlayhead(g, L) {
  var x = Math.round(timeToX(S.time, L)) + 0.5;
  if (x < L.x0 - 1 || x > L.x1 + 1) { if (S.hover) paintHover(g, L); return; }
  var clock = cssVar('--c-clock', '#ffb648');
  g.strokeStyle = clock;
  g.lineWidth = 1;
  g.beginPath();
  g.moveTo(x, 0);
  g.lineTo(x, L.h);
  g.stroke();
  g.fillStyle = clock;
  g.beginPath();
  g.moveTo(x - 4, 0);
  g.lineTo(x + 4, 0);
  g.lineTo(x, 6);
  g.closePath();
  g.fill();
  if (S.hover) paintHover(g, L);
}

function paintHover(g, L) {
  var li = S.hover.line;
  var ln = L.lines[li];
  if (!ln) return;
  var y = L.rowTop + li * (L.rowH + TL.rowGap);
  var x = S.hover.x;
  g.strokeStyle = fgLine(0.45);
  g.beginPath();
  g.moveTo(Math.round(x) + 0.5, 0);
  g.lineTo(Math.round(x) + 0.5, L.h);
  g.stroke();
  g.strokeStyle = fgLine(0.8);
  g.strokeRect(L.x0 + 0.5, y + 0.5, L.x1 - L.x0 - 1, L.rowH - 1);
  var t = xToTime(x, L);
  var label = fmtTime(t) + '  ·  线 ' + (ln.index + 1) + '  ·  ' + countNear(ln, t) + ' 键';
  g.font = '10px ui-monospace, Consolas, monospace';
  var tw = g.measureText(label).width + 10;
  var tx = clamp(x + 8, L.x0, L.x1 - tw);
  var ty = y + L.rowH + 4;
  if (ty > L.h - 16) ty = y - 16;
  g.fillStyle = fgLine(0.96, '--c-float');
  g.fillRect(tx, ty, tw, 14);
  g.fillStyle = cssVar('--c-fg', '#e6e9ec');
  g.textBaseline = 'middle';
  g.fillText(label, tx + 5, ty + 7.5);
}

function countNear(ln, t) {
  var win = 0.35, n = 0;
  var notes = ln.notes || [];
  // notes 由 /api/timeline 按 start 升序返回：二分到窗口左边界，再往后数。
  var lo = 0, hi = notes.length;
  while (lo < hi) {
    var mid = (lo + hi) >> 1;
    if (notes[mid].start < t - win) lo = mid + 1;
    else hi = mid;
  }
  for (var i = lo; i < notes.length; i++) {
    if (notes[i].start > t + win) break;
    n++;
  }
  return n;
}

/* ───────────────────────────── 播放 ───────────────────────────── */

function seek(t, fromUser) {
  S.time = clamp(t, 0, tlDuration());
  S.seLast = S.time;
  S.sePos = seLowerBound(S.time);
  stopSeVoices();
  paintTimeReadout();
  paintTimeline();
  showFrame(S.time);
  if (!S.playing) prefetchAround(S.time);
  if (fromUser && S.playing) { /* 拖动时继续保持播放速度基准 */ }
}

function paintTimeReadout() {
  $('timecode').textContent = fmtTime(S.time);
  var beat = S.bpm > 0 ? S.time * S.bpm / 60 : 0;
  $('tc-sub').textContent = '拍 ' + beat.toFixed(2);
  $('stage-time').textContent = S.time.toFixed(3) + 's';
  paintZoom();
  // 时间轴画布对读屏是 role=slider，值必须跟着走，否则读屏一直念「0 秒」。
  var tl = $('tl');
  tl.setAttribute('aria-valuenow', S.time.toFixed(3));
  tl.setAttribute('aria-valuemax', tlDuration().toFixed(3));
  tl.setAttribute('aria-valuetext', fmtTime(S.time));
}


/* ───────────────────────────── 预览打击音 ───────────────────────────── */

var seCtx = null;
var seBuffers = {};
var seLoading = null;
var seVolume = 0.532;

function seKind(type) {
  if (type === 'flick') return 'flick';
  if (type === 'drag') return 'drag';
  if (type === 'tap' || type === 'hold') return 'tap';
  return '';
}

function rebuildSeNotes() {
  var out = [];
  var lines = (S.timeline && S.timeline.lines) || [];
  lines.forEach(function (ln) {
    (ln.notes || []).forEach(function (nt) {
      if (nt.fake) return;
      var kind = seKind(nt.type);
      if (kind) out.push({ t: nt.start, kind: kind });
    });
  });
  out.sort(function (a, b) { return a.t - b.t; });
  S.seNotes = out;
  S.sePos = seLowerBound(S.time);
}

function seLowerBound(t) {
  var lo = 0, hi = S.seNotes.length;
  while (lo < hi) {
    var mid = (lo + hi) >> 1;
    if (S.seNotes[mid].t < t) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

function ensureSeAudio() {
  var AC = window.AudioContext || window.webkitAudioContext;
  if (!AC) return null;
  if (!seCtx) {
    try { seCtx = new AC(); } catch (e) { return null; }
  }
  if (seCtx.state === 'suspended') {
    try {
      var resumed = seCtx.resume();
      // resume() 返回 Promise；不在用户手势里调用时会被拒绝，吞掉避免 unhandled rejection。
      if (resumed && typeof resumed.catch === 'function') resumed.catch(function () {});
    } catch (e2) { /* 用户手势之外可能失败 */ }
  }
  return seCtx;
}

async function loadSeBuffers() {
  var kinds = (S.caps && S.caps.se) || [];
  if (!kinds.length) return null;
  if (seLoading) return seLoading;
  seLoading = (async function () {
    var ctx = ensureSeAudio();
    if (!ctx) return;
    seVolume = num(S.caps && S.caps.se_volume, 0.532);
    for (var i = 0; i < kinds.length; i++) {
      var kind = kinds[i];
      if (!kind || seBuffers[kind]) continue;
      try {
        var res = await api('/api/se/' + encodeURIComponent(kind));
        var raw = await res.arrayBuffer();
        var decoded = await new Promise(function (resolve, reject) {
          try { ctx.decodeAudioData(raw.slice(0), resolve, reject); } catch (e) { reject(e); }
        });
        seBuffers[kind] = decoded;
      } catch (e2) { /* 单个素材失败不影响其它素材 */ }
    }
  })();
  try { await seLoading; } finally { seLoading = null; }
  return null;
}

function stopSeVoices() {
  var voices = (S.seVoices || []).slice();
  S.seVoices = [];
  voices.forEach(function (src) {
    try { src.stop(); } catch (e) { /* 已经结束 */ }
  });
}

function playSeKind(kind) {
  var buf = seBuffers[kind];
  if (!buf || !seCtx || !S.seEnabled) return;
  var src = seCtx.createBufferSource();
  src.buffer = buf;
  var gain = seCtx.createGain();
  gain.gain.value = clamp(seVolume, 0, 1) * 0.9;
  src.connect(gain);
  gain.connect(seCtx.destination);
  src.onended = function () {
    var i = S.seVoices.indexOf(src);
    if (i >= 0) S.seVoices.splice(i, 1);
  };
  S.seVoices.push(src);
  src.start();
}

/* 只触发 (t0, t1] 范围内的音符；t0 恰好是上一帧，避免同一音符每帧重放。 */
function playSeBetween(t0, t1) {
  if (!S.seEnabled || !S.seNotes.length || !seCtx) return;
  var i = seLowerBound(t0);
  while (i < S.seNotes.length) {
    var nt = S.seNotes[i];
    if (nt.t > t1 + 1e-6) break;
    if (nt.t > t0 + 1e-6) playSeKind(nt.kind);
    i++;
  }
}


var rafId = null;
var playT0 = 0;
var playFrom = 0;
/* SVG 元素没有 hidden IDL 属性：给 svg.hidden 赋值只会挂一个 expando，
   既不改属性也不改样式。图标切换必须走 setAttribute/removeAttribute。 */
function setPlayingIcon(playing) {
  var btn = $('btn-play');
  var play = btn.querySelector('.i-play');
  var pause = btn.querySelector('.i-pause');
  if (playing) {
    play.setAttribute('hidden', '');
    pause.removeAttribute('hidden');
  } else {
    play.removeAttribute('hidden');
    pause.setAttribute('hidden', '');
  }
  btn.setAttribute('aria-pressed', playing ? 'true' : 'false');
}
function play() {
  if (S.playing) return;
  if (S.time >= tlDuration() - 1e-3) S.time = 0;
  S.playing = true;
  ensureSeAudio();
  loadSeBuffers();
  S.seLast = S.time - 1e-4;
  S.sePos = seLowerBound(S.time);
  playFrom = S.time;
  playT0 = (window.performance && performance.now) ? performance.now() : Date.now();
  setPlayingIcon(true);
  // aria-label 必须跟着图标走，否则读屏用户永远听到「播放」。
  $('btn-play').setAttribute('aria-label', '暂停');
  $('btn-play').setAttribute('title', '播放 / 暂停 (空格)');
  S.fpsSamples = [];
  paintStatus('busy', '播放中');
  var tick = function () {
    if (!S.playing) return;
    var now = (window.performance && performance.now) ? performance.now() : Date.now();
    var t = playFrom + (now - playT0) / 1000;
    if (t >= tlDuration()) { t = tlDuration(); pause(); seek(t, false); return; }
    S.time = t;
    playSeBetween(S.seLast, t);
    S.seLast = t;
    paintTimeReadout();
    paintTimeline();
    showFrame(t);
    prefetchAround(t);
    rafId = requestAnimationFrame(tick);
  };
  rafId = requestAnimationFrame(tick);
}

function pause() {
  if (!S.playing) return;
  S.playing = false;
  if (rafId) cancelAnimationFrame(rafId);
  rafId = null;
  stopSeVoices();
  setPlayingIcon(false);
  $('btn-play').setAttribute('aria-label', '播放');
  paintStatus();
}

/* ───────────────────────────── 数据装载 ───────────────────────────── */

function applyStats(data) {
  S.stats = data;
  S.duration = data.duration || 0;
  S.lastBeat = data.last_beat || 0;
  S.bpm = data.bpm || 120;
  // 后端发的是小写 error/warning/caution，而 CSS 类名与筛选键是大写；
  // 不统一大小写，严重度配色、错误徽标与筛选会全部静默失效。
  S.issues = ((data.report && data.report.issues) || []).map(function (it) {
    it.severity = String(it.severity || '').toUpperCase();
    return it;
  });
  S.lastError = data.error || '';
  $('doc-name').value = data.name || '';
  $('doc-meta').textContent = data.lines + ' 线 · ' + data.notes + ' 音符 · '
    + (data.duration != null ? data.duration.toFixed(2) + 's' : '—');
  $('status-path').textContent = data.chart_path || '—';
  renderIssues();
  renderLines();
  renderInfo();
  if (S.lastError) banner('上次生成失败（仍在使用上一份可用谱面）：' + S.lastError, 'err');
  else if (S.caps && S.caps.assets === false) banner('未找到官方素材（PhiEdit/Resources），预览与导出使用内置键形兜底。设置 RPECHART_ASSETS 可指向素材目录。', 'warn');
  else banner('');
}

async function loadTimeline() {
  try {
    S.timeline = await api('/api/timeline');
    if (S.timeline.duration_sec) S.duration = S.timeline.duration_sec;
    if (S.timeline.last_beat) S.lastBeat = S.timeline.last_beat;
    if (S.timeline.bpm) S.bpm = S.timeline.bpm;
    if (S.timeline.truncated) toast('音符太多，时间轴只画了最早的一批。', 'warn');
  } catch (e) {
    S.timeline = null;
    toast('时间轴数据不可用：' + e.message, 'warn');
  }
  rebuildSeNotes();
  paintTimeReadout();
  paintTimeline();
}

/* 胶片只覆盖一个时间窗口：zoom=1 时是整条谱面，放大后是当前视野。
   不这样做的话，放大只能把整条 14 张缩略图裁下一小块硬拉满 —— 看起来
   「就是当前帧被拉长」，而且完全没有细节。 */
function filmWindow() {
  var d = tlDuration();
  if (S.view.zoom <= 1.001) return { start: 0, end: d };
  var span = Math.min(viewSpan(), d);
  var start = clamp(S.view.start, 0, Math.max(0, d - span));
  return { start: start, end: start + span };
}

function scheduleFilmstrip() {
  if (S.filmTimer) clearTimeout(S.filmTimer);
  S.filmTimer = setTimeout(function () {
    S.filmTimer = 0;
    loadFilmstrip();
  }, 180);
}

async function loadFilmstrip(force) {
  var v = filmWindow();
  var width = $('tl') ? $('tl').clientWidth : 960;
  // 每张缩略图约 120px 宽：既有足够的时间细节，又不会发几十个渲染请求。
  var n = clamp(Math.round(width / 120), 6, 16);
  var wh = (stageSize()[0] <= 640) ? [128, 72] : [160, 90];
  var key = [n, wh[0], wh[1], $('hud').checked ? 1 : 0,
    Math.round(tlDuration() * 100), Math.round(v.start * 100),
    Math.round(v.end * 100)].join('|');
  if (!force && key === S.filmKey) return;
  if (S.filmTimer) { clearTimeout(S.filmTimer); S.filmTimer = 0; }
  S.filmKey = key;
  var seq = ++S.filmSeq;
  try {
    var res = await api('/api/filmstrip.jpg?n=' + n + '&w=' + wh[0] + '&h=' + wh[1]
      + '&hud=' + ($('hud').checked ? 1 : 0) + '&q=72'
      + '&start=' + v.start.toFixed(3) + '&end=' + v.end.toFixed(3));
    var blob = await res.blob();
    if (seq !== S.filmSeq) return;
    var url = URL.createObjectURL(blob);
    var img = new Image();
    var old = S.filmUrl;
    img.onload = function () {
      if (seq !== S.filmSeq) { try { URL.revokeObjectURL(url); } catch (err0) { /* 已回收 */ } return; }
      S.filmImg = img;
      S.filmUrl = url;
      S.filmView = v;
      S.filmError = '';
      if (old) { try { URL.revokeObjectURL(old); } catch (e2) { /* 已回收 */ } }
      paintTimeline();
    };
    img.onerror = function () {
      if (seq !== S.filmSeq) return;
      S.filmImg = null;
      S.filmError = '图片解码失败';
      S.filmKey = '';
      paintTimeline();
    };
    img.src = url;
  } catch (e) {
    if (seq !== S.filmSeq) return;
    S.filmImg = null;
    S.filmError = e.message || '请求失败';
    S.filmKey = '';
    paintTimeline();
  }
}

async function refreshChartText() {
  try {
    var res = await fetch('/api/chart');
    if (!res.ok) throw new Error('HTTP ' + res.status);
    $('chart').textContent = await res.text();
  } catch (e) {
    // 错误页不是 chart.json；把它挡在概览之外，顺便留一条可诊断的提示。
    $('chart').textContent = 'chart.json 读取失败：' + (e && e.message ? e.message : e);
  }
}

async function loadCapabilities() {
  try {
    S.caps = await api('/api/capabilities');
  } catch (e) {
    S.caps = { pillow: true, ffmpeg: false, assets: false, python: '—' };
  }
  renderCaps();
  renderInfo();
  var hasSe = !!(S.caps.se && S.caps.se.length);
  $('se-switch').hidden = !hasSe;
  if (hasSe) {
    $('se-preview').checked = S.seEnabled;
    loadSeBuffers();
  }
  if (S.caps.assets === false) {
    $('asset-hint').hidden = false;
    $('asset-hint').textContent = '贴图缺失 · 预览为内置键形';
  }
}

async function loadSpec() {
  var data = await api('/api/spec');
  S.spec = data.spec || {};
  S.dirty = false;
  $('btn-discard').disabled = true;
  renderForm();
  mirrorSpecToJson();
}

async function refreshAll(opts) {
  opts = opts || {};
  busy(true);
  try {
    var stats = await api('/api/stats');
    applyStats(stats);
    await loadTimeline();
    if (opts.film !== false) loadFilmstrip();
    await refreshChartText();
    showFrame(S.time, { force: true });
  } finally { busy(false); }
}

/* ───────────────────────────── 生成 ───────────────────────────── */

async function regenerate() {
  pause();
  var body;
  try { body = parseEditor(); } catch (e) {
    banner('JSON 语法错误：' + e.message + '（先修正再生成）', 'err');
    switchTab('json');
    return;
  }
  busy(true);
  paintStatus('busy', '生成中…');
  banner('');
  try {
    var data = await api('/api/spec', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    S.spec = body;
    S.dirty = false;
    $('btn-discard').disabled = true;
    renderForm();
    mirrorSpecToJson();
    applyStats(data);
    S.frameKey = '';
    S.filmKey = '';
    clearFrameCache();
    await loadTimeline();
    loadFilmstrip();
    await refreshChartText();
    toast('生成完成 · ' + data.notes + ' 音符 / ' + data.lines + ' 线', 'ok');
    showFrame(S.time, { force: true });
  } catch (e) {
    banner('生成失败：' + e.message + '\n（服务器保留了上一份可用谱面）', 'err');
    toast('生成失败：' + e.message, 'err', 5000);
    paintStatus('err', '生成失败');
  } finally {
    busy(false);
    if (!S.lastError) paintStatus();
  }
}

function clearFrameCache() {
  Object.keys(S.frameCache).forEach(function (k) {
    try { URL.revokeObjectURL(S.frameCache[k]); } catch (e) { /* 已回收 */ }
  });
  S.frameCache = {};
  S.frameOrder = [];
  S.inflight = {};
  S.fpsSamples = [];
}

async function discardChanges() {
  try {
    await loadSpec();
    banner('');
    toast('已从服务器重新读取 spec', 'ok');
  } catch (e) {
    toast('读取失败：' + e.message, 'err');
  }
}

/* ───────────────────────────── 导出 ───────────────────────────── */

var lastFocus = null;
function openModal(id) {
  var m = $(id);
  if (!m.hidden) return;
  lastFocus = document.activeElement;
  m.hidden = false;
  // 导出浮层优先把焦点给第一个字段；帮助浮层没有字段，再退回标题栏的关闭键。
  var first = m.querySelector('.modal-body input, .modal-body select, .modal-body textarea, .modal-body button')
    || m.querySelector('button, [tabindex]:not([tabindex="-1"])');
  if (first) first.focus();
}
function closeModal(id) {
  var m = $(id);
  if (m.hidden) return;
  m.hidden = true;
  if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
  lastFocus = null;
}

function exportOpen() {
  openModal('modal-export');
  $('ex-msg').className = 'modal-msg';
  var m = $('ex-msg');
  if (!S.caps || !S.caps.ffmpeg) {
    m.className = 'modal-msg err';
    m.textContent = '后端找不到 ffmpeg（装 imageio-ffmpeg 即可）：'
      + 'pip install "rpechart[audio]"。当前无法导出 MP4。';
    $('ex-go').disabled = true;
  } else {
    m.textContent = '编码由一个后台任务完成，可以随时取消；完成后路径在下方提示。';
    $('ex-go').disabled = false;
    if (!$('ex-end').value && S.duration) $('ex-end').placeholder = S.duration.toFixed(2);
  }
  // 打击音：有素材就默认「自动」，并把后端报的音量填进输入框
  var caps = S.caps || {};
  var hasSe = !!(caps.se && caps.se.length);
  if (!$('ex-se-mode').dataset.touched) {
    $('ex-se-mode').value = hasSe ? 'auto' : 'none';
  }
  if (!$('ex-se-volume').dataset.touched) {
    $('ex-se-volume').value = String(caps.se_volume != null ? caps.se_volume : 0.532);
  }
  paintSeMode();
  $('ex-se-hint').textContent = hasSe
    ? ('已找到：' + caps.se.join(' / ') + '（' + (caps.se_dir || '') + '）· 蓝键 tap / 红键 flick / 黄键 drag，Hold 走蓝键')
    : '没有找到打击音素材。把 PhiEdit/Resources 放好，或设 RPECHART_SE_DIR 指向含 tap/flick/drag 的目录。';
  pollExport();
}

/** 导出时用哪种打击音：none / auto / 指定目录。 */
function exportHitsounds() {
  var mode = $('ex-se-mode').value;
  if (mode === 'auto') return 'auto';
  if (mode === 'dir') {
    var dir = $('ex-se-dir').value.trim();
    return dir || 'auto';    // 填了目录却留空，就当自动找
  }
  return null;
}

/** 选了「指定目录」才露出路径输入框。 */
function paintSeMode() {
  var mode = $('ex-se-mode').value;
  $('ex-se-dir-row').hidden = mode !== 'dir';
}

async function exportStart() {
  var endRaw = $('ex-end').value.trim();
  // select 的 value 在「当前选项与赋值不匹配」时会变成空串（脚本设值、
  // 或选项被改过之后就会遇到）。空串发过去后端只会回一句
  // 「尺寸要写成 宽x高：''」，看不出所以然，所以这里先兜到默认档位。
  var sizeRaw = $('ex-size').value || '1280x720';
  if (!/^[0-9]+x[0-9]+$/i.test(sizeRaw)) {
    $('ex-msg').className = 'modal-msg err';
    $('ex-msg').textContent = '分辨率要写成 宽x高，例如 1280x720';
    return;
  }
  var payload = {
    fps: num($('ex-fps').value, 60),
    size: sizeRaw,
    start: num($('ex-start').value, 0),
    end: endRaw === '' ? null : num(endRaw, null),
    hud: $('ex-hud').checked,
    audio: $('ex-audio').value.trim() || null,
    out: $('ex-out').value.trim() || undefined,
    crf: num($('ex-crf').value, 18),
    preset: $('ex-preset').value,
    hitsounds: exportHitsounds(),
    se_volume: num($('ex-se-volume').value, 0.532)
  };
  $('ex-go').disabled = true;
  $('ex-msg').className = 'modal-msg';
  $('ex-msg').textContent = '正在提交…';
  try {
    var data = await api('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    S.exportJob = data.job || null;
    $('ex-prog').hidden = false;
    $('ex-cancel').hidden = false;
    $('ex-msg').textContent = '导出中…';
    toast('开始导出：' + (payload.size) + ' @ ' + payload.fps + 'fps', 'ok');
    pollExport();
  } catch (e) {
    $('ex-go').disabled = false;
    $('ex-msg').className = 'modal-msg err';
    $('ex-msg').textContent = '提交失败：' + e.message;
  }
}

function pollExport() {
  if (S.exportTimer) { clearTimeout(S.exportTimer); S.exportTimer = null; }
  var step = async function () {
    S.exportTimer = null;
    var data;
    try {
      data = await api('/api/export/status');
    } catch (e) {
      $('ex-msg').className = 'modal-msg err';
      $('ex-msg').textContent = '进度查询失败：' + e.message;
      return;
    }
    var job = data.job;
    S.exportJob = job;
    if (job) {
      $('ex-prog').hidden = false;
      var pct = clamp((job.progress || 0) * 100, 0, 100);
      $('ex-bar').style.width = pct.toFixed(1) + '%';
      $('ex-state').textContent = ({ idle: '空闲', running: '导出中', done: '完成', failed: '失败', cancelled: '已取消' })[job.state] || job.state;
      $('ex-num').textContent = (job.total ? (job.done + ' / ' + job.total + ' 帧') : '') +
        (job.elapsed != null ? '  ·  ' + job.elapsed.toFixed(1) + 's' : '') +
        (job.bytes ? '  ·  ' + fmtBytes(job.bytes) : '');
      if (job.state === 'running') {
        $('ex-cancel').hidden = false;
        $('ex-go').disabled = true;
        $('ex-msg').className = 'modal-msg';
        $('ex-msg').textContent = '输出：' + (job.out || '—');
        // 唯一的调度点：终态/失败查询不再挂第二条链。
        S.exportTimer = setTimeout(step, 600);
        return;
      }
      $('ex-cancel').hidden = true;
      $('ex-go').disabled = false;
      if (job.state === 'done') {
        $('ex-msg').className = 'modal-msg ok';
        $('ex-msg').textContent = '导出完成：' + job.out + '（' + fmtBytes(job.bytes) + '）';
        toast('导出完成：' + job.out, 'ok', 8000);
      } else if (job.state === 'failed') {
        $('ex-msg').className = 'modal-msg err';
        $('ex-msg').textContent = '导出失败：' + (job.error || '未知原因');
        toast('导出失败：' + (job.error || ''), 'err', 6000);
      } else if (job.state === 'cancelled') {
        $('ex-msg').className = 'modal-msg';
        $('ex-msg').textContent = '已取消。';
      }
    } else {
      $('ex-prog').hidden = true;
      $('ex-state').textContent = '空闲';
    }
  };
  step();
}

async function exportCancel() {
  try {
    var data = await api('/api/export/cancel', { method: 'POST' });
    toast(data.cancelled ? '已请求取消导出' : '没有正在运行的导出', 'warn');
  } catch (e) {
    toast('取消失败：' + e.message, 'err');
  }
}

/* ───────────────────────────── 页签 ───────────────────────────── */

function switchTab(name) {
  qsa('.rail .tab[data-tab]').forEach(function (b) {
    var on = b.getAttribute('data-tab') === name;
    b.classList.toggle('is-on', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
    b.tabIndex = on ? 0 : -1;
  });
  qsa('.rail .tabpane').forEach(function (p) {
    p.classList.toggle('is-on', p.getAttribute('data-pane') === name);
  });
  S.frameKey = '';
}

function switchInspector(name) {
  qsa('.inspector .tab[data-itab]').forEach(function (b) {
    var on = b.getAttribute('data-itab') === name;
    b.classList.toggle('is-on', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
    b.tabIndex = on ? 0 : -1;
  });
  qsa('.inspector .tabpane').forEach(function (p) {
    p.classList.toggle('is-on', p.getAttribute('data-ipane') === name);
  });
}

/* ───────────────────────────── 事件绑定 ───────────────────────────── */

/* role=tab 的键盘约定：整组只留一个 tabindex=0，左右键移动焦点并切页。 */
function bindTabKeys(scope, attr, activate) {
  var tabs = qsa(scope + ' .tab[' + attr + ']');
  tabs.forEach(function (b, i) {
    b.tabIndex = b.classList.contains('is-on') ? 0 : -1;
    b.addEventListener('keydown', function (e) {
      var step;
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') step = 1;
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') step = -1;
      else if (e.key === 'Home') step = -i;
      else if (e.key === 'End') step = tabs.length - 1 - i;
      else return;
      e.preventDefault();
      var j = (i + step + tabs.length) % tabs.length;
      tabs[j].focus();
      activate(tabs[j].getAttribute(attr));
    });
  });
}

function bind() {
  $('btn-gen').addEventListener('click', regenerate);
  $('btn-apply').addEventListener('click', regenerate);
  $('btn-discard').addEventListener('click', discardChanges);
  $('btn-template').addEventListener('click', useTemplate);
  $('doc-name').addEventListener('change', function () {
    S.spec.name = $('doc-name').value;
    markDirty();
    mirrorSpecToJson();
  });

  qsa('.rail .tab[data-tab]').forEach(function (b) {
    b.addEventListener('click', function () { switchTab(b.getAttribute('data-tab')); });
  });
  qsa('.inspector .tab[data-itab]').forEach(function (b) {
    b.addEventListener('click', function () { switchInspector(b.getAttribute('data-itab')); });
  });
  bindTabKeys('.rail', 'data-tab', switchTab);
  bindTabKeys('.inspector', 'data-itab', switchInspector);
  qsa('#sev-filters .chip').forEach(function (c) {
    c.addEventListener('click', function () {
      var sev = c.getAttribute('data-sev');
      S.sev[sev] = !(S.sev[sev] !== false);
      c.classList.toggle('is-on', S.sev[sev] !== false);
      renderIssues();
    });
  });

  var ta = $('spec');
  ta.addEventListener('input', function () {
    S.dirty = true;
    $('btn-discard').disabled = false;
    paintGutter();
    var raw = ta.value;
    try { JSON.parse(raw); S.jsonOk = true; } catch (err) { S.jsonOk = false; }
    paintJsonState(S.jsonOk);
  });
  ta.addEventListener('scroll', function () { $('gutter').scrollTop = ta.scrollTop; });
  ta.addEventListener('keydown', function (e) {
    if (e.key === 'Tab') {
      e.preventDefault();
      var s = ta.selectionStart, t2 = ta.selectionEnd;
      ta.value = ta.value.slice(0, s) + '  ' + ta.value.slice(t2);
      ta.selectionStart = ta.selectionEnd = s + 2;
      ta.dispatchEvent(new Event('input'));
    }
  });

  $('btn-chart').addEventListener('click', function () {
    var on = this.getAttribute('aria-expanded') === 'true';
    this.setAttribute('aria-expanded', on ? 'false' : 'true');
    $('chart').hidden = on;
  });

  $('btn-play').addEventListener('click', function () { if (S.playing) pause(); else play(); });
  $('btn-home').addEventListener('click', function () { pause(); seek(0, true); });
  $('btn-end').addEventListener('click', function () { pause(); seek(tlDuration(), true); });
  $('btn-zoom-in').addEventListener('click', function () { nudgeZoom(1.5); });
  $('btn-zoom-out').addEventListener('click', function () { nudgeZoom(1 / 1.5); });
  $('btn-zoom-fit').addEventListener('click', resetZoom);
  $('hud').addEventListener('change', function () { S.frameKey = ''; S.filmKey = ''; showFrame(S.time, { force: true }); loadFilmstrip(); });
  $('grid').addEventListener('change', function () { S.frameKey = ''; showFrame(S.time, { force: true }); });
  $('se-preview').addEventListener('change', function () {
    S.seEnabled = !!this.checked;
    if (!S.seEnabled) stopSeVoices();
    else { ensureSeAudio(); loadSeBuffers(); }
  });
  $('size').addEventListener('change', function () {
    // 胶片缩略图的尺寸也跟预览档位走，换尺寸必须重载。
    S.frameKey = '';
    S.filmKey = '';
    showFrame(S.time, { force: true });
    loadFilmstrip();
  });
  $('fps').addEventListener('change', function () {
    S.fps = num($('fps').value, 60);
    S.frameKey = '';
    // 帧键带 fps，换档后必须立刻按新帧率重取当前帧，否则画面停在旧时间点。
    showFrame(S.time, { force: true });
    prefetchAround(S.time);
  });

  $('btn-export').addEventListener('click', exportOpen);
  $('ex-go').addEventListener('click', exportStart);
  $('ex-se-mode').addEventListener('change', function () {
    this.dataset.touched = '1';
    paintSeMode();
  });
  $('ex-se-dir').addEventListener('input', function () { this.dataset.touched = '1'; });
  $('ex-se-volume').addEventListener('input', function () { this.dataset.touched = '1'; });
  $('ex-cancel').addEventListener('click', exportCancel);
  qsa('[data-close]').forEach(function (b) {
    b.addEventListener('click', function () { closeModal(b.closest('.modal').id); });
  });
  qsa('.modal').forEach(function (m) {
    m.addEventListener('mousedown', function (e) { if (e.target === m) closeModal(m.id); });
  });
  $('btn-help').addEventListener('click', function () { openModal('modal-help'); });

  bindTimeline();
  window.addEventListener('resize', function () { paintTimeline(); });
}

function bindTimeline() {
  var cv = $('tl');
  function localX(e) {
    var r = cv.getBoundingClientRect();
    return e.clientX - r.left;
  }
  function scrubTo(e) {
    var L = tlLayout();
    seek(xToTime(localX(e), L), true);
  }
  cv.addEventListener('pointerdown', function (e) {
    if (e.button !== 0 || e.altKey) return;   // 中键 / Alt+左键 是平移，别抢
    cv.setPointerCapture(e.pointerId);
    S.scrubbing = true;
    cv.classList.add('scrubbing');
    pause();
    scrubTo(e);
    cv.focus();
  });
  cv.addEventListener('pointermove', function (e) {
    var L = tlLayout();
    var x = clamp(localX(e), L.x0, L.x1);
    var y = e.clientY - cv.getBoundingClientRect().top;
    var li = Math.floor((y - L.rowTop) / (L.rowH + TL.rowGap));
    if (li >= 0 && li < L.lines.length) S.hover = { x: x, line: li };
    else S.hover = null;
    if (S.scrubbing) scrubTo(e);
    else scheduleTimelinePaint();
  });
  cv.addEventListener('pointerleave', function () { S.hover = null; scheduleTimelinePaint(); });
  cv.addEventListener('pointerup', function (e) {
    S.scrubbing = false;
    S.panning = false;
    cv.classList.remove('scrubbing');
    try { cv.releasePointerCapture(e.pointerId); } catch (err) { /* 已释放 */ }
  });

  /* 滚轮 = 以指针位置为锚点缩放。这是专业时间轴的肌肉记忆：
     想看哪一段，就把指针放上去往上滚。 */
  cv.addEventListener('wheel', function (e) {
    e.preventDefault();
    var L = tlLayout();
    var x = clamp(localX(e), L.x0, L.x1);
    var anchor = xToTime(x, L);
    var factor = e.deltaY < 0 ? 1.18 : 1 / 1.18;
    var z = S.view.zoom * factor;
    if (z <= 1.0001) { S.view.zoom = 1; S.view.start = 0; }
    else zoomTo(z, anchor);
    paintTimeline();
    paintZoom();
  }, { passive: false });

  // 中键或 Alt+左键拖动 = 平移视野（不改变时间）
  cv.addEventListener('pointerdown', function (e) {
    if (e.button === 1 || (e.button === 0 && e.altKey)) {
      S.panning = { x: e.clientX, start: S.view.start };
      cv.setPointerCapture(e.pointerId);
      cv.classList.add('panning');
      e.preventDefault();
    }
  }, true);   // 捕获阶段先于刷洗处理拿事件
  cv.addEventListener('pointermove', function (e) {
    if (S.panning) {
      var L = tlLayout();
      var perPx = viewSpan() / Math.max(1, L.x1 - L.x0);
      S.view.start = S.panning.start - (e.clientX - S.panning.x) * perPx;
      clampView();
      paintTimeline();
      paintZoom();
      e.preventDefault();
    }
  });
  cv.addEventListener('dblclick', function () { resetZoom(); });

  cv.addEventListener('keydown', function (e) {
    var f = 1 / S.fps;
    if (e.key === 'ArrowLeft') { pause(); seek(S.time - (e.shiftKey ? 1 : f), true); e.preventDefault(); }
    else if (e.key === 'ArrowRight') { pause(); seek(S.time + (e.shiftKey ? 1 : f), true); e.preventDefault(); }
    else if (e.key === ' ') { if (S.playing) pause(); else play(); e.preventDefault(); }
    else if (e.key === '+' || e.key === '=') { nudgeZoom(1.5); e.preventDefault(); }
    else if (e.key === '-' || e.key === '_') { nudgeZoom(1 / 1.5); e.preventDefault(); }
    // 缩放后键盘也要能横移视野，否则放大完就只能在窗口里看一小段
    else if (e.key === 'PageUp') { panView(-1); e.preventDefault(); }
    else if (e.key === 'PageDown') { panView(1); e.preventDefault(); }
    else if (e.key === '0') { resetZoom(); e.preventDefault(); }
  });
}

function resetZoom() {
  S.view = { zoom: 1, start: 0 };
  paintTimeline();
  paintZoom();
  scheduleFilmstrip();
}

/* 缩放倍率显示在 transport 上。没有它用户不知道画面为什么只显示了一小段，
   也没发现双击能复位。zoom=1 时留空，免得白占位置。 */
function paintZoom() {
  var el = $('tl-zoom');
  if (!el) return;
  var z = S.view.zoom;
  el.textContent = z > 1.001 ? '×' + (z < 10 ? z.toFixed(1) : Math.round(z)) : '';
  var zin = $('btn-zoom-in'), zout = $('btn-zoom-out');
  if (zin) zin.disabled = z >= VIEW_MAX_ZOOM - 1e-9;
  if (zout) zout.disabled = z <= 1.001;
}

function useTemplate() {
  S.spec = {
    name: '新谱面', charter: 'rpechart', bpm: 150, last_beat: 64, duration: 25.6,
    lines: [{ name: '主线', y: -200 }, { name: '副线', y: 180 }],
    sections: [{ start: 0, end: 32, role: 'N', density: 0.5 },
               { start: 32, end: 64, role: 'B', density: 2.0 }],
    out: 'build/web_demo.json'
  };
  renderForm();
  mirrorSpecToJson();
  $('doc-name').value = S.spec.name;
  markDirty();
  switchTab('form');
  toast('已插入模板 · 点「生成」看结果', 'ok');
}

/* ───────────────────────────── 快捷键 ───────────────────────────── */

function globalKeys(e) {
  var tag = (e.target && e.target.tagName) || '';
  var typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
  if (e.key === 'Escape') {
    qsa('.modal').forEach(function (m) { if (!m.hidden) closeModal(m.id); });
    return;
  }
  // 浮层打开时快捷键只允许 ? 关闭它，不能穿透到后面的画布/表单。
  var modalOpen = document.querySelector('.modal:not([hidden])');
  if (modalOpen) {
    // 浮层里的输入框可能真的要输入 ?，别把它当快捷键。
    if (!typing && e.key === '?') { closeModal(modalOpen.id); e.preventDefault(); }
    return;
  }
  // 画布 keydown 已经 preventDefault 的事件别再处理一次：否则空格会
  // play() 后立刻 pause()，方向键一次走两帧。
  if (e.defaultPrevented) return;
  if (typing && !(e.ctrlKey && e.key === 'Enter')) return;
  // 焦点在按钮上时，空格/回车是「按这个按钮」，不是全局播放。
  if (tag === 'BUTTON' && (e.key === ' ' || e.key === 'Enter')) return;
  if (e.ctrlKey && e.key === 'Enter') { e.preventDefault(); regenerate(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  var f = 1 / S.fps;
  switch (e.key) {
    case ' ': if (S.playing) pause(); else play(); e.preventDefault(); break;
    case 'ArrowLeft': pause(); seek(S.time - (e.shiftKey ? 1 : f), true); e.preventDefault(); break;
    case 'ArrowRight': pause(); seek(S.time + (e.shiftKey ? 1 : f), true); e.preventDefault(); break;
    case 'j': case 'J': pause(); seek(S.time - 1, true); e.preventDefault(); break;
    case 'l': case 'L': pause(); seek(S.time + 1, true); e.preventDefault(); break;
    case 'Home': pause(); seek(0, true); e.preventDefault(); break;
    case 'End': pause(); seek(tlDuration(), true); e.preventDefault(); break;
    case 'g': case 'G': regenerate(); break;
    case 'e': case 'E': exportOpen(); break;
    case 'h': case 'H': $('hud').checked = !$('hud').checked; $('hud').dispatchEvent(new Event('change')); break;
    case 'f': case 'F': S.filmKey = ''; loadFilmstrip(); break;
    case '+': case '=': nudgeZoom(1.5); e.preventDefault(); break;
    case '-': case '_': nudgeZoom(1 / 1.5); e.preventDefault(); break;
    case '0': resetZoom(); e.preventDefault(); break;
    case 'PageUp': panView(-1); e.preventDefault(); break;
    case 'PageDown': panView(1); e.preventDefault(); break;
    case '?': openModal('modal-help'); break;
    default: break;
  }
}

/* ───────────────────────────── 启动 ───────────────────────────── */

async function boot() {
  try {
    await loadCapabilities();
    await loadSpec();
    var stats = await api('/api/stats');
    applyStats(stats);
    await loadTimeline();
    await refreshChartText();
    $('boot').hidden = true;
    $('app').hidden = false;
    paintTimeline();
    showFrame(0, { force: true });
    loadFilmstrip();
    paintStatus();
    if (stats.error) {
      banner('启动时生成失败：' + stats.error, 'err');
    }
  } catch (e) {
    // 只留一条文案：先 textContent 再 append 同一句会显示两遍。
    var msg = '初始化失败：' + (e && e.message ? e.message : e);
    clear($('boot'));
    $('boot').appendChild(mk('p', 'boot-text', msg));
  }
}

window.addEventListener('DOMContentLoaded', function () {
  bind();
  S.fps = num($('fps') && $('fps').value, 60) || 60;
  document.addEventListener('keydown', globalKeys);
  boot();
});

})();
