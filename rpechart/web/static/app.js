/* rpechart web —— 参数 → 谱面 → 画面。无构建步骤，直接跑。 */
'use strict';

const $ = (id) => document.getElementById(id);
let SPEC = {};          // 当前 spec（quick 面板与 textarea 的双向真值）
let DURATION = 60;      // 预览时间轴长度（秒）
let playing = false;
let rafId = null;

const ROLES = ['N', 'R', 'B', 'RB'];

async function api(path, opts) {
  const res = await fetch(path, opts);
  const ctype = res.headers.get('content-type') || '';
  if (ctype.includes('application/json')) {
    const data = await res.json();
    if (!res.ok || data.ok === false) throw new Error(data.error || ('HTTP ' + res.status));
    return data;
  }
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return res;
}

function setSpecToTextarea() {
  $('spec').value = JSON.stringify(SPEC, null, 2);
}

/* ---------------- quick 面板 ---------------- */
function field(label, value, onChange, type) {
  const wrap = document.createElement('label');
  wrap.textContent = label;
  const input = document.createElement('input');
  input.type = type || 'text';
  if (type === 'number') input.step = 'any';
  input.value = value;
  input.addEventListener('change', () => onChange(input.type === 'number' ? Number(input.value) : input.value));
  wrap.appendChild(input);
  return wrap;
}

function renderQuick() {
  const box = $('quick');
  box.innerHTML = '';
  box.appendChild(field('曲名 name', SPEC.name || '', (v) => { SPEC.name = v; setSpecToTextarea(); }));
  box.appendChild(field('BPM', SPEC.bpm || 120, (v) => { SPEC.bpm = v; setSpecToTextarea(); }, 'number'));
  box.appendChild(field('末拍 last_beat', SPEC.last_beat || 0, (v) => { SPEC.last_beat = v; setSpecToTextarea(); }, 'number'));
  box.appendChild(field('时长 duration(s)', SPEC.duration || 0, (v) => { SPEC.duration = v; setSpecToTextarea(); }, 'number'));

  const lines = SPEC.lines = SPEC.lines || [];
  lines.forEach((ln, i) => {
    box.appendChild(field('线 ' + (i + 1) + ' 名', ln.name || '', (v) => { ln.name = v; setSpecToTextarea(); }));
    box.appendChild(field('线 ' + (i + 1) + ' y', ln.y != null ? ln.y : 0, (v) => { ln.y = v; setSpecToTextarea(); }, 'number'));
  });

  const secs = SPEC.sections = SPEC.sections || [];
  secs.forEach((s, i) => {
    box.appendChild(field('段 ' + (i + 1) + ' 起拍', s.start || 0, (v) => { s.start = v; setSpecToTextarea(); }, 'number'));
    box.appendChild(field('段 ' + (i + 1) + ' 止拍', s.end || 0, (v) => { s.end = v; setSpecToTextarea(); }, 'number'));
    const role = document.createElement('label');
    role.textContent = '段 ' + (i + 1) + ' 角色';
    const sel = document.createElement('select');
    ROLES.forEach((r) => {
      const o = document.createElement('option');
      o.value = r; o.textContent = r;
      if (s.role === r) o.selected = true;
      sel.appendChild(o);
    });
    sel.addEventListener('change', () => { s.role = sel.value; setSpecToTextarea(); });
    role.appendChild(sel);
    box.appendChild(role);
    box.appendChild(field('段 ' + (i + 1) + ' 密度', s.density != null ? s.density : 1, (v) => { s.density = v; setSpecToTextarea(); }, 'number'));
  });
}

/* ---------------- 观察面板 ---------------- */
function renderSummary(data) {
  const box = $('summary');
  box.innerHTML = '';
  const cards = [
    ['物量', data.notes],
    ['判定线', data.lines],
    ['BPM', data.bpm],
    ['时长(s)', data.duration != null ? data.duration.toFixed(2) : '—'],
    ['错误', (data.report && data.report.counts.error) || 0],
    ['警告', (data.report && data.report.counts.warning) || 0],
  ];
  cards.forEach(([k, v]) => {
    const d = document.createElement('div');
    d.className = 'card';
    const b = document.createElement('b'); b.textContent = v;
    const s = document.createElement('span'); s.textContent = k;
    d.appendChild(b); d.appendChild(s);
    box.appendChild(d);
  });
}

function renderReport(report) {
  const box = $('report');
  box.innerHTML = '';
  const issues = (report && report.issues) || [];
  if (!issues.length) {
    box.innerHTML = '<div class="issue"><span class="sev caution">干净</span><span>没有校验问题。</span></div>';
    return;
  }
  const order = { ERROR: 0, WARNING: 1, CAUTION: 2 };
  issues.slice().sort((a, b) => (order[a.severity] ?? 9) - (order[b.severity] ?? 9)).forEach((it) => {
    const row = document.createElement('div');
    row.className = 'issue';
    const sev = document.createElement('span');
    sev.className = 'sev ' + it.severity.toLowerCase();
    sev.textContent = it.severity;
    const code = document.createElement('span');
    code.className = 'code';
    code.textContent = it.code + (it.line != null ? (' @L' + it.line) : '');
    const msg = document.createElement('span');
    msg.textContent = it.message;
    row.appendChild(sev); row.appendChild(code); row.appendChild(msg);
    box.appendChild(row);
  });
}

function renderLines(perLine) {
  const t = $('lines');
  t.innerHTML = '<tr><th>#</th><th>名称</th><th>物量</th><th>类型</th></tr>';
  (perLine || []).forEach((ln) => {
    const tr = document.createElement('tr');
    const kinds = Object.entries(ln.kinds || {}).map(([k, v]) => k + ' ' + v).join(' / ');
    tr.innerHTML = '<td>' + (ln.index + 1) + '</td><td>' + (ln.name || '') + '</td><td>' +
      ln.notes + '</td><td>' + kinds + '</td>';
    t.appendChild(tr);
  });
}

function applyStats(data) {
  renderSummary(data);
  renderReport(data.report);
  renderLines(data.per_line);
  if (data.duration) {
    DURATION = data.duration;
    $('time').max = DURATION.toFixed(3);
  }
  $('meta').textContent = '《' + data.name + '》 ' + data.lines + ' 线 / ' + data.notes +
    ' 音符 · ' + (data.chart_path || '');
  if (data.error) {
    $('spec-err').textContent = data.error;
  } else {
    $('spec-err').textContent = '';
  }
}

/* ---------------- 预览 ---------------- */
function sizeParts() {
  const [w, h] = $('size').value.split('x').map(Number);
  return [w, h];
}

let previewSeq = 0;
async function drawFrame(t) {
  const [w, h] = sizeParts();
  const hud = $('hud').checked ? 1 : 0;
  const seq = ++previewSeq;
  const res = await fetch('/api/preview?t=' + t.toFixed(4) + '&w=' + w + '&h=' + h + '&hud=' + hud);
  if (seq !== previewSeq) return;                  // 丢弃过期帧
  if (!res.ok) {
    let msg = 'HTTP ' + res.status;
    try { msg = (await res.json()).error || msg; } catch (e) {}
    $('spec-err').textContent = msg;
    return;
  }
  const blob = await res.blob();
  if (seq !== previewSeq) return;
  const url = URL.createObjectURL(blob);
  const img = $('frame');
  const old = img.dataset.url;
  img.src = url;
  img.dataset.url = url;
  if (old) URL.revokeObjectURL(old);
  $('time-label').textContent = t.toFixed(3) + 's';
}

function play() {
  if (playing) return;
  playing = true;
  $('btn-play').textContent = '⏸ 暂停';
  let last = performance.now();
  const tick = (now) => {
    if (!playing) return;
    let t = Number($('time').value) + (now - last) / 1000;
    last = now;
    if (t > DURATION) t = 0;
    $('time').value = t;
    drawFrame(t).catch((e) => { $('spec-err').textContent = String(e.message || e); });
    rafId = requestAnimationFrame(tick);
  };
  rafId = requestAnimationFrame(tick);
}

function pause() {
  playing = false;
  if (rafId) cancelAnimationFrame(rafId);
  rafId = null;
  $('btn-play').textContent = '▶ 播放';
}

/* ---------------- 动作 ---------------- */
async function loadSpec() {
  const data = await api('/api/spec');
  SPEC = data.spec || {};
  renderQuick();
  setSpecToTextarea();
}

async function regenerate() {
  let body;
  try {
    body = JSON.parse($('spec').value);
  } catch (e) {
    $('spec-err').textContent = 'JSON 语法错误：' + e.message;
    return;
  }
  $('spec-err').textContent = '生成中…';
  try {
    const data = await api('/api/spec', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    SPEC = body;
    renderQuick();
    applyStats(data);
    await refreshChartText();
    await drawFrame(Number($('time').value) || 0);
  } catch (e) {
    $('spec-err').textContent = String(e.message || e);
  }
}

async function refreshChartText() {
  try {
    const res = await fetch('/api/chart');
    $('chart').textContent = await res.text();
  } catch (e) { /* 忽略 */ }
}

/* ---------------- 绑定 ---------------- */
$('btn-gen').addEventListener('click', regenerate);
$('btn-apply').addEventListener('click', regenerate);
$('btn-reload').addEventListener('click', loadSpec);
$('btn-template').addEventListener('click', () => {
  SPEC = {
    name: '新谱面', bpm: 150, last_beat: 64, duration: 25.6,
    lines: [{ name: '主线', y: -200 }, { name: '副线', y: 180 }],
    sections: [{ start: 0, end: 32, role: 'N', density: 0.5 },
               { start: 32, end: 64, role: 'B', density: 2.0 }],
    out: 'build/web_demo.json',
  };
  renderQuick(); setSpecToTextarea();
});
$('btn-play').addEventListener('click', () => (playing ? pause() : play()));
$('time').addEventListener('input', () => { pause(); drawFrame(Number($('time').value)); });
$('hud').addEventListener('change', () => drawFrame(Number($('time').value)));
$('size').addEventListener('change', () => drawFrame(Number($('time').value)));

(async function boot() {
  try {
    await loadSpec();
    const stats = await api('/api/stats');
    applyStats(stats);
    await refreshChartText();
    await drawFrame(0);
  } catch (e) {
    $('spec-err').textContent = '初始化失败：' + String(e.message || e);
  }
})();
