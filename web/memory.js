'use strict';

// ---------------------------------------------------------------------------
// Memory & GPU tab: system memory by physical address and GPU memory, one bar
// per GB, plus GPU engine and CPU utilization. Uses the theme, color ramp and
// helpers from app.js (T, ramp, rgb, esc, ease, quad, C, S, $).
// ---------------------------------------------------------------------------
const GiB = 1073741824;
const MP = 30, MS = 24, MMAXH = 66, MSLAB = 5;    // world px: cell pitch, bar footprint, tallest bar, floor depth
const MLABEL_W = 78, MTITLE_H = 42, MLEGEND_H = 40, MGAP = 40;
const HISTORY = 60;                                // samples (seconds) kept for the chart
const MEM_USES = ['Process memory', 'Mapped files', 'Shared memory', 'Page tables', 'Paged pool',
  'Nonpaged pool', 'System', 'Session memory', 'File system metadata', 'Locked by apps (AWE)',
  'Locked by drivers', 'Kernel stacks'];

const mstage = $('mstage'), mcv = $('mcv'), mctx = mcv.getContext('2d'), mtip = $('mtip');
const ucv = $('ucv'), uctx = ucv.getContext('2d'), utip = $('utip');
const mem = {
  last: null,            // latest sample from the server
  ram: [], gpu: [],      // per GB: { h, target, d }, or null where there's no RAM
  hist: [],              // [{ g3d, cpu }] once a second, oldest first
  panels: [], hover: null, mouse: null, uhover: null,
};
let mK = 1, mdpr = 1, mW = 0, mH = 0, UC = {};

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
  const ram = m.ram, gpu = m.gpu;
  const g3d = gpu ? (gpu.engines.find(e => e[0] === '3D') || [])[1] : undefined;
  mem.hist.push({ g3d: g3d ?? null, cpu: m.cpu ?? null });
  if (mem.hist.length > HISTORY) mem.hist.shift();

  const ramList = !ram ? [] : ram.cells ? ram.cells.map(c => c && { ...c, v: Math.min(1, c.u + c.m) })
    : fillOrder(ram.total - ram.avail, ram.installed);
  const gpuList = gpu ? fillOrder(gpu.used, gpu.vram) : [];
  const relayout = ramList.length !== mem.ram.length || gpuList.length !== mem.gpu.length;
  mem.ram = cellsFrom(ramList, mem.ram);
  mem.gpu = cellsFrom(gpuList, mem.gpu);
  if (relayout) mlayout();
  renderMemStats();
  renderMeters();
  renderMemMsg();
  drawChart();
  if (mem.hover && mem.mouse) showMemTip();
}
sysListeners.push(onSys);

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

const mproj = (g, x, y, z = 0) => [g.ox + (x - y) * C * mK, g.oy + ((x + y) * S - z) * mK];
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
  const every = Math.max(1, Math.ceil(15 / (MP * S * mK))), n = g.cells.length;
  for (let r = 0; r < g.rows; r += every) {   // each row's range, just past its right end
    const [a, b] = mproj(g, g.cols * MP, r * MP + MP / 2);
    mtext(`${r * g.cols}–${Math.min(n, (r + 1) * g.cols)} GB`, a + 8, b, 11, T.muted);
  }
  const [title, sub] = panelTitle(g);
  const tw = mtext(title, g.left + 4, 24, 13, T.ink, 'left', 600);
  const room = mW - (g.left + 4 + tw + 8) - 8;
  mctx.font = `400 12px ${T.font}`;
  if (mctx.measureText(sub).width <= room) mtext(sub, g.left + 4 + tw + 8, 24, 12, T.muted);
  else mtext(sub.split(' · ').pop(), g.left + 4 + tw + 8, 24, 12, T.muted);   // just the size
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
    const a = (mx - g.ox) / (C * mK), b = (my - g.oy) / (S * mK);
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
  mem.hover = memAt(ev.clientX - r.left, ev.clientY - r.top);
  showMemTip();
});
mcv.addEventListener('mouseleave', () => { mem.hover = null; mem.mouse = null; mtip.hidden = true; });

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
// Utilization chart: GPU 3D and CPU over the last 60 seconds
// ---------------------------------------------------------------------------
const CH = { l: 34, r: 6, t: 8, b: 18 };

function readChartColors() {
  const cs = getComputedStyle(document.documentElement);
  UC = { g3d: cs.getPropertyValue('--c-3d').trim(), cpu: cs.getPropertyValue('--c-cpu').trim() };
  $('mswatches').innerHTML = [0.1, 0.3, 0.5, 0.7, 0.9].map(t => `<span style="background:${rgb(ramp(t))}"></span>`).join('');
}

function chartGeom() {
  const r = ucv.getBoundingClientRect();
  const pw = r.width - CH.l - CH.r, ph = r.height - CH.t - CH.b;
  return { r, pw, ph, X: i => CH.l + pw * i / (HISTORY - 1), Y: v => CH.t + ph * (1 - v / 100) };
}

function drawChart() {
  const { r, pw, X, Y } = chartGeom();
  if (!r.width || !r.height) return;
  const dpr = window.devicePixelRatio || 1;
  if (ucv.width !== Math.round(r.width * dpr) || ucv.height !== Math.round(r.height * dpr)) {
    ucv.width = Math.round(r.width * dpr);
    ucv.height = Math.round(r.height * dpr);
  }
  const g = uctx;
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, r.width, r.height);
  g.font = `10px ${T.font}`;
  g.textBaseline = 'middle';
  for (const v of [0, 50, 100]) {   // recessive grid
    g.beginPath();
    g.moveTo(CH.l, Y(v));
    g.lineTo(CH.l + pw, Y(v));
    g.strokeStyle = T.grid;
    g.lineWidth = 1;
    g.stroke();
    g.fillStyle = T.muted;
    g.textAlign = 'right';
    g.fillText(`${v}%`, CH.l - 6, Y(v));
  }
  g.textAlign = 'left';
  g.fillText('60 s ago', CH.l, r.height - 6);
  g.textAlign = 'right';
  g.fillText('now', CH.l + pw, r.height - 6);

  const n = mem.hist.length, start = HISTORY - n;
  for (const key of ['cpu', 'g3d']) {
    g.beginPath();
    let pen = false;
    mem.hist.forEach((h, i) => {
      if (h[key] == null) { pen = false; return; }
      const x = X(start + i), y = Y(Math.min(100, h[key]));
      if (pen) g.lineTo(x, y); else g.moveTo(x, y);
      pen = true;
    });
    g.strokeStyle = UC[key];
    g.lineWidth = 2;
    g.lineJoin = 'round';
    g.stroke();
  }
  const i = mem.uhover;
  if (i != null && i < n) {   // crosshair and markers at the hovered second
    const x = X(start + i);
    g.beginPath();
    g.moveTo(x, CH.t);
    g.lineTo(x, Y(0));
    g.strokeStyle = T.lead;
    g.lineWidth = 1;
    g.stroke();
    for (const key of ['cpu', 'g3d']) {
      const v = mem.hist[i][key];
      if (v == null) continue;
      g.beginPath();
      g.arc(x, Y(Math.min(100, v)), 4, 0, Math.PI * 2);
      g.fillStyle = UC[key];
      g.fill();
      g.strokeStyle = T.bg;
      g.lineWidth = 2;
      g.stroke();
    }
  }
}

ucv.addEventListener('mousemove', ev => {
  const n = mem.hist.length;
  if (!n) return;
  const { r, pw } = chartGeom();
  const slot = Math.round((ev.clientX - r.left - CH.l) / pw * (HISTORY - 1));
  const i = Math.max(0, Math.min(n - 1, slot - (HISTORY - n)));
  mem.uhover = i;
  drawChart();
  const h = mem.hist[i], ago = n - 1 - i;
  const val = v => (v == null ? '–' : pct(v / 100));
  utip.innerHTML = `<div class="head">${ago ? `${ago} s ago` : 'Now'}</div>` +
    `<div><i class="ln g3d"></i> GPU 3D ${val(h.g3d)}</div><div><i class="ln cpu"></i> CPU ${val(h.cpu)}</div>`;
  utip.hidden = false;
  const x = ev.clientX - r.left;
  utip.style.left = (x > r.width / 2 ? x - utip.offsetWidth - 12 : x + 12) + 'px';
  utip.style.top = '4px';
});
ucv.addEventListener('mouseleave', () => { mem.uhover = null; utip.hidden = true; drawChart(); });

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------
readChartColors();
darkQuery.addEventListener('change', () => { readChartColors(); drawChart(); });
new ResizeObserver(mlayout).observe(mstage);
new ResizeObserver(drawChart).observe(ucv);
requestAnimationFrame(mframe);
