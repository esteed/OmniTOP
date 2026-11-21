'use strict';

// ---------------------------------------------------------------------------
// Memory & GPU tab: system memory by physical address and GPU memory, one bar
// per GB, plus GPU engine and CPU utilization. Uses the theme, color ramp and
// helpers from app.js (T, ramp, rgb, esc, ease, quad, C, S, $).
// ---------------------------------------------------------------------------
const GiB = 1073741824;
const MP = 30, MS = 24, MMAXH = 66, MSLAB = 5;    // world px: cell pitch, bar footprint, tallest bar, floor depth
const MLABEL_W = 78, MTITLE_H = 42, MLEGEND_H = 40, MGAP = 40;
const HISTORY_S = 60;                              // seconds of samples kept for the charts
const M_INTERVALS = [50, 100, 250, 500, 1000];     // ms: 20, 10, 4, 2, 1 samples a second, as on the CPU tab
const M_TEXT_MS = 250;                             // numbers and lists redraw at most 4 times a second
const MEM_USES = ['Process memory', 'Mapped files', 'Shared memory', 'Page tables', 'Paged pool',
  'Nonpaged pool', 'System', 'Session memory', 'File system metadata', 'Locked by apps (AWE)',
  'Locked by drivers', 'Kernel stacks'];

const mstage = $('mstage'), mcv = $('mcv'), mctx = mcv.getContext('2d'), mtip = $('mtip');
const mem = {
  last: null,            // latest sample from the server
  ram: [], gpu: [],      // per GB: { h, target, d }, or null where there's no RAM
  hist: [],              // [{ t, g3d, cpu, commit, ram }] for the last HISTORY_S seconds, oldest first
  interval: 1000,        // ms between samples, as the server reports it
  restored: false,       // the rate chosen last time has been put back
  textAt: 0,             // when the numbers and lists last redrew
  commitLimit: 0,        // bytes that can be committed: RAM plus page file
  panels: [], hover: null, mouse: null,
};
let mK = 1, mdpr = 1, mW = 0, mH = 0, UC = {};
// Zoom and pan on top of the fitted layout: screen = layout * z + (x, y)
const mview = { z: 1, x: 0, y: 0 };
const MAX_MZOOM = 12, MIN_MZOOM = 0.5;
const vx = sx => sx * mview.z + mview.x, vy = sy => sy * mview.z + mview.y;

function fmtGB(b) {
  const g = b / GiB;
  return g >= 100 ? g.toFixed(0) : g >= 10 ? g.toFixed(1) : g.toFixed(2);
}
const fmtSize = b => (b >= GiB ? `${fmtGB(b)} GB` : `${Math.round(b / 1048576)} MB`);
const capacity = b => String(Math.round(b / GiB));   // installed sizes: 32, 128, 24
const pct = f => `${Math.round(f * 100)}%`;   // f is a fraction

// ---------------------------------------------------------------------------
// Samples
// ---------------------------------------------------------------------------
// One cell per GB filled in order, for memory whose layout isn't known.
function fillOrder(used, size) {
  const n = Math.max(1, Math.round(size / GiB)), u = used / GiB;
  return Array.from({ length: n }, (_, i) => ({ v: Math.max(0, Math.min(1, u - i)), fill: true }));
}

function cellsFrom(list, old) {
  return list.map((d, i) => d && { h: old[i] ? old[i].h : 0, target: d.v * MMAXH, d });
}

function onSys(m) {
  mem.last = m;
  syncRate(m.iv);
  const ram = m.ram, gpu = m.gpu;
  const g3d = gpu ? (gpu.engines.find(e => e[0] === '3D') || [])[1] : undefined;
  const t = m.t ?? performance.now() / 1000;
  const prev = mem.hist[mem.hist.length - 1];
  if (prev && (t < prev.t || t - prev.t > HISTORY_S)) mem.hist = [];   // the server restarted, or a long gap
  mem.hist.push({ t, g3d: g3d ?? null, cpu: m.cpu ?? null,
                  commit: m.commit ? m.commit.used : null, ram: ram ? ram.total - ram.avail : null });
  while (mem.hist.length && mem.hist[0].t < t - HISTORY_S) mem.hist.shift();
  mem.commitLimit = m.commit ? m.commit.limit : 0;

  const ramList = !ram ? [] : ram.cells ? ram.cells.map(c => c && { ...c, v: Math.min(1, c.u + c.m) })
    : fillOrder(ram.total - ram.avail, ram.installed);
  const gpuList = gpu ? fillOrder(gpu.used, gpu.vram) : [];
  const relayout = ramList.length !== mem.ram.length || gpuList.length !== mem.gpu.length;
  mem.ram = cellsFrom(ramList, mem.ram);
  mem.gpu = cellsFrom(gpuList, mem.gpu);
  // Lay out on a change in size, or if there's no layout yet (the tab was hidden at the first sample).
  if (relayout || !mem.panels.length) mlayout();
  // The grids draw the newest bars (each sample brings new cell objects).
  for (const g of mem.panels) g.cells = g.kind === 'ram' ? mem.ram : mem.gpu;
  // Bars and charts follow every sample; text redraws less often so it stays readable.
  const now = performance.now();
  if (relayout || now - mem.textAt >= M_TEXT_MS) {
    mem.textAt = now;
    renderMemStats();
    renderMeters();
    renderMemMsg();
    if (mem.hover && mem.mouse) showMemTip();
  }
  drawCharts();
}
sysListeners.push(onSys);

// ---------------------------------------------------------------------------
// Sample rate: the same choices as the CPU tab, kept between runs
// ---------------------------------------------------------------------------
function syncRate(iv) {
  if (!M_INTERVALS.includes(iv)) return;
  mem.interval = iv;
  $('mctl-rate').value = String(iv);
  if (!mem.restored) {   // put back the rate chosen last time
    mem.restored = true;
    const want = loadPref('mem.interval');
    if (M_INTERVALS.includes(want) && want !== iv) setMemRate(want);
  }
}

async function setMemRate(ms) {
  if (!M_INTERVALS.includes(ms)) return;
  savePref('mem.interval', ms);
  $('mctl-rate').value = String(ms);
  try {
    const res = await fetch('/api/sys', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ interval: ms }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
  } catch {
    $('mctl-rate').value = String(mem.interval);   // the samples that arrive say what's in use
  }
}

$('mctl-rate').addEventListener('change', ev => setMemRate(Number(ev.target.value)));
document.addEventListener('keydown', ev => {
  if (activeTab !== 'mem' || ev.ctrlKey || ev.altKey || ev.metaKey) return;
  if (ev.target.closest && ev.target.closest('input, select, textarea')) return;
  const i = M_INTERVALS.indexOf(mem.interval);
  if (ev.key === 'ArrowUp' && i > 0) setMemRate(M_INTERVALS[i - 1]);           // faster
  else if (ev.key === 'ArrowDown' && i < M_INTERVALS.length - 1) setMemRate(M_INTERVALS[i + 1]);   // slower
  else return;
  ev.preventDefault();
});

// ---------------------------------------------------------------------------
// Layout and drawing of the two grids
// ---------------------------------------------------------------------------
function mlayout() {
  const r = mstage.getBoundingClientRect();
  if (!r.width || !r.height) return;
  mW = r.width;
  mH = r.height;
  mdpr = window.devicePixelRatio || 1;
  mcv.width = Math.round(mW * mdpr);
  mcv.height = Math.round(mH * mdpr);
  mcv.style.width = mW + 'px';
  mcv.style.height = mH + 'px';
  const panels = [];
  if (mem.ram.length) {
    const cols = mem.ram.length > 64 ? 16 : 8;
    panels.push({ kind: 'ram', cells: mem.ram, cols, rows: Math.ceil(mem.ram.length / cols) });
  }
  if (mem.gpu.length) panels.push({ kind: 'gpu', cells: mem.gpu, cols: 8, rows: Math.ceil(mem.gpu.length / 8) });
  for (const g of panels) {
    const span = (g.cols + g.rows) * MP;
    g.w = span * C;
    g.h = span * S + MMAXH + MSLAB;
  }
  mem.panels = panels;
  if (!panels.length) return;
  const sumW = panels.reduce((a, g) => a + g.w, 0), maxH = Math.max(...panels.map(g => g.h));
  const fixedW = 32 + panels.length * MLABEL_W + (panels.length - 1) * MGAP;
  const availH = mH - MTITLE_H - MLEGEND_H - 16;
  mK = Math.max(0.2, Math.min(2.4, (mW - fixedW) / sumW, availH / maxH));
  let x = (mW - (sumW * mK + fixedW - 32)) / 2;
  const top = MTITLE_H + 8 + Math.max(0, (availH - maxH * mK) / 2);
  for (const g of panels) {   // row labels go to the right of each grid, where no bar can cover them
    g.left = x;
    g.ox = x + g.rows * MP * C * mK;   // screen position of the grid's back corner
    g.oy = top + MMAXH * mK;
    x += g.w * mK + MLABEL_W + MGAP;
  }
}

const mproj = (g, x, y, z = 0) => [vx(g.ox + (x - y) * C * mK), vy(g.oy + ((x + y) * S - z) * mK)];
function mpath(g, pts) {
  mctx.beginPath();
  pts.forEach(([x, y, z], i) => {
    const [a, b] = mproj(g, x, y, z || 0);
    if (i) mctx.lineTo(a, b); else mctx.moveTo(a, b);
  });
  mctx.closePath();
}
function mfill(g, pts, color) { mpath(g, pts); mctx.fillStyle = color; mctx.fill(); }
function mbox(g, x, y, s, h, col) {
  mfill(g, [[x, y + s, 0], [x + s, y + s, 0], [x + s, y + s, h], [x, y + s, h]], rgb(col, T.shadeL));
  mfill(g, [[x + s, y, 0], [x + s, y + s, 0], [x + s, y + s, h], [x + s, y, h]], rgb(col, T.shadeR));
  mfill(g, quad(x, y, s, s, h), rgb(col));
}
function mtext(str, x, y, size, color, align = 'left', weight = 400) {
  mctx.font = `${weight} ${size}px ${T.font}`;
  mctx.textAlign = align;
  mctx.textBaseline = 'middle';
  mctx.fillStyle = color;
  mctx.fillText(str, x, y);
  return mctx.measureText(str).width;
}

const CELL_OFF = (MP - MS) / 2;
const cellXY = (g, i) => [(i % g.cols) * MP + CELL_OFF, Math.floor(i / g.cols) * MP + CELL_OFF];

function panelTitle(g) {
  const m = mem.last;
  if (g.kind === 'ram') {
    const size = `${capacity(m.ram.installed)} GB`;
    return ['System memory', m.ram.cells ? `${size} · by physical address` : `${size} · filled in order`];
  }
  return ['GPU memory', `${m.gpu.name} · ${capacity(m.gpu.vram)} GB`];
}

function drawPanel(g) {
  const w = g.cols * MP, d = g.rows * MP;
  mfill(g, [[0, d, 0], [w, d, 0], [w, d, -MSLAB], [0, d, -MSLAB]], T.sideL);
  mfill(g, [[w, 0, 0], [w, d, 0], [w, d, -MSLAB], [w, 0, -MSLAB]], T.sideR);
  mfill(g, quad(0, 0, w, d), T.gap);
  g.cells.forEach((c, i) => {
    const [x, y] = cellXY(g, i);
    if (c) {
      mfill(g, quad(x, y, MS, MS), T.floor);
    } else {   // no RAM at this address
      mpath(g, quad(x + 1.5, y + 1.5, MS - 3, MS - 3));
      mctx.setLineDash([3, 3]);
      mctx.strokeStyle = T.muted;
      mctx.lineWidth = 1;
      mctx.stroke();
      mctx.setLineDash([]);
    }
  });
  g.cells.forEach((c, i) => {   // row by row is back to front
    if (!c || c.h <= 0.3) return;
    const [x, y] = cellXY(g, i);
    mbox(g, x, y, MS, c.h, ramp(c.h / MMAXH));
  });
  if (mem.hover && mem.hover.g === g) {
    const c = g.cells[mem.hover.i], [x, y] = cellXY(g, mem.hover.i);
    mpath(g, quad(x, y, MS, MS, c ? c.h : 0));
    mctx.strokeStyle = T.hl;
    mctx.lineWidth = 2;
    mctx.stroke();
  }
  // Label every row, or every few rows when they're drawn too close together
  const every = Math.max(1, Math.ceil(15 / (MP * S * mK * mview.z))), n = g.cells.length;
  for (let r = 0; r < g.rows; r += every) {   // each row's range, just past its right end
    const [a, b] = mproj(g, g.cols * MP, r * MP + MP / 2);
    mtext(`${r * g.cols}–${Math.min(n, (r + 1) * g.cols)} GB`, a + 8, b, 11, T.muted);
  }
  const [title, sub] = panelTitle(g);
  const tx = vx(g.left + 4), ty = vy(24);   // titles move with the grids
  const tw = mtext(title, tx, ty, 13, T.ink, 'left', 600);
  const room = mW - (tx + tw + 8) - 8;
  mctx.font = `400 12px ${T.font}`;
  if (mctx.measureText(sub).width <= room) mtext(sub, tx + tw + 8, ty, 12, T.muted);
  else mtext(sub.split(' · ').pop(), tx + tw + 8, ty, 12, T.muted);   // just the size
}

function mdraw() {
  mctx.setTransform(1, 0, 0, 1, 0, 0);
  mctx.clearRect(0, 0, mcv.width, mcv.height);
  mctx.setTransform(mdpr, 0, 0, mdpr, 0, 0);
  if (!mem.panels.length) {
    if (!mem.last) mtext('Waiting for the first sample…', mW / 2, mH / 2, 13, T.muted, 'center');
    return;
  }
  for (const g of mem.panels) drawPanel(g);
}

let mLast = performance.now();
function mframe(t) {
  const dt = Math.min(0.1, (t - mLast) / 1000);
  mLast = t;
  requestAnimationFrame(mframe);
  if (activeTab !== 'mem') return;
  if ((window.devicePixelRatio || 1) !== mdpr) mlayout();
  for (const list of [mem.ram, mem.gpu]) for (const c of list) if (c) c.h = ease(c.h, c.target, dt);
  mdraw();
}

// ---------------------------------------------------------------------------
// Hover
// ---------------------------------------------------------------------------
function memAt(mx, my) {
  for (const g of mem.panels) {
    let best = null;
    g.cells.forEach((c, i) => {   // bars first: the frontmost one under the pointer
      if (!c || c.h < 0.5) return;
      const [x, y] = cellXY(g, i);
      const x0 = mproj(g, x, y + MS)[0], x1 = mproj(g, x + MS, y)[0];
      const y0 = mproj(g, x, y, c.h)[1], y1 = mproj(g, x + MS, y + MS)[1];
      if (mx >= x0 && mx <= x1 && my >= y0 && my <= y1 && (!best || x + y > best.d)) best = { g, i, d: x + y };
    });
    if (best) return best;
    const sx = (mx - mview.x) / mview.z, sy = (my - mview.y) / mview.z;   // undo zoom and pan
    const a = (sx - g.ox) / (C * mK), b = (sy - g.oy) / (S * mK);
    const col = Math.floor((a + b) / 2 / MP), row = Math.floor((b - a) / 2 / MP);
    const i = row * g.cols + col;
    if (col >= 0 && col < g.cols && row >= 0 && i < g.cells.length) return { g, i };
  }
  return null;
}

function showMemTip() {
  const hv = mem.hover, m = mem.last;
  if (!hv || !m) { mtip.hidden = true; return; }
  const c = hv.g.cells[hv.i];
  let html;
  if (hv.g.kind === 'gpu') {
    html = `<div class="head">GPU memory · GB ${hv.i + 1} of ${hv.g.cells.length}</div>` +
      `<div>${pct(c.d.v)} in use</div>` +
      `<div class="faint">Filled in order: Windows doesn't report where in GPU memory data is stored</div>`;
  } else if (!c) {
    html = `<div class="head">${hv.i}–${hv.i + 1} GB</div>` +
      `<div class="faint">No RAM at this address. The range is reserved for hardware such as the graphics card.</div>`;
  } else if (c.d.fill) {
    html = `<div class="head">System memory · GB ${hv.i + 1} of ${hv.g.cells.length}</div>` +
      `<div>${pct(c.d.v)} in use</div>` +
      `<div class="faint">Filled in order: ${esc(m.ram.err || 'the address map is unavailable')}</div>`;
  } else {
    const d = c.d;
    const uses = d.t.map(([k, f]) => `${esc(MEM_USES[k] || 'Other')} ${pct(f)}`).join(' · ');
    html = `<div class="head">${hv.i}–${hv.i + 1} GB <span class="faint">physical address</span></div>` +
      `<div>${pct(d.u + d.m)} in use</div>` +
      `<div class="faint">Cached ${pct(d.c)} · Free ${pct(d.f)}${d.m >= 0.005 ? ` · Waiting to be written ${pct(d.m)}` : ''}</div>` +
      (uses ? `<div class="sub">${uses}</div>` : '') +
      (d.r < 0.995 ? `<div class="faint">Only ${pct(d.r)} of this GB is RAM; the rest is reserved for hardware</div>` : '');
  }
  mtip.innerHTML = html;
  mtip.hidden = false;
  const sr = mstage.getBoundingClientRect();
  let x = mem.mouse[0] - sr.left + 14, y = mem.mouse[1] - sr.top + 14;
  if (x + mtip.offsetWidth > sr.width - 8) x -= mtip.offsetWidth + 28;
  if (y + mtip.offsetHeight > sr.height - 8) y -= mtip.offsetHeight + 28;
  mtip.style.left = Math.max(4, x) + 'px';
  mtip.style.top = Math.max(4, y) + 'px';
}

mcv.addEventListener('mousemove', ev => {
  const r = mcv.getBoundingClientRect();
  mem.mouse = [ev.clientX, ev.clientY];
  mem.hover = mdrag ? null : memAt(ev.clientX - r.left, ev.clientY - r.top);
  showMemTip();
});
mcv.addEventListener('mouseleave', () => { mem.hover = null; mem.mouse = null; mtip.hidden = true; });

// ---------------------------------------------------------------------------
// Zoom with the mouse wheel, move by dragging, like the Network ports tab
// ---------------------------------------------------------------------------
function clampMemView() {   // keep at least a corner of the grids on screen
  const m = 60;
  mview.x = Math.min(mW - m, Math.max(m - mW * mview.z, mview.x));
  mview.y = Math.min(mH - m, Math.max(m - mH * mview.z, mview.y));
}

function resetMemView() {
  mview.z = 1;
  mview.x = mview.y = 0;
}

let mdrag = null;
mcv.addEventListener('wheel', ev => {
  ev.preventDefault();
  const r = mcv.getBoundingClientRect(), mx = ev.clientX - r.left, my = ev.clientY - r.top;
  const dy = ev.deltaMode === 1 ? ev.deltaY * 33 : ev.deltaMode === 2 ? ev.deltaY * mH : ev.deltaY;
  const z = Math.min(MAX_MZOOM, Math.max(MIN_MZOOM, mview.z * Math.pow(1.2, -dy / 100)));   // 1.2x per notch
  mview.x = mx - (mx - mview.x) * z / mview.z;   // keep the point under the mouse where it is
  mview.y = my - (my - mview.y) * z / mview.z;
  mview.z = z;
  clampMemView();
  if (mem.mouse) { mem.hover = memAt(mx, my); showMemTip(); }
}, { passive: false });
mcv.addEventListener('mousedown', ev => {
  if (ev.button !== 0) return;
  ev.preventDefault();
  mdrag = { x: ev.clientX, y: ev.clientY, ox: mview.x, oy: mview.y };
  mcv.classList.add('dragging');
  mem.hover = null;
  mtip.hidden = true;
});
window.addEventListener('mousemove', ev => {
  if (!mdrag) return;
  mview.x = mdrag.ox + ev.clientX - mdrag.x;
  mview.y = mdrag.oy + ev.clientY - mdrag.y;
  clampMemView();
});
window.addEventListener('mouseup', () => {
  if (!mdrag) return;
  mdrag = null;
  mcv.classList.remove('dragging');
});
$('mctl-zoom').addEventListener('click', resetMemView);

// ---------------------------------------------------------------------------
// Header numbers, side lists, notices
// ---------------------------------------------------------------------------
function renderMemStats() {
  const m = mem.last, h = mem.hist[mem.hist.length - 1];
  $('ms-ram').textContent = m.ram ? `${fmtGB(m.ram.total - m.ram.avail)} / ${capacity(m.ram.installed)} GB` : '–';
  $('ms-vram').textContent = m.gpu ? `${fmtGB(m.gpu.used)} / ${capacity(m.gpu.vram)} GB` : '–';
  const g3d = h.g3d != null ? pct(h.g3d / 100) : '–', cpu = h.cpu != null ? pct(h.cpu / 100) : '–';
  $('ms-3d').textContent = $('key-3d').textContent = g3d;
  $('ms-cpu').textContent = $('key-cpu').textContent = cpu;
  const c = m.commit;
  $('ms-commit').textContent = c ? `${fmtGB(c.used)} / ${capacity(c.limit)} GB` : '–';
  $('key-commit').textContent = c ? `${fmtGB(c.used)} GB` : '–';
  $('key-ramuse').textContent = m.ram ? `${fmtGB(m.ram.total - m.ram.avail)} GB` : '–';
  $('key-limit').textContent = c ? `${capacity(c.limit)} GB` : '–';
  $('ms-commit').parentElement.title = c ? `Peak since Windows started: ${fmtGB(c.peak)} GB` : '';
}

function meterRow(name, value, t) {
  t = Math.max(0, Math.min(1, t));
  return `<li><span class="n">${esc(name)}</span><span class="v">${value}</span>` +
    `<span class="m"><i style="width:${(t * 100).toFixed(1)}%;background:${rgb(ramp(t))}"></i></span></li>`;
}

function renderMeters() {
  const m = mem.last, gpu = m.gpu;
  const none = `<li class="empty">${esc(m.gpuErr || m.err || 'No GPU data')}</li>`;
  $('engines').innerHTML = gpu && gpu.engines.length
    ? gpu.engines.map(([k, v]) => meterRow(k, pct(v / 100), v / 100)).join('') : none;
  const rows = gpu ? gpu.procs.map(([name, b]) => meterRow(name, fmtSize(b), b / gpu.vram)) : [];
  if (gpu && gpu.unowned >= 1048576) {   // driver memory and surfaces shared between apps
    rows.push(meterRow('Not owned by one app', fmtSize(gpu.unowned), gpu.unowned / gpu.vram).replace('<li>', '<li class="other">'));
  }
  $('gprocs').innerHTML = rows.length ? rows.join('') : none;
}

function renderMemMsg() {
  const m = mem.last;
  let text = '';
  if (m.err) text = `Memory and GPU readings aren't available: ${m.err}`;
  else if (m.ram && !m.ram.cells && m.ram.err) text = `${m.ram.err} Showing how much memory is in use instead of where.`;
  $('mmsg').textContent = text;
  $('mmsg').hidden = !text;
}

// ---------------------------------------------------------------------------
// Line charts over the last 60 seconds: utilization, and committed memory
// ---------------------------------------------------------------------------
const CH = { l: 44, r: 6, t: 8, b: 18 };   // plot margins, px

function readChartColors() {
  const cs = getComputedStyle(document.documentElement);
  const v = n => cs.getPropertyValue(n).trim();
  UC = { g3d: v('--c-3d'), cpu: v('--c-cpu'), commit: v('--c-commit'), ram: v('--text-2') };
  $('mswatches').innerHTML = [0.1, 0.3, 0.5, 0.7, 0.9].map(t => `<span style="background:${rgb(ramp(t))}"></span>`).join('');
}

// cfg: series [{ key, label, cls }] (drawn in order, so the main one goes last),
// max() top of the scale, ticks() gridline values, axis(v) and fmt(v) labels,
// and optionally ref() -> { value, label } for a dashed reference line.
class LineChart {
  constructor(canvasId, tipId, cfg) {
    this.cv = $(canvasId);
    this.g = this.cv.getContext('2d');
    this.tip = $(tipId);
    this.cfg = cfg;
    this.hover = null;
    this.cv.addEventListener('mousemove', ev => this.onMove(ev));
    this.cv.addEventListener('mouseleave', () => { this.hover = null; this.tip.hidden = true; this.draw(); });
    new ResizeObserver(() => this.draw()).observe(this.cv);
  }

  // Samples sit by time: the newest at the right edge, HISTORY_S seconds back at the left.
  geom() {
    const r = this.cv.getBoundingClientRect(), max = this.cfg.max() || 1;
    const pw = r.width - CH.l - CH.r, ph = r.height - CH.t - CH.b;
    const h = mem.hist, now = h.length ? h[h.length - 1].t : 0;
    return { r, pw, now, X: t => CH.l + pw * (1 - (now - t) / HISTORY_S), Y: v => CH.t + ph * (1 - Math.min(v, max) / max) };
  }

  // The sample nearest a time, or -1.
  nearest(t) {
    const h = mem.hist;
    let lo = 0, hi = h.length - 1;
    if (hi < 0) return -1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (h[mid].t < t) lo = mid + 1; else hi = mid;
    }
    return lo > 0 && t - h[lo - 1].t < h[lo].t - t ? lo - 1 : lo;
  }

  draw() {
    const { r, pw, X, Y } = this.geom(), g = this.g, cfg = this.cfg;
    if (!r.width || !r.height) return;
    const dpr = window.devicePixelRatio || 1;
    if (this.cv.width !== Math.round(r.width * dpr) || this.cv.height !== Math.round(r.height * dpr)) {
      this.cv.width = Math.round(r.width * dpr);
      this.cv.height = Math.round(r.height * dpr);
    }
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, r.width, r.height);
    g.font = `10px ${T.font}`;
    g.textBaseline = 'middle';
    for (const v of cfg.ticks()) {   // recessive grid
      g.beginPath();
      g.moveTo(CH.l, Y(v));
      g.lineTo(CH.l + pw, Y(v));
      g.strokeStyle = T.grid;
      g.lineWidth = 1;
      g.stroke();
      g.fillStyle = T.muted;
      g.textAlign = 'right';
      g.fillText(cfg.axis(v), CH.l - 6, Y(v));
    }
    g.textAlign = 'left';
    g.fillText('60 s ago', CH.l, r.height - 6);
    g.textAlign = 'right';
    g.fillText('now', CH.l + pw, r.height - 6);

    const ref = cfg.ref && cfg.ref();
    if (ref) {
      g.beginPath();
      g.setLineDash([4, 3]);
      g.moveTo(CH.l, Y(ref.value));
      g.lineTo(CH.l + pw, Y(ref.value));
      g.strokeStyle = T.muted;
      g.lineWidth = 1.5;
      g.stroke();
      g.setLineDash([]);
    }

    const n = mem.hist.length;
    for (const s of cfg.series) {
      g.beginPath();
      let pen = false, prevT = -Infinity;
      mem.hist.forEach(h => {
        if (h[s.key] == null || h.t - prevT > 5) pen = false;   // gaps stay gaps
        prevT = h.t;
        if (h[s.key] == null) return;
        const x = X(h.t), y = Y(h[s.key]);
        if (pen) g.lineTo(x, y); else g.moveTo(x, y);
        pen = true;
      });
      g.strokeStyle = UC[s.key];
      g.lineWidth = 2;
      g.lineJoin = 'round';
      g.stroke();
    }
    const i = this.hover == null ? -1 : this.nearest(this.hover);
    if (i >= 0 && i < n) {   // crosshair and markers at the hovered sample
      const x = X(mem.hist[i].t);
      g.beginPath();
      g.moveTo(x, CH.t);
      g.lineTo(x, Y(0));
      g.strokeStyle = T.lead;
      g.lineWidth = 1;
      g.stroke();
      for (const s of cfg.series) {
        const v = mem.hist[i][s.key];
        if (v == null) continue;
        g.beginPath();
        g.arc(x, Y(v), 4, 0, Math.PI * 2);
        g.fillStyle = UC[s.key];
        g.fill();
        g.strokeStyle = T.bg;
        g.lineWidth = 2;
        g.stroke();
      }
    }
  }

  onMove(ev) {
    const n = mem.hist.length;
    if (!n) return;
    const { r, pw, now } = this.geom(), cfg = this.cfg;
    const frac = Math.max(0, Math.min(1, (ev.clientX - r.left - CH.l) / pw));
    const i = this.nearest(now - (1 - frac) * HISTORY_S);
    const h = mem.hist[i], ago = now - h.t;
    this.hover = h.t;   // a time, so the marker stays on its sample as new ones arrive
    this.draw();
    const rows = [...cfg.series].reverse().map(s =>
      `<div><i class="ln ${s.cls}"></i> ${s.label} ${h[s.key] == null ? '–' : cfg.fmt(h[s.key])}</div>`);
    const ref = cfg.ref && cfg.ref();
    const when = ago < mem.interval / 2000 ? 'Now' : ago < 10 ? `${ago.toFixed(1)} s ago` : `${Math.round(ago)} s ago`;
    this.tip.innerHTML = `<div class="head">${when}</div>${rows.join('')}` +
      (ref ? `<div class="faint">${ref.label}</div>` : '');
    this.tip.hidden = false;
    const x = ev.clientX - r.left;
    this.tip.style.left = (x > r.width / 2 ? x - this.tip.offsetWidth - 12 : x + 12) + 'px';
    this.tip.style.top = '4px';
  }
}

const utilChart = new LineChart('ucv', 'utip', {
  series: [{ key: 'cpu', label: 'CPU', cls: 'cpu' }, { key: 'g3d', label: 'GPU 3D', cls: 'g3d' }],
  max: () => 100,
  ticks: () => [0, 50, 100],
  axis: v => `${v}%`,
  fmt: v => `${Math.round(v)}%`,
});

// Committed memory: what programs have been promised, backed by RAM or the
// page file, against the limit (RAM plus page file). In use (RAM) for scale.
function commitScale() {
  const top = Math.max(mem.commitLimit || 0, ...mem.hist.map(h => Math.max(h.commit || 0, h.ram || 0)));
  const step = top > 64 * GiB ? 32 * GiB : 8 * GiB;
  return Math.max(step, Math.ceil(top * 1.02 / step) * step);
}
const commitChart = new LineChart('ccv', 'ctip', {
  series: [{ key: 'ram', label: 'In use (RAM)', cls: 'ram' }, { key: 'commit', label: 'Committed', cls: 'commit' }],
  max: commitScale,
  ticks: () => { const m = commitScale(); return [0, m / 2, m]; },
  axis: v => `${Math.round(v / GiB)} GB`,
  fmt: v => `${fmtGB(v)} GB`,
  ref: () => (mem.commitLimit ? { value: mem.commitLimit, label: `Limit ${capacity(mem.commitLimit)} GB` } : null),
});

function drawCharts() {
  utilChart.draw();
  commitChart.draw();
}

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------
readChartColors();
// The tab may open before a resize notice arrives: lay out then too.
tabListeners.push(name => { if (name === 'mem') { mlayout(); drawCharts(); } });
darkQuery.addEventListener('change', () => { readChartColors(); drawCharts(); });
new ResizeObserver(mlayout).observe(mstage);
requestAnimationFrame(mframe);
