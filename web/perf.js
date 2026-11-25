'use strict';

// ---------------------------------------------------------------------------
// CPU, disk & network tab: a port of CPU Graph 2.0 (github.com/esteed/CpuGraph).
// One row per machine (This PC, then each remote machine running cpugraph-agent)
// with three columns: a small graph per CPU thread, a strip per network adapter
// and a strip per disk. All graphs share one time axis. Every number is a
// 3-second average; the lines show the samples. Uses the theme and helpers from
// app.js (T, $, esc, loadPref, savePref, activeTab, perfListeners).
// ---------------------------------------------------------------------------
const P_INTERVALS = [50, 100, 250, 500, 1000];   // ms: 20, 10, 4, 2, 1 samples a second
const P_SPANS = [15, 30, 60, 120, 300];          // seconds (at 20 a second the graphs show a third)
const P_AVG = 3;                                 // seconds averaged for the amber line and every number
const P_CAP = 8192;                              // samples kept per machine: 409 s at 20 a second
const P_GRID = [1, 2, 3, 5, 10, 15, 20, 30, 60, 120, 300];
const PL = { top: 28, title: 24, rowGap: 12, margin: 12, colGap: 14, stripGap: 4, cellGap: 3,
             minBody: 44, cpuWeight: 0.8, aspect: 1.6 };

const pstage = $('pstage'), pcv = $('pcv'), pctx = pcv.getContext('2d'), ptip = $('ptip');
const pp = {
  meta: null, tEnd: 0, paused: false, started: performance.now(), plots: [], hover: null,
  span: 60, threads: true, avg: true, remotes: true, net: true, disk: true, onTop: false,
  restored: false, desktop: false,
};
const pm = new Map();   // machine id -> { v, cpu, nets, disks, info, lastLink }
let PC = {}, pW = 0, pH = 0, pdpr = 1, pQueued = false;

// A ring of samples: time plus a fixed number of channels.
class PRing {
  constructor(channels) {
    this.ch = channels;
    this.t = new Float64Array(P_CAP);
    this.v = Array.from({ length: channels }, () => new Float32Array(P_CAP));
    this.start = 0;
    this.n = 0;
  }
  push(t, vals) {
    const i = (this.start + this.n) % P_CAP;
    this.t[i] = t;
    for (let c = 0; c < this.ch; c++) this.v[c][i] = vals[c] || 0;
    if (this.n < P_CAP) this.n++; else this.start = (this.start + 1) % P_CAP;
  }
  T(k) { return this.t[(this.start + k) % P_CAP]; }
  V(c, k) { return this.v[c][(this.start + k) % P_CAP]; }
  after(t) {   // first sample later than t
    let lo = 0, hi = this.n;
    while (lo < hi) { const m = (lo + hi) >> 1; if (this.T(m) > t) hi = m; else lo = m + 1; }
    return lo;
  }
  avg(c, tEnd) {   // mean over the P_AVG seconds up to tEnd; null if none
    let k = this.after(tEnd) - 1, s = 0, n = 0;
    if (k < 0) return null;
    const from = this.T(k) - P_AVG;
    for (; k >= 0 && this.T(k) > from; k--) { s += this.V(c, k); n++; }
    return n ? s / n : null;
  }
}

// ---------------------------------------------------------------------------
// Formatting (CPU Graph's)
// ---------------------------------------------------------------------------
const pPct = v => `${v.toFixed(1)}%`;
const pNum = v => (v >= 100 ? v.toFixed(0) : v.toFixed(1));
const pBits = b => (b >= 1e9 ? `${pNum(b / 1e9)} Gb/s` : b >= 1e6 ? `${pNum(b / 1e6)} Mb/s` : `${pNum(b / 1e3)} Kb/s`);
const pBytes = b => (b >= 1e9 ? `${pNum(b / 1e9)} GB/s` : b >= 1e6 ? `${pNum(b / 1e6)} MB/s` : `${pNum(b / 1e3)} KB/s`);
const trim2 = v => String(+v.toFixed(2));
const pAxisBits = v => (v >= 1e12 ? `${trim2(v / 1e12)} Tb/s` : v >= 1e9 ? `${trim2(v / 1e9)} Gb/s`
  : v >= 1e6 ? `${trim2(v / 1e6)} Mb/s` : `${trim2(v / 1e3)} Kb/s`);
const pAxisBytes = v => (v >= 1e9 ? `${trim2(v / 1e9)} GB/s` : v >= 1e6 ? `${trim2(v / 1e6)} MB/s` : `${trim2(v / 1e3)} KB/s`);
const pLink = l => (l < 0 ? 'disconnected' : l === 0 ? '' : l >= 1e9 ? `${+(l / 1e9).toFixed(1)} Gb/s link` : `${+(l / 1e6).toFixed(1)} Mb/s link`);
const pSpanLabel = s => (s >= 60 && s % 60 === 0 ? `${s / 60} min` : `${s} s`);
function niceCeil(v) {   // smallest 1-2-5 x 10^k >= v
  if (v <= 0) return 1;
  const mag = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 5, 10]) if (m * mag >= v * 0.9999) return m * mag;
  return 10 * mag;
}
const pInterval = () => (pp.meta ? pp.meta.interval : 50);
const pVisible = () => (pInterval() <= 50 ? Math.max(1, pp.span / 3) : pp.span);   // at 20/s, a third: samples stay apart
const pRate = () => { const n = Math.max(1, 1000 / pInterval()); return `${n} ${n === 1 ? 'sample' : 'samples'}/s`; };

// ---------------------------------------------------------------------------
// Data from the server
// ---------------------------------------------------------------------------
function pStore(id) {
  let s = pm.get(id);
  if (!s) { s = { v: -1, cpu: null, nets: [], disks: [], info: null, lastLink: [] }; pm.set(id, s); }
  return s;
}

function onPerf(msg) {
  for (const [id, [v, samples]] of Object.entries(msg.m)) {
    const s = pStore(id);
    if (s.v !== v) { s.v = v; s.cpu = null; s.nets = []; s.disks = []; s.lastLink = []; }   // new layout or clock
    for (const [t, cpu, nets, disks] of samples) {
      if (!s.cpu || s.cpu.ch !== cpu.length || s.nets.length !== nets.length || s.disks.length !== disks.length) {
        s.cpu = new PRing(cpu.length);
        s.nets = nets.map(() => new PRing(3));
        s.disks = disks.map(() => new PRing(3));
      }
      s.cpu.push(t, cpu);
      nets.forEach((x, i) => s.nets[i].push(t, x));
      disks.forEach((x, i) => s.disks[i].push(t, x));
    }
  }
  const local = msg.m.local && msg.m.local[1].length ? msg.m.local[1][msg.m.local[1].length - 1][0] : msg.t;
  if (!pp.paused) pp.tEnd = local;   // shared clock: every graph ends at this PC's newest sample
  pInvalidate();
}

function onPerfMeta(meta) {
  const first = !pp.meta;
  pp.meta = meta;
  const ids = new Set(meta.machines.map(m => m.id));
  for (const id of pm.keys()) if (!ids.has(id)) pm.delete(id);
  for (const m of meta.machines) pStore(m.id).info = m;
  $('pctl-rate').value = String(meta.interval);
  for (const b of $('pctl-source').querySelectorAll('button')) b.classList.toggle('on', b.dataset.v === meta.source);
  fillSpans();
  if (first && !pp.restored) {   // put back the rate and measure chosen last time
    pp.restored = true;
    const want = {};
    const iv = loadPref('perf.interval'), src = loadPref('perf.source');
    if (P_INTERVALS.includes(iv) && iv !== meta.interval) want.interval = iv;
    if ((src === 'cputime' || src === 'utility') && src !== meta.source) want.source = src;
    if (Object.keys(want).length) postPerf(want);
  }
  pInvalidate();
}
perfListeners.perf.push(onPerf);
perfListeners.perfmeta.push(onPerfMeta);

async function postPerf(body) {
  try {
    const res = await fetch('/api/perf', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                           body: JSON.stringify(body) });
    return res.ok;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Drawing
// ---------------------------------------------------------------------------
function readPerfColors() {
  const cs = getComputedStyle(document.documentElement);
  const v = n => cs.getPropertyValue(n).trim();
  PC = { line: v('--p-line'), avg: v('--p-avg'), rx: v('--p-rx'), tx: v('--p-tx'), read: v('--p-read'),
         write: v('--p-write'), plot: v('--panel'), border: v('--border-strong'), warn: v('--warn'), bg: v('--bg') };
}

function pInvalidate() {
  if (pQueued || activeTab !== 'perf') return;
  pQueued = true;
  requestAnimationFrame(() => { pQueued = false; if (activeTab === 'perf') pdraw(); });
}

function pFont(size, weight = 400) { pctx.font = `${weight} ${size}px ${T.font}`; }
function pText(str, x, y, size, color, weight = 400, align = 'left', maxW = 0) {
  pFont(size, weight);
  pctx.fillStyle = color;
  pctx.textAlign = align;
  pctx.textBaseline = 'alphabetic';
  if (maxW > 0 && pctx.measureText(str).width > maxW) {   // shorten with an ellipsis
    while (str.length > 1 && pctx.measureText(str + '…').width > maxW) str = str.slice(0, -1);
    str += '…';
  }
  pctx.fillText(str, x, y);
  return pctx.measureText(str).width;
}
function pWidth(str, size, weight = 400) { pFont(size, weight); return pctx.measureText(str).width; }

function visibleMachines() {
  if (!pp.meta) return [];
  return pp.meta.machines.filter(m => m.id === 'local' || pp.remotes).map(m => pStore(m.id));
}

function bodyLayout(rows) {
  const fixed = PL.top + rows * PL.title + (rows - 1) * PL.rowGap + PL.margin;
  return Math.max(PL.minBody, (pH - fixed) / rows);
}

function pdraw() {
  const r = pstage.getBoundingClientRect();
  if (!r.width || !r.height) return;
  readPerfColors();
  const machines = visibleMachines();
  const rows = Math.max(1, machines.length);
  pW = pstage.clientWidth;
  pH = r.height;
  const body = bodyLayout(rows);
  const contentH = Math.max(pH, PL.top + rows * (PL.title + body) + (rows - 1) * PL.rowGap + PL.margin);
  pdpr = window.devicePixelRatio || 1;
  if (pcv.width !== Math.round(pW * pdpr) || pcv.height !== Math.round(contentH * pdpr)) {
    pcv.width = Math.round(pW * pdpr);
    pcv.height = Math.round(contentH * pdpr);
    pcv.style.width = pW + 'px';
    pcv.style.height = contentH + 'px';
  }
  const g = pctx;
  g.setTransform(pdpr, 0, 0, pdpr, 0, 0);
  g.clearRect(0, 0, pW, contentH);
  pp.plots = [];
  const left = PL.margin, right = pW - PL.margin, span = pVisible();
  drawTopBar(left, right, span);
  if (!machines.length) {
    pText('Waiting for the first sample…', pW / 2, pH / 2, 13, T.muted, 400, 'center');
    return;
  }
  const showNet = pp.net, showDisk = pp.disk;
  const columns = 1 + showNet + showDisk;
  const weights = PL.cpuWeight + showNet + showDisk;
  const usable = right - left - (columns - 1) * PL.colGap;
  const cpuW = usable * PL.cpuWeight / weights, otherW = usable / weights;
  let y = PL.top;
  for (const s of machines) {
    const info = s.info;
    const bodyTop = y + PL.title;
    let x = left;
    drawCpuTitle(s, x, y, cpuW);
    drawCpuGrid(s, { x, y: bodyTop, w: cpuW, h: body }, span);
    x += cpuW + PL.colGap;
    if (showNet) {
      pText('Network adapters', x, bodyTop - 7, 12.5, T.ink2);
      drawStrips(s, 'net', { x, y: bodyTop, w: otherW, h: body }, span);
      x += otherW + PL.colGap;
    }
    if (showDisk) {
      pText('Disk devices', x, bodyTop - 7, 12.5, T.ink2);
      drawStrips(s, 'disk', { x, y: bodyTop, w: otherW, h: body }, span);
    }
    if (info.id !== 'local' && info.status) veil(info.status, { x: left, y: bodyTop, w: right - left, h: body });
    y = bodyTop + body + PL.rowGap;
  }
  drawCrosshair(span);
}

// "20 samples/s · last 20 s · CPU time · numbers are 3-second averages"  ...  note
function drawTopBar(left, right, span) {
  const meta = pp.meta, base = PL.top - 9;
  const source = meta && meta.source === 'utility' ? 'Processor utility' : 'CPU time';
  const info = `${pRate()}  ·  last ${pSpanLabel(span)}  ·  ${source}  ·  numbers are 3-second averages`;
  let note = '', color = PC.warn;
  if (pp.paused) note = 'PAUSED  –  press Space to resume';
  else if (meta && meta.sourceNote) note = meta.sourceNote;
  else if (performance.now() - pp.started < 20000) {
    note = 'Keys: Space  ↑ ↓  ← →  R  N  D  M  C  A' + (pp.desktop ? '  T  E' : '');
    color = T.muted;
  }
  const noteW = note ? Math.min(pWidth(note, 12.5), (right - left) * 0.6) : 0;
  if (note) pText(note, right, base, 12.5, color, 400, 'right', noteW);
  const room = right - left - noteW - 24;
  if (room > 40) pText(info, left, base, 12.5, T.ink2, 400, 'left', room);
}

// "tinker9  2 threads · 10.1.0.13"  ...  "23.4%"
function drawCpuTitle(s, x, y, w) {
  const info = s.info, base = y + PL.title - 7;
  const total = s.cpu ? s.cpu.avg(0, pp.tEnd) : null;
  const live = total != null && (info.id === 'local' || !info.status);   // no stale figure while disconnected
  const value = live ? pPct(total) : '--';
  const vw = pWidth(value, 15, 600);
  pText(value, x + w, base, 15, PC.line, 600, 'right');
  const room = w - vw - 12;
  const nw = Math.min(pWidth(info.name, 15, 600), room);
  pText(info.name, x, base, 15, T.ink, 600, 'left', room);
  const detailRoom = room - nw - 8;
  if (info.detail && detailRoom > 30) pText(info.detail, x + nw + 8, base, 12.5, T.ink2, 400, 'left', detailRoom);
}

// Columns and rows for `cells` graphs in a w x h area, each near the preferred shape.
function gridShape(cells, w, h) {
  let best = Infinity, cols = 1, rows = cells;
  for (let c = 1; c <= cells; c++) {
    const r = Math.ceil(cells / c);
    if (c > 1 && (c - 1) * r >= cells) continue;
    const aspect = (w / c) / Math.max(1, h / r);
    const score = Math.abs(Math.log(aspect / PL.aspect)) + 3 * (c * r - cells) / cells;
    if (score < best) { best = score; cols = c; rows = r; }
  }
  return [cols, rows];
}

function pframe(p, divisions, span, verticals) {
  const g = pctx;
  g.fillStyle = PC.plot;
  g.fillRect(p.x, p.y, p.w, p.h);
  g.beginPath();
  for (let i = 1; i < divisions; i++) {
    const y = Math.round(p.y + p.h * i / divisions) + 0.5;
    g.moveTo(p.x, y); g.lineTo(p.x + p.w, y);
  }
  if (verticals) {
    const step = P_GRID.find(s => span <= s * 6) || 600;
    for (let sec = step; sec < span; sec += step) {
      const x = Math.round(p.x + p.w - p.w * sec / span) + 0.5;
      g.moveTo(x, p.y); g.lineTo(x, p.y + p.h);
    }
  }
  g.strokeStyle = T.grid;
  g.lineWidth = 1;
  g.stroke();
}
function border(p) {
  pctx.strokeStyle = PC.border;
  pctx.lineWidth = 1;
  pctx.strokeRect(Math.round(p.x) + 0.5, Math.round(p.y) + 0.5, Math.round(p.w) - 1, Math.round(p.h) - 1);
}

// Trailing 3-second average of samples [from, to) of a channel.
function trailingAvg(ring, c, from, to) {
  const out = new Float32Array(Math.max(0, to - from));
  let j = from;
  while (j > 0 && ring.T(j - 1) > ring.T(from) - P_AVG) j--;
  let sum = 0;
  for (let k = j; k < from; k++) sum += ring.V(c, k);
  for (let i = from; i < to; i++) {
    sum += ring.V(c, i);
    while (ring.T(j) <= ring.T(i) - P_AVG) { sum -= ring.V(c, j); j++; }
    out[i - from] = sum / (i - j + 1);
  }
  return out;
}

// One point per pixel column: the mean of its samples (small CPU graphs).
function meanPath(ring, get, from, to, p, span, yMax) {
  const g = pctx, xPer = p.w / span, right = p.x + p.w, bottom = p.y + p.h, ys = p.h / yMax;
  let col = null, sum = 0, n = 0, started = false;
  g.beginPath();
  const emit = () => {
    const x = col + 0.5, y = bottom - Math.min(sum / n, yMax) * ys;
    if (started) g.lineTo(x, y); else { g.moveTo(x, y); started = true; }
  };
  for (let k = from; k < to; k++) {
    const c = Math.floor(right - (pp.tEnd - ring.T(k)) * xPer);
    if (c !== col) { if (n) emit(); col = c; sum = 0; n = 0; }
    sum += get(k); n++;
  }
  if (n) emit();
}

// Every sample when sparse; first/min/max/last per pixel column when dense (strips).
function stripPath(ring, c, from, to, p, span, yMax) {
  const g = pctx, xPer = p.w / span, right = p.x + p.w, bottom = p.y + p.h, ys = p.h / yMax;
  const Y = v => bottom - Math.min(v, yMax) * ys;
  g.beginPath();
  let firstX = null, lastX = null;
  if (to - from <= p.w) {
    for (let k = from; k < to; k++) {
      const x = right - (pp.tEnd - ring.T(k)) * xPer, y = Y(ring.V(c, k));
      if (firstX === null) { g.moveTo(x, y); firstX = x; } else g.lineTo(x, y);
      lastX = x;
    }
  } else {
    let col = null, f = 0, lo = 0, hi = 0, last = 0;
    const flush = () => {
      const x = col + 0.5;
      if (firstX === null) { g.moveTo(x, Y(f)); firstX = x; } else g.lineTo(x, Y(f));
      g.lineTo(x, Y(lo)); g.lineTo(x, Y(hi)); g.lineTo(x, Y(last));
      lastX = x;
    };
    for (let k = from; k < to; k++) {
      const v = ring.V(c, k), cc = Math.floor(right - (pp.tEnd - ring.T(k)) * xPer);
      if (cc !== col) { if (col !== null) flush(); col = cc; f = lo = hi = last = v; }
      else { lo = Math.min(lo, v); hi = Math.max(hi, v); last = v; }
    }
    if (col !== null) flush();
  }
  return [firstX, lastX];
}

function drawSeries(ring, c, from, to, p, span, yMax, color, fill) {
  const g = pctx;
  const [fx, lx] = stripPath(ring, c, from, to, p, span, yMax);
  if (fx === null) return;
  g.strokeStyle = color;
  g.lineWidth = 1.5;
  g.lineJoin = 'round';
  g.stroke();
  if (fill) {   // the received / read area, faintly filled
    g.lineTo(lx, p.y + p.h);
    g.lineTo(fx, p.y + p.h);
    g.closePath();
    g.globalAlpha = 0.17;
    g.fillStyle = color;
    g.fill();
    g.globalAlpha = 1;
  }
}

function clipTo(p) {
  pctx.save();
  pctx.beginPath();
  pctx.rect(p.x, p.y - 3, p.w + 1, p.h + 6);
  pctx.clip();
}

// One small graph per logical processor (or one for the total), each the per-pixel
// mean of its samples, with the 3-second average on top.
function drawCpuGrid(s, area, span) {
  const cpu = s.cpu, info = s.info;
  const threads = pp.threads && cpu ? cpu.ch - 1 : 0;
  const cells = Math.max(1, threads), gap = PL.cellGap;
  const [cols, rows] = gridShape(cells, area.w + gap, area.h + gap);
  const cw = (area.w - (cols - 1) * gap) / cols, ch = (area.h - (rows - 1) * gap) / rows;
  if (cw < 4 || ch < 4) { pframe(area, 1, span, false); border(area); return; }
  const from = cpu ? Math.max(0, cpu.after(pp.tEnd - span) - 1) : 0, to = cpu ? cpu.after(pp.tEnd) : 0;
  const label = cw >= 26 && ch >= 18;
  for (let i = 0; i < cells; i++) {
    const p = { x: area.x + (i % cols) * (cw + gap), y: area.y + Math.floor(i / cols) * (ch + gap), w: cw, h: ch };
    pframe(p, ch >= 30 ? 2 : 1, span, false);
    const channel = threads > 0 ? 1 + i : 0;
    if (cpu && to - from >= 2) {
      clipTo(p);
      meanPath(cpu, k => cpu.V(channel, k), from, to, p, span, 100);
      pctx.strokeStyle = PC.line; pctx.lineWidth = 1.5; pctx.lineJoin = 'round'; pctx.stroke();
      if (pp.avg) {
        const a = trailingAvg(cpu, channel, from, to);
        meanPath(cpu, k => a[k - from], from, to, p, span, 100);
        pctx.strokeStyle = PC.avg; pctx.lineWidth = 1.5; pctx.stroke();
      }
      pctx.restore();
    }
    border(p);
    if (label) pText(threads > 0 ? String(i) : 'all threads', p.x + 3, p.y + 11, 10.5, T.muted);
    pp.plots.push({ ...p, kind: 'cpu', s, channel, thread: threads > 0 ? i : null });
  }
  if (info.id !== 'local' && !info.status && info.note) {   // an old agent: say so over the graph
    pText(info.note, area.x + 8, area.y + area.h - 8, 12, PC.warn, 400, 'left', area.w - 16);
  }
}

// Stacked strips: one per adapter (received filled, sent) or per disk (read filled, written).
function drawStrips(s, kind, area, span) {
  const info = s.info, items = kind === 'net' ? info.nets : info.disks;
  if (!items.length) {
    const why = info.id !== 'local' && info.status ? ''
      : info.id !== 'local' && info.note ? info.note
      : info.id !== 'local' && !s.cpu ? 'Connecting…'
      : kind === 'net' ? `No network adapters selected${pp.desktop ? ' – press E to choose' : ''}`
      : `No disks selected${pp.desktop ? ' – press E to choose' : ''}`;
    pframe(area, 1, span, false);
    border(area);
    if (why) pText(why, area.x + 10, area.y + Math.min(area.h - 6, 22), 12.5, T.ink2, 400, 'left', area.w - 20);
    return;
  }
  const gap = PL.stripGap, h = (area.h - (items.length - 1) * gap) / items.length;
  items.forEach((item, i) => {
    const p = { x: area.x, y: area.y + i * (h + gap), w: area.w, h };
    const ring = (kind === 'net' ? s.nets : s.disks)[i];
    if (kind === 'net') drawNetStrip(s, item, ring, i, p, span); else drawDiskStrip(s, item, ring, p, span);
    pp.plots.push({ ...p, kind, s, item, ring });
  });
}

// Top of a network strip's axis: the link speed (kept while the link is down),
// a fixed rate, or the traffic on screen rounded up to 1-2-5 (at least 1 Mb/s).
function netCeiling(s, item, ring, i, from, to) {
  if (item.scale.mode === 'fixed') return item.scale.bps;
  if (item.scale.mode === 'link' && ring && ring.n) {
    const link = ring.V(2, ring.n - 1);
    if (link > 0) s.lastLink[i] = link;
    if (s.lastLink[i] > 0) return s.lastLink[i];
  }
  let peak = 0;
  for (let k = from; k < to; k++) peak = Math.max(peak, ring.V(0, k), ring.V(1, k));
  return niceCeil(Math.max(peak, 1e6));
}

function stripLive(s, item, ring) {
  return ring && ring.n > 0 && !item.status && (s.info.id === 'local' || !s.info.status);
}

function drawNetStrip(s, item, ring, i, p, span) {
  const from = ring ? Math.max(0, ring.after(pp.tEnd - span) - 1) : 0, to = ring ? ring.after(pp.tEnd) : 0;
  const yMax = ring ? netCeiling(s, item, ring, i, from, to) : 1e6;
  pframe(p, 2, span, true);
  if (ring && to - from >= 2 && p.h > 2) {
    clipTo(p);
    drawSeries(ring, 0, from, to, p, span, yMax, PC.rx, true);
    drawSeries(ring, 1, from, to, p, span, yMax, PC.tx, false);
    pctx.restore();
  }
  border(p);
  const live = stripLive(s, item, ring);
  const link = ring && ring.n ? ring.V(2, ring.n - 1) : 0;
  const axis = item.status ? '' : item.scale.mode === 'link' && ring && ring.n && (link !== 0 || s.lastLink[i] > 0)
    ? pLink(link) : `scale ${pAxisBits(yMax)}`;
  stripLabels(p, item.name, axis, `↓ ${live ? pBits(ring.avg(0, pp.tEnd)) : '--'}`, PC.rx,
              `↑ ${live ? pBits(ring.avg(1, pp.tEnd)) : '--'}`, PC.tx, null);
  stripProblems(p, item.status, item.problem);
}

function drawDiskStrip(s, item, ring, p, span) {
  const from = ring ? Math.max(0, ring.after(pp.tEnd - span) - 1) : 0, to = ring ? ring.after(pp.tEnd) : 0;
  let peak = 0;
  for (let k = from; k < to; k++) peak = Math.max(peak, ring.V(1, k), ring.V(2, k));
  const yMax = niceCeil(Math.max(peak, 1e6));   // at least 1 MB/s
  pframe(p, 2, span, true);
  if (ring && to - from >= 2 && p.h > 2) {
    clipTo(p);
    drawSeries(ring, 1, from, to, p, span, yMax, PC.read, true);
    drawSeries(ring, 2, from, to, p, span, yMax, PC.write, false);
    pctx.restore();
  }
  border(p);
  const live = stripLive(s, item, ring);
  const axis = `scale ${pAxisBytes(yMax)}${item.detail ? `  ·  ${item.detail}` : ''}`;
  stripLabels(p, item.name, axis, `R ${live ? pBytes(ring.avg(1, pp.tEnd)) : '--'}`, PC.read,
              `W ${live ? pBytes(ring.avg(2, pp.tEnd)) : '--'}`, PC.write,
              live ? `${pPct(ring.avg(0, pp.tEnd))} busy` : null);
  stripProblems(p, item.status, null);
}

// Name and axis on the left; the two readings (and an optional extra) on the right.
function stripLabels(p, name, axis, a, aColor, b, bColor, extra) {
  if (p.h < 12 || p.w < 60) return;
  const top = p.y + 14, pad = 6;
  let x = p.x + p.w - pad;
  const bw = pWidth(b, 12.5), aw = pWidth(a, 12.5), ew = extra ? pWidth(extra, 12.5) : 0;
  const block = aw + 12 + bw;
  if (extra && block + ew + 12 < p.w * 0.6) { x -= ew; pText(extra, x, top, 12.5, T.ink2); x -= 12; }
  if (block < p.w - 2 * pad) {
    x -= bw; pText(b, x, top, 12.5, bColor);
    x -= 12 + aw; pText(a, x, top, 12.5, aColor);
  }
  const room = x - 14 - (p.x + pad);
  if (room < 20) return;
  const nw = Math.min(pWidth(name, 12.5), room);
  pText(name, p.x + pad, top, 12.5, T.ink, 400, 'left', room);
  const axisRoom = room - nw - 8;
  if (axis && axisRoom > 24) pText(axis, p.x + pad + nw + 8, top, 12.5, T.muted, 400, 'left', axisRoom);
}

function stripProblems(p, status, problem) {
  const text = status || problem;
  if (!text || p.h < 14) return;
  const y = p.h >= 34 ? p.y + p.h - 6 : p.y + p.h / 2 + 4;
  pText(text, status ? p.x + p.w / 2 : p.x + 8, y, 12.5, PC.warn, 400, status ? 'center' : 'left', p.w - 16);
}

// A message across a whole row (a remote machine that can't be reached).
function veil(text, row) {
  const g = pctx, w = Math.min(pWidth(text, 12.5) + 28, row.w - 8), h = 30;
  if (w < 40 || row.h < h) return;
  const x = row.x + (row.w - w) / 2, y = row.y + (row.h - h) / 2;
  g.globalAlpha = 0.9;
  g.fillStyle = PC.bg;
  g.fillRect(x, y, w, h);
  g.globalAlpha = 1;
  g.strokeStyle = PC.border;
  g.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
  pText(text, x + w / 2, y + 19, 12.5, PC.warn, 400, 'center', w - 28);
}

// The hovered moment, marked in every graph.
function drawCrosshair(span) {
  const hv = pp.hover;
  if (!hv) return;
  const g = pctx;
  g.beginPath();
  for (const p of pp.plots) {
    const x = Math.round(p.x + p.w - (pp.tEnd - hv.t) * p.w / span) + 0.5;
    if (x < p.x || x > p.x + p.w) continue;
    g.moveTo(x, p.y + 1); g.lineTo(x, p.y + p.h - 1);
  }
  g.strokeStyle = T.lead;
  g.lineWidth = 1;
  g.stroke();
}

// ---------------------------------------------------------------------------
// Hover
// ---------------------------------------------------------------------------
function nearest(ring, t) {
  if (!ring || !ring.n) return -1;
  const k = ring.after(t);
  if (k <= 0) return 0;
  if (k >= ring.n) return ring.n - 1;
  return t - ring.T(k - 1) <= ring.T(k) - t ? k - 1 : k;
}

function showPerfTip(ev) {
  const r = pcv.getBoundingClientRect(), mx = ev.clientX - r.left, my = ev.clientY - r.top;
  const p = pp.plots.find(q => mx >= q.x && mx <= q.x + q.w && my >= q.y && my <= q.y + q.h);
  if (!p) { pp.hover = null; ptip.hidden = true; pInvalidate(); return; }
  const span = pVisible(), t = pp.tEnd - (p.x + p.w - mx) / p.w * span;
  pp.hover = { t };
  const ring = p.kind === 'cpu' ? p.s.cpu : p.ring, k = nearest(ring, t);
  const name = p.s.info.name;
  let html;
  if (k < 0) {
    html = `<div class="head">${esc(name)}</div><div class="faint">No samples yet</div>`;
  } else {
    const ago = Math.max(0, pp.tEnd - ring.T(k));
    const when = `<div class="faint">${ago < 0.05 ? 'now' : `${ago.toFixed(ago < 10 ? 1 : 0)} s ago`}</div>`;
    if (p.kind === 'cpu') {
      const what = p.thread == null ? (p.s.cpu.ch > 1 ? 'all threads' : 'CPU') : `thread ${p.thread}`;
      const avg = trailingAvg(ring, p.channel, k, k + 1)[0];
      html = `<div class="head">${esc(name)} · ${what}</div><div>${pPct(ring.V(p.channel, k))}</div>` +
        `<div class="faint">3 s average ${pPct(avg)}</div>${when}`;
    } else if (p.kind === 'net') {
      html = `<div class="head">${esc(p.item.name)} <span class="faint">${esc(name)}</span></div>` +
        `<div><span style="color:${PC.rx}">↓ ${pBits(ring.V(0, k))}</span> &nbsp; <span style="color:${PC.tx}">↑ ${pBits(ring.V(1, k))}</span></div>` +
        (pLink(ring.V(2, k)) ? `<div class="faint">${pLink(ring.V(2, k))}</div>` : '') + when;
    } else {
      html = `<div class="head">${esc(p.item.name)} <span class="faint">${esc(name)}</span></div>` +
        `<div><span style="color:${PC.read}">R ${pBytes(ring.V(1, k))}</span> &nbsp; <span style="color:${PC.write}">W ${pBytes(ring.V(2, k))}</span></div>` +
        `<div class="faint">${pPct(ring.V(0, k))} busy${p.item.detail ? ` · ${esc(p.item.detail)}` : ''}</div>${when}`;
    }
  }
  ptip.innerHTML = html;
  ptip.hidden = false;
  const sr = pstage.getBoundingClientRect();
  let x = ev.clientX - sr.left + 14, y = ev.clientY - sr.top + pstage.scrollTop + 14;
  if (x + ptip.offsetWidth > sr.width - 8) x -= ptip.offsetWidth + 28;
  if (ev.clientY - sr.top + 14 + ptip.offsetHeight > sr.height - 8) y -= ptip.offsetHeight + 28;
  ptip.style.left = Math.max(4, x) + 'px';
  ptip.style.top = Math.max(4, y) + 'px';
  pInvalidate();
}
pcv.addEventListener('mousemove', showPerfTip);
pcv.addEventListener('mouseleave', () => { pp.hover = null; ptip.hidden = true; pInvalidate(); });

// ---------------------------------------------------------------------------
// Controls and keys (CPU Graph's)
// ---------------------------------------------------------------------------
function fillSpans() {
  const sel = $('pctl-span'), keep = String(pp.span);
  sel.innerHTML = P_SPANS.map(s => `<option value="${s}">${pSpanLabel(pInterval() <= 50 ? Math.max(1, s / 3) : s)}</option>`).join('');
  sel.value = keep;
}

function setInterval_(ms) {
  if (!P_INTERVALS.includes(ms)) return;
  savePref('perf.interval', ms);
  if (pp.meta) { pp.meta.interval = ms; fillSpans(); }
  $('pctl-rate').value = String(ms);
  postPerf({ interval: ms });
  pInvalidate();
}
function setSpan(s) {
  if (!P_SPANS.includes(s)) return;
  pp.span = s;
  savePref('perf.span', s);
  $('pctl-span').value = String(s);
  pInvalidate();
}
function setSource(src) {
  savePref('perf.source', src);
  for (const b of $('pctl-source').querySelectorAll('button')) b.classList.toggle('on', b.dataset.v === src);
  if (pp.meta) pp.meta.source = src;
  postPerf({ source: src });
  pInvalidate();
}
function setFlag(key, on) {
  pp[key] = on;
  savePref('perf.' + key, on);
  const box = { avg: 'pctl-avg', remotes: 'pctl-remotes', net: 'pctl-net', disk: 'pctl-disk' }[key];
  if (box) $(box).checked = on;
  if (key === 'threads') for (const b of $('pctl-threads').querySelectorAll('button')) b.classList.toggle('on', (b.dataset.v === '1') === on);
  pInvalidate();
}
function togglePause() {
  pp.paused = !pp.paused;
  $('pctl-pause').textContent = pp.paused ? 'Resume' : 'Pause';
  if (!pp.paused) {   // back to now
    const s = pm.get('local');
    if (s && s.cpu && s.cpu.n) pp.tEnd = s.cpu.T(s.cpu.n - 1);
  }
  pInvalidate();
}
function toggleTop() {
  if (!pp.desktop) return;
  pp.onTop = !pp.onTop;
  $('pctl-top').classList.toggle('on', pp.onTop);
  $('pctl-top').setAttribute('aria-pressed', String(pp.onTop));
  savePref('perf.onTop', pp.onTop);
  try { window.pywebview.api.set_on_top(pp.onTop); } catch { /* not in the desktop app */ }
}
function editSettings() {
  if (pp.desktop) try { window.pywebview.api.edit_perf_settings(); } catch { /* not in the desktop app */ }
}
const stepOf = (list, value, dir) => list[Math.max(0, Math.min(list.length - 1, list.indexOf(value) + dir))];

$('pctl-rate').addEventListener('change', ev => setInterval_(Number(ev.target.value)));
$('pctl-span').addEventListener('change', ev => setSpan(Number(ev.target.value)));
$('pctl-source').addEventListener('click', ev => { const b = ev.target.closest('button[data-v]'); if (b) setSource(b.dataset.v); });
$('pctl-threads').addEventListener('click', ev => { const b = ev.target.closest('button[data-v]'); if (b) setFlag('threads', b.dataset.v === '1'); });
for (const [id, key] of [['pctl-avg', 'avg'], ['pctl-remotes', 'remotes'], ['pctl-net', 'net'], ['pctl-disk', 'disk']]) {
  $(id).addEventListener('change', ev => setFlag(key, ev.target.checked));
}
$('pctl-pause').addEventListener('click', togglePause);
$('pctl-top').addEventListener('click', toggleTop);
$('pctl-edit').addEventListener('click', editSettings);

document.addEventListener('keydown', ev => {
  if (activeTab !== 'perf' || ev.ctrlKey || ev.altKey || ev.metaKey) return;
  if (ev.target.closest && ev.target.closest('input, select, textarea')) return;
  const k = ev.key;
  const handled = {
    ' ': togglePause,
    ArrowUp: () => setInterval_(stepOf(P_INTERVALS, pInterval(), -1)),
    ArrowDown: () => setInterval_(stepOf(P_INTERVALS, pInterval(), +1)),
    ArrowLeft: () => setSpan(stepOf(P_SPANS, pp.span, -1)),
    ArrowRight: () => setSpan(stepOf(P_SPANS, pp.span, +1)),
    r: () => setFlag('remotes', !pp.remotes), n: () => setFlag('net', !pp.net), d: () => setFlag('disk', !pp.disk),
    m: () => setSource(pp.meta && pp.meta.source === 'utility' ? 'cputime' : 'utility'),
    c: () => setFlag('threads', !pp.threads), a: () => setFlag('avg', !pp.avg),
    t: toggleTop, e: editSettings,
  }[k.length === 1 ? k.toLowerCase() : k];
  if (!handled) return;
  if (ev.target.closest && ev.target.closest('.tabs') && (k === 'ArrowLeft' || k === 'ArrowRight')) return;   // moving between tabs
  ev.preventDefault();
  handled();
});

// The desktop app adds Always on top (T) and opening settings.ini (E).
function desktopReady() {
  if (!(window.pywebview && window.pywebview.api && window.pywebview.api.set_on_top)) return;
  pp.desktop = true;
  $('pctl-top').hidden = false;
  $('pctl-edit').hidden = false;
  if (loadPref('perf.onTop') === true) toggleTop();
  pInvalidate();
}
window.addEventListener('pywebviewready', desktopReady);

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------
pp.span = P_SPANS.includes(loadPref('perf.span')) ? loadPref('perf.span') : 60;
for (const key of ['threads', 'avg', 'remotes', 'net', 'disk']) {
  const v = loadPref('perf.' + key);
  if (typeof v === 'boolean') setFlag(key, v);
}
fillSpans();
readPerfColors();
darkQuery.addEventListener('change', pInvalidate);
new ResizeObserver(pInvalidate).observe(pstage);
desktopReady();
