'use strict';

// ---------------------------------------------------------------------------
// CPU, network and disk graphs, first ported from CPU Graph 2.0
// (github.com/esteed/CpuGraph): small graphs per CPU thread or one of them all, a
// strip per network adapter and one per disk. All graphs share one time axis.
// Every number is a 3-second average; the lines show the samples. Here are the
// samples and the drawing; views.js and remote.js put the graphs on pages. Uses
// the theme and helpers from app.js (T, $, esc, loadPref, savePref, perfListeners).
// ---------------------------------------------------------------------------
const P_INTERVALS = [50, 100, 250, 500, 1000];   // ms: 20, 10, 4, 2, 1 samples a second
const P_SPANS = [15, 30, 60, 120, 300];          // seconds (at 20 a second the graphs show a third)
const P_AVG = 3;                                 // seconds averaged for the amber line and every number
const P_CAP = 8192;                              // samples kept per machine: 409 s at 20 a second
const P_GRID = [1, 2, 3, 5, 10, 15, 20, 30, 60, 120, 300];
const PL = { stripGap: 4, cellGap: 3, aspect: 1.6 };
const P_LOST_FADE = 1.6;                         // seconds for a lost adapter's graph to fade out and in

const pp = {
  meta: null, tEnd: 0, paused: false, started: performance.now(),
  span: 60, threads: true, avg: true, onTop: false, restored: false, desktop: false,
};
const pm = new Map();   // machine id -> { v, info, cpu, nets, disks, lastLink }, the last three by item key
let PC = {};

// The canvas being drawn on: { cv, ctx, dpr, w, h, plots, hover, fading }
let PS = null, pctx = null, pAlpha = 1, pDim = 1;   // pDim: 0.4 while a machine that's away is drawn
function makeSurface(canvas) {
  return { cv: canvas, ctx: canvas.getContext('2d'), dpr: 1, w: 0, h: 0, plots: [], hover: null, fading: false };
}
function startSurface(S, w, h) {   // size it, clear it, and draw on it from here
  PS = S;
  pctx = S.ctx;
  pAlpha = pDim = 1;
  S.w = w;
  S.h = h;
  S.dpr = window.devicePixelRatio || 1;
  S.plots = [];
  S.fading = false;
  const cw = Math.round(w * S.dpr), ch = Math.round(h * S.dpr);
  if (S.cv.width !== cw || S.cv.height !== ch) {
    S.cv.width = cw;
    S.cv.height = ch;
    S.cv.style.width = w + 'px';
    S.cv.style.height = h + 'px';
  }
  pctx.setTransform(S.dpr, 0, 0, S.dpr, 0, 0);
  pctx.clearRect(0, 0, w, h);
}

// A ring of samples: time plus a fixed number of channels. It starts small and grows
// until it holds P_KEEP seconds, or P_CAP samples, so hundreds of machines fit.
const P_KEEP = 330;
class PRing {
  constructor(channels) {
    this.ch = channels;
    this.cap = 256;
    this.t = new Float64Array(this.cap);
    this.v = Array.from({ length: channels }, () => new Float32Array(this.cap));
    this.start = 0;
    this.n = 0;
  }
  grow() {
    const order = a => { const b = new a.constructor(this.cap * 2); b.set(a.subarray(this.start)); b.set(a.subarray(0, this.start), this.cap - this.start); return b; };
    this.t = order(this.t);
    this.v = this.v.map(order);
    this.cap *= 2;
    this.start = 0;
  }
  push(t, vals) {
    if (this.n === this.cap && this.cap < P_CAP && t - this.T(0) < P_KEEP) this.grow();
    const i = (this.start + this.n) % this.cap;
    this.t[i] = t;
    for (let c = 0; c < this.ch; c++) this.v[c][i] = vals[c] || 0;
    if (this.n < this.cap) this.n++; else this.start = (this.start + 1) % this.cap;
  }
  T(k) { return this.t[(this.start + k) % this.cap]; }
  V(c, k) { return this.v[c][(this.start + k) % this.cap]; }
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
const pBitsTiny = b => (b >= 1e9 ? `${pNum(b / 1e9)}G` : b >= 1e6 ? `${pNum(b / 1e6)}M` : `${Math.round(b / 1e3)}K`);
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
  if (!s) { s = { v: -1, info: null, cpu: null, nets: new Map(), disks: new Map(), lastLink: new Map() }; pm.set(id, s); }
  return s;
}

function pushRing(map, key, channels, t, values) {
  let ring = map.get(key);
  if (!ring || ring.ch !== channels) map.set(key, ring = new PRing(channels));
  if (ring.n && t <= ring.T(ring.n - 1)) return;   // a reconnected machine's clock, placed a little differently
  ring.push(t, values);
}

function onPerf(msg) {
  for (const [id, [v, samples]] of Object.entries(msg.m)) {
    const s = pStore(id), info = s.info;
    if (!info || info.v !== v) continue;           // its description is on the way
    for (const [t, cpu, nets, disks] of samples) {
      if (nets.length !== info.nets.length || disks.length !== info.disks.length) continue;
      if (!s.cpu || s.cpu.ch !== cpu.length) s.cpu = new PRing(cpu.length);
      if (!s.cpu.n || t > s.cpu.T(s.cpu.n - 1)) s.cpu.push(t, cpu);
      nets.forEach((x, i) => pushRing(s.nets, info.nets[i].key, 3, t, x));
      disks.forEach((x, i) => pushRing(s.disks, info.disks[i].key, x.length, t, x));
    }
  }
  const local = msg.m.local && msg.m.local[1].length ? msg.m.local[1][msg.m.local[1].length - 1][0] : msg.t;
  if (!pp.paused) pp.tEnd = local;   // shared clock: every graph ends at this PC's newest sample
  for (const f of perfDrawers) f();
}

function onPerfMeta(meta) {
  const first = !pp.meta;
  pp.meta = meta;
  const ids = new Set(meta.machines.map(m => m.id));
  for (const id of pm.keys()) if (!ids.has(id)) pm.delete(id);
  for (const m of meta.machines) {
    const s = pStore(m.id);
    if (s.info && s.info.address !== m.address) {   // a different machine now
      pm.delete(m.id);
      pStore(m.id).info = m;
      continue;
    }
    s.info = m;
    const nets = new Set(m.nets.map(i => i.key)), disks = new Set(m.disks.map(i => i.key));
    if (m.state === 'ok') {   // keep what a machine that's away showed
      for (const k of s.nets.keys()) if (!nets.has(k)) s.nets.delete(k);
      for (const k of s.disks.keys()) if (!disks.has(k)) s.disks.delete(k);
    }
  }
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
  for (const f of perfMetaListeners) f(meta);
  for (const f of perfDrawers) f();
}
const perfDrawers = [];          // views.js, remote.js: draw what's showing
const perfMetaListeners = [];    // remote.js: the machines changed
perfListeners.perf.push(onPerf);
perfListeners.perfmeta.push(onPerfMeta);

// Latency and jitter of a machine's adapters, while its Network page shows: { at, g: {key: gateway},
// n: {key: [average ms, jitter ms, lost, pings]}, e: error, w: seconds }. Each adapter's last minute of
// them is kept too, as [when, average, jitter], for the scale of its bars.
const P_LAT_KEEP = 60000;
function onLat(msg) {
  const s = pStore(msg.m), at = performance.now();
  s.lat = { at, g: msg.g || {}, n: msg.n || {}, e: msg.e || '', w: msg.w || 3 };
  s.latHist = s.latHist || new Map();
  for (const [key, [avg, jitter]] of Object.entries(s.lat.n)) {
    const h = s.latHist.get(key) || [];
    h.push([at, avg, jitter]);
    while (h.length && h[0][0] < at - P_LAT_KEEP) h.shift();
    s.latHist.set(key, h);
  }
  for (const f of perfDrawers) f();
}
perfListeners.lat.push(onLat);
// The latest, if it's recent: a page that has just opened, or a machine that stopped answering, has none.
const pLat = s => (s.lat && performance.now() - s.lat.at < 2500 ? s.lat : null);

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

// "20 samples/s · last 20 s · numbers are 3-second averages"  ...  note
function drawTopBar(left, right, base, span, extra = '', note = '', noteColor = '') {
  const info = [pRate(), `last ${pSpanLabel(span)}`, extra, 'numbers are 3-second averages'].filter(Boolean).join('  ·  ');
  if (pp.paused) { note = 'PAUSED  –  press Space to resume'; noteColor = PC.warn; }
  const noteW = note ? Math.min(pWidth(note, 12.5), (right - left) * 0.6) : 0;
  if (note) pText(note, right, base, 12.5, noteColor || PC.warn, 400, 'right', noteW);
  const room = right - left - noteW - 24;
  if (room > 40) pText(info, left, base, 12.5, T.ink2, 400, 'left', room);
}

// "server1  2 threads · 192.168.1.30"  ...  "23.4%"
const pLive = s => s.info.id === 'local' || s.info.state === 'ok';   // no stale figures while disconnected
function drawCpuTitle(s, x, base, w, name, detail, size = 15) {
  const total = s.cpu ? s.cpu.avg(0, pp.tEnd) : null;
  const value = total != null && pLive(s) ? pPct(total) : '--';
  const vw = pWidth(value, size, 600);
  pText(value, x + w, base, size, PC.line, 600, 'right');
  const room = w - vw - 12;
  const nw = Math.min(pWidth(name, size, 600), room);
  pText(name, x, base, size, T.ink, 600, 'left', room);
  const detailRoom = room - nw - 8;
  if (detail && detailRoom > 30) pText(detail, x + nw + 8, base, 12.5, T.ink2, 400, 'left', detailRoom);
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
    g.globalAlpha = 0.17 * pAlpha;
    g.fillStyle = color;
    g.fill();
    g.globalAlpha = pAlpha;
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
function drawCpuGrid(s, area, span, perThread, labels = true) {
  const cpu = s.cpu;
  const threads = perThread && cpu ? cpu.ch - 1 : 0;
  const cells = Math.max(1, threads), gap = PL.cellGap;
  const [cols, rows] = gridShape(cells, area.w + gap, area.h + gap);
  const cw = (area.w - (cols - 1) * gap) / cols, ch = (area.h - (rows - 1) * gap) / rows;
  if (cw < 4 || ch < 4) { pframe(area, 1, span, false); border(area); return; }
  const from = cpu ? Math.max(0, cpu.after(pp.tEnd - span) - 1) : 0, to = cpu ? cpu.after(pp.tEnd) : 0;
  const label = labels && cw >= 26 && ch >= 18;
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
    PS.plots.push({ ...p, kind: 'cpu', s, channel, thread: threads > 0 ? i : null });
  }
}

// Strips, one per adapter (received filled, sent) or per disk (read filled, written),
// stacked in as many columns as asked for.
function drawStrips(s, kind, area, span, cols = 1) {
  const info = s.info, items = kind === 'net' ? info.nets : info.disks;
  if (!items.length) {
    const why = !pLive(s) ? '' : !s.cpu ? 'Waiting for the first sample…'
      : info.note && info.threads === 0 ? info.note
      : kind === 'net' ? 'No network adapter has a link' : 'No disks to show';
    pframe(area, 1, span, false);
    border(area);
    if (why) pText(why, area.x + 10, area.y + Math.min(area.h - 6, 22), 12.5, T.ink2, 400, 'left', area.w - 20);
    return;
  }
  const gap = PL.stripGap, colGap = 14, rows = Math.ceil(items.length / cols);
  const h = (area.h - (rows - 1) * gap) / rows, w = (area.w - (cols - 1) * colGap) / cols;
  items.forEach((item, i) => {
    const p = { x: area.x + (i % cols) * (w + colGap), y: area.y + Math.floor(i / cols) * (h + gap), w, h };
    const ring = (kind === 'net' ? s.nets : s.disks).get(item.key);
    if (kind === 'net') drawNetStrip(s, item, ring, p, span); else drawDiskStrip(s, item, ring, p, span);
    PS.plots.push({ ...p, kind, s, item, ring });
  });
}

// Top of a network strip's axis: the link speed (kept while the link is down), or,
// if it has none that means anything, the traffic on screen rounded up to 1-2-5 (at
// least 1 Mb/s).
function netCeiling(s, item, ring, from, to) {
  if (!item.virtual && ring && ring.n) {
    const link = ring.V(2, ring.n - 1);
    if (link > 0) s.lastLink.set(item.key, link);
    if (s.lastLink.get(item.key) > 0) return s.lastLink.get(item.key);
  }
  let peak = 0;
  for (let k = from; k < to; k++) peak = Math.max(peak, ring.V(0, k), ring.V(1, k));
  return niceCeil(Math.max(peak, 1e6));
}

function stripLive(s, item, ring) {
  return ring && ring.n > 0 && !item.status && pLive(s);
}

const stillMotion = matchMedia('(prefers-reduced-motion: reduce)');
function fadeNow() {   // 1 to 0.15 and back, every P_LOST_FADE seconds
  if (stillMotion.matches) return 0.45;
  return 0.575 + 0.425 * Math.cos(performance.now() / 1000 / P_LOST_FADE * 2 * Math.PI);
}

function drawNetStrip(s, item, ring, p, span, labels = true, more = '') {
  const from = ring ? Math.max(0, ring.after(pp.tEnd - span) - 1) : 0, to = ring ? ring.after(pp.tEnd) : 0;
  const yMax = ring ? netCeiling(s, item, ring, from, to) : 1e6;
  if (item.lost) {   // its link went down a moment ago: fade out and in until it's back or gone
    PS.fading = true;
    pctx.globalAlpha = pAlpha = fadeNow() * pDim;
  }
  pframe(p, 2, span, true);
  if (ring && to - from >= 2 && p.h > 2) {
    clipTo(p);
    drawSeries(ring, 0, from, to, p, span, yMax, PC.rx, true);
    drawSeries(ring, 1, from, to, p, span, yMax, PC.tx, false);
    pctx.restore();
  }
  border(p);
  if (!labels) {
    pctx.globalAlpha = pAlpha = pDim;
    return;
  }
  const live = stripLive(s, item, ring) && !item.lost;
  const link = ring && ring.n ? ring.V(2, ring.n - 1) : 0;
  const axis = item.status ? '' : item.lost ? 'disconnected'
    : !item.virtual && ring && ring.n && (link !== 0 || s.lastLink.get(item.key) > 0) ? pLink(link) : `scale ${pAxisBits(yMax)}`;
  stripLabels(p, item.name, [axis, more].filter(Boolean).join('  ·  '),`↓ ${live ? pBits(ring.avg(0, pp.tEnd)) : '--'}`, PC.rx,
              `↑ ${live ? pBits(ring.avg(1, pp.tEnd)) : '--'}`, PC.tx, null);
  stripProblems(p, item.status, null);
  pctx.globalAlpha = pAlpha = pDim;
}

const pBytesSize = b => (b >= 1e12 ? `${trim2(Math.round(b / 1e11) / 10)} TB` : b >= 1e9 ? `${Math.round(b / 1e9)} GB` : `${Math.round(b / 1e6)} MB`);

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
  const full = item.size ? `${pBytesSize(item.used)} of ${pBytesSize(item.size)} used (${Math.round(100 * item.used / item.size)}%)` : '';
  const axis = [`scale ${pAxisBytes(yMax)}`, item.detail, full].filter(Boolean).join('  ·  ');
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
  const hv = PS.hover;
  if (!hv || hv.t == null) return;
  const g = pctx;
  g.beginPath();
  for (const p of PS.plots) {
    if (P_NOT_GRAPHS.has(p.kind)) continue;
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

// The graph of a surface under a point on it, or null. Marks the moment there for the crosshair.
// The moment pointed at, and the graph it's in (by machine and item key), are kept as S.hover.
const P_NOT_GRAPHS = new Set(['disklabel', 'netbars']);   // things on the canvas that aren't graphs over time
function plotAt(S, mx, my) {
  const p = S.plots.find(q => mx >= q.x && mx <= q.x + q.w && my >= q.y && my <= q.y + q.h);
  if (p && P_NOT_GRAPHS.has(p.kind)) { S.hover = null; return p; }
  S.hover = p ? { t: pp.tEnd - (p.x + p.w - mx) / p.w * pVisible(), kind: p.kind, id: p.s ? p.s.info.id : null,
                  key: p.item ? p.item.key : null } : null;
  return p || null;
}

function plotTip(p, t) {
  if (p.kind === 'disk' && p.ring && p.ring.ch >= 9) return dkTip(p, t);
  const ring = p.kind === 'cpu' ? p.s.cpu : p.ring, k = nearest(ring, t);
  const name = p.s.info.name;
  if (k < 0) return `<div class="head">${esc(p.item ? p.item.name : name)}</div><div class="faint">No samples yet</div>`;
  const ago = Math.max(0, pp.tEnd - ring.T(k));
  const when = `<div class="faint">${ago < 0.05 ? 'now' : `${ago.toFixed(ago < 10 ? 1 : 0)} s ago`}</div>`;
  if (p.kind === 'cpu') {
    const what = p.thread == null ? (p.s.cpu.ch > 1 ? 'all threads' : 'CPU') : `thread ${p.thread}`;
    const avg = trailingAvg(ring, p.channel, k, k + 1)[0];
    return `<div class="head">${esc(name)} · ${what}</div><div>${pPct(ring.V(p.channel, k))}</div>` +
      `<div class="faint">3 s average ${pPct(avg)}</div>${when}`;
  }
  if (p.kind === 'net') {
    return `<div class="head">${esc(p.item.name)} <span class="faint">${esc(name)}</span></div>` +
      `<div><span style="color:${PC.rx}">↓ ${pBits(ring.V(0, k))}</span> &nbsp; <span style="color:${PC.tx}">↑ ${pBits(ring.V(1, k))}</span></div>` +
      (p.item.detail ? `<div class="faint">${esc(p.item.detail)}</div>` : '') +
      (!p.item.virtual && pLink(ring.V(2, k)) ? `<div class="faint">${pLink(ring.V(2, k))}</div>` : '') + when;
  }
  return `<div class="head">${esc(p.item.name)} <span class="faint">${esc(name)}</span></div>` +
    `<div><span style="color:${PC.read}">R ${pBytes(ring.V(1, k))}</span> &nbsp; <span style="color:${PC.write}">W ${pBytes(ring.V(2, k))}</span></div>` +
    `<div class="faint">${pPct(ring.V(0, k))} busy${p.item.detail ? ` · ${esc(p.item.detail)}` : ''}</div>` +
    (p.item.size ? `<div class="faint">${pBytesSize(p.item.used)} of ${pBytesSize(p.item.size)} used</div>` : '') + when;
}

// Put a tip by the pointer, inside its scrolling box.
function placeTip(tipEl, box, ev) {
  const r = box.getBoundingClientRect();
  let x = ev.clientX - r.left + 14, y = ev.clientY - r.top + box.scrollTop + 14;
  if (x + tipEl.offsetWidth > r.width - 8) x -= tipEl.offsetWidth + 28;
  if (ev.clientY - r.top + 14 + tipEl.offsetHeight > r.height - 8) y -= tipEl.offsetHeight + 28;
  tipEl.style.left = Math.max(4, x) + 'px';
  tipEl.style.top = Math.max(box.scrollTop + 4, y) + 'px';
}

// ---------------------------------------------------------------------------
// Controls and keys (CPU Graph's)
// ---------------------------------------------------------------------------
function fillSpans() {
  const sel = $('pctl-span'), keep = String(pp.span);
  sel.innerHTML = P_SPANS.map(s => `<option value="${s}">${pSpanLabel(pInterval() <= 50 ? Math.max(1, s / 3) : s)}</option>`).join('');
  sel.value = keep;
}

const pRedraw = () => { for (const f of perfDrawers) f(); };
function setInterval_(ms) {
  if (!P_INTERVALS.includes(ms)) return;
  savePref('perf.interval', ms);
  if (pp.meta) { pp.meta.interval = ms; fillSpans(); }
  $('pctl-rate').value = String(ms);
  postPerf({ interval: ms });
  pRedraw();
}
function setSpan(s) {
  if (!P_SPANS.includes(s)) return;
  pp.span = s;
  savePref('perf.span', s);
  $('pctl-span').value = String(s);
  pRedraw();
}
function setSource(src) {
  savePref('perf.source', src);
  for (const b of $('pctl-source').querySelectorAll('button')) b.classList.toggle('on', b.dataset.v === src);
  if (pp.meta) pp.meta.source = src;
  postPerf({ source: src });
  pRedraw();
}
function setFlag(key, on) {
  pp[key] = on;
  savePref('perf.' + key, on);
  if (key === 'avg') $('pctl-avg').checked = on;
  if (key === 'threads') for (const b of $('pctl-threads').querySelectorAll('button')) b.classList.toggle('on', (b.dataset.v === '1') === on);
  pRedraw();
}
function setPaused(on) {
  pp.paused = on;
  for (const id of ['pctl-pause', 'rctl-pause']) $(id).textContent = on ? 'Resume' : 'Pause';
  if (!on) {   // back to now
    const s = pm.get('local');
    if (s && s.cpu && s.cpu.n) pp.tEnd = s.cpu.T(s.cpu.n - 1);
  }
  pRedraw();
}
function toggleTop() {
  if (!pp.desktop) return;
  pp.onTop = !pp.onTop;
  $('pctl-top').classList.toggle('on', pp.onTop);
  $('pctl-top').setAttribute('aria-pressed', String(pp.onTop));
  savePref('perf.onTop', pp.onTop);
  try { window.pywebview.api.set_on_top(pp.onTop); } catch { /* not in the desktop app */ }
}
const stepOf = (list, value, dir) => list[Math.max(0, Math.min(list.length - 1, list.indexOf(value) + dir))];

$('pctl-rate').addEventListener('change', ev => setInterval_(Number(ev.target.value)));
$('pctl-span').addEventListener('change', ev => setSpan(Number(ev.target.value)));
$('pctl-source').addEventListener('click', ev => { const b = ev.target.closest('button[data-v]'); if (b) setSource(b.dataset.v); });
$('pctl-threads').addEventListener('click', ev => { const b = ev.target.closest('button[data-v]'); if (b) setFlag('threads', b.dataset.v === '1'); });
$('pctl-avg').addEventListener('change', ev => setFlag('avg', ev.target.checked));
for (const id of ['pctl-pause', 'rctl-pause']) $(id).addEventListener('click', () => setPaused(!pp.paused));
$('pctl-top').addEventListener('click', toggleTop);

document.addEventListener('keydown', ev => {
  if (ev.ctrlKey || ev.altKey || ev.metaKey || document.querySelector('dialog[open]')) return;
  if (ev.target.closest && ev.target.closest('input, select, textarea')) return;
  const k = ev.key.length === 1 ? ev.key.toLowerCase() : ev.key;
  if (k === 't') { ev.preventDefault(); toggleTop(); return; }
  if (!['overview', 'cpu', 'disk', 'network', 'remotes'].includes(activeTab)) return;
  if (ev.target.closest && ev.target.closest('button, [role=tablist]') && [' ', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(k)) return;
  const onCpu = activeTab === 'cpu';
  const handled = {
    ' ': () => setPaused(!pp.paused),
    ArrowLeft: () => setSpan(stepOf(P_SPANS, pp.span, -1)),
    ArrowRight: () => setSpan(stepOf(P_SPANS, pp.span, +1)),
    a: () => setFlag('avg', !pp.avg),
    // Up and Down scroll the list of machines; elsewhere they change the rate
    ArrowUp: activeTab !== 'remotes' && (() => setInterval_(stepOf(P_INTERVALS, pInterval(), -1))),
    ArrowDown: activeTab !== 'remotes' && (() => setInterval_(stepOf(P_INTERVALS, pInterval(), +1))),
    c: onCpu && (() => setFlag('threads', !pp.threads)),
    m: onCpu && viewMachine() === 'local' && (() => setSource(pp.meta && pp.meta.source === 'utility' ? 'cputime' : 'utility')),
  }[k];
  if (!handled) return;
  ev.preventDefault();
  handled();
});

// The desktop app adds Always on top (T).
function desktopReady() {
  if (!(window.pywebview && window.pywebview.api && window.pywebview.api.set_on_top)) return;
  pp.desktop = true;
  $('pctl-top').hidden = false;
  document.body.classList.add('desktop');
  if (loadPref('perf.onTop') === true) toggleTop();
}
window.addEventListener('pywebviewready', desktopReady);

// ---------------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------------
pp.span = P_SPANS.includes(loadPref('perf.span')) ? loadPref('perf.span') : 60;
for (const key of ['threads', 'avg']) {
  const v = loadPref('perf.' + key);
  if (typeof v === 'boolean') setFlag(key, v);
}
fillSpans();
readPerfColors();
darkQuery.addEventListener('change', () => { readPerfColors(); pRedraw(); });
desktopReady();
