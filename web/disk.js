'use strict';

// ---------------------------------------------------------------------------
// The Disk page's panels, one per disk: two graphs, one above the other, each with a
// scale of its own: throughput (reads and writes), then IOPS; each line with a line to
// its label. Then bars for the average I/O size and queue depth, and two stacked bars,
// latency (reads, writes) and busy time (reading, writing), each part named.
//
// A label's dot hides its line or shows it again. A click on its name shows that measure
// by process instead: a stacked graph of the four processes that did the most of it, named
// inside the graph, and the rest together as Other (perfmon "procio" events).
//
// The figures of a sample, as perfmon.DiskRates makes them:
//   0 busy %   1 read B/s   2 written B/s   3 reads/s   4 writes/s
//   5 ms per read   6 ms per write   7 queue depth   8 busy % reading
// Uses perf.js's samples and drawing. Every top-level name here starts with dk or DK.
// ---------------------------------------------------------------------------
const DK = { head: 22, label: 148, reach: 44, bar: 62, stack: 132, gap: 14, smooth: 1, numbers: 3, bars: 5, min: 300, max: 460,
  row: 27, tight: 15, dot: 15, procs: 4, keep: 330, split: 10 };
const dkOne = () => 1;
// Averages are of what happened, not of the samples: latency counts each operation the same.
const DK_SERIES = [
  { key: 'tr', name: 'Read throughput', unit: 'bytes', num: (r, k) => r.V(1, k), den: dkOne, proc: 2 },
  { key: 'tw', name: 'Write throughput', unit: 'bytes', num: (r, k) => r.V(2, k), den: dkOne, dash: true, proc: 3 },
  { key: 'ir', name: 'Read IOPS', unit: 'iops', num: (r, k) => r.V(3, k), den: dkOne, proc: 4, bytes: 2 },
  { key: 'iw', name: 'Write IOPS', unit: 'iops', num: (r, k) => r.V(4, k), den: dkOne, dash: true, proc: 5, bytes: 3 },
  { key: 'lr', name: 'Read latency', unit: 'ms', num: (r, k) => r.V(5, k) * r.V(3, k), den: (r, k) => r.V(3, k) },
  { key: 'lw', name: 'Write latency', unit: 'ms', num: (r, k) => r.V(6, k) * r.V(4, k), den: (r, k) => r.V(4, k) },
  { key: 'br', name: 'Busy reading', unit: 'busy', num: (r, k) => r.V(8, k), den: dkOne },
  { key: 'bw', name: 'Busy writing', unit: 'busy', num: (r, k) => Math.max(0, r.V(0, k) - r.V(8, k)), den: dkOne },
];
const DK_LINES = DK_SERIES.slice(0, 4), DK_LAT = DK_SERIES.slice(4, 6), DK_BUSY = DK_SERIES.slice(6, 8);
const DK_CHARTS = [DK_SERIES.slice(0, 2), DK_SERIES.slice(2, 4)];   // throughput above, IOPS below
const dkSeries = key => DK_SERIES.find(q => q.key === key);
const DK_SIZE = { num: (r, k) => r.V(1, k) + r.V(2, k), den: (r, k) => r.V(3, k) + r.V(4, k) };
const DK_QUEUE = { num: (r, k) => r.V(7, k), den: dkOne };
const DK_LEAST = { bytes: 1e6, iops: 10, ms: 1, busy: 100 };   // the lowest top of each scale
// OKLCH: a hue for each measure, the write a step lighter (and dashed, on the graph)
const DK_PAL = {
  light: { tr: [0.48, 0.19, 296], tw: [0.60, 0.17, 318], ir: [0.50, 0.14, 250], iw: [0.60, 0.11, 222], lr: [0.55, 0.14, 60],
           lw: [0.70, 0.15, 42], br: [0.50, 0.12, 155], bw: [0.68, 0.13, 135], size: [0.55, 0.10, 220], queue: [0.60, 0.14, 55],
           p0: [0.55, 0.15, 255], p1: [0.64, 0.15, 155], p2: [0.72, 0.15, 70], p3: [0.58, 0.17, 335], other: [0.66, 0.01, 250] },
  dark: { tr: [0.68, 0.17, 296], tw: [0.78, 0.15, 322], ir: [0.68, 0.14, 250], iw: [0.80, 0.10, 218], lr: [0.82, 0.15, 82],
          lw: [0.70, 0.16, 48], br: [0.70, 0.15, 155], bw: [0.84, 0.15, 128], size: [0.72, 0.10, 220], queue: [0.80, 0.13, 70],
          p0: [0.68, 0.14, 255], p1: [0.74, 0.15, 155], p2: [0.82, 0.14, 75], p3: [0.72, 0.16, 335], other: [0.55, 0.01, 250] },
};
let dkColors = {}, dkHot = null;   // dkHot: { id, key } of the label pointed at
// The measures hidden from each disk's graph, by "machine|disk": a label's dot hides or shows one.
const dkHidden = new Map(Object.entries(loadPref('disk.hidden') || {}).filter(([, v]) => Array.isArray(v)).map(([k, v]) => [k, new Set(v)]));
const dkId = (s, item) => `${s.info.id}|${item.key}`;
const dkOff = (s, item) => dkHidden.get(dkId(s, item)) || new Set();
// The disks whose graph shows a measure by process: "machine|disk" -> the measure's key.
const dkProc = new Map();

function dkToggle(id, key) {
  const off = dkHidden.get(id) || new Set();
  if (off.has(key)) off.delete(key); else off.add(key);
  if (off.size) dkHidden.set(id, off); else dkHidden.delete(id);
  savePref('disk.hidden', Object.fromEntries([...dkHidden].slice(-200).map(([k, v]) => [k, [...v]])));
}

// A measure by process, or back to all of them (the same measure again).
function dkProcToggle(id, key) {
  if (dkProc.get(id) === key) dkProc.delete(id); else dkProc.set(id, key);
  dkProcTell();
}

function dkReadColors() {
  const pal = DK_PAL[darkQuery.matches ? 'dark' : 'light'];
  dkColors = Object.fromEntries(Object.entries(pal).map(([k, c]) => [k, rgb(oklch(...c))]));
}
dkReadColors();
darkQuery.addEventListener('change', dkReadColors);

// ---------------------------------------------------------------------------
// Processes' disk I/O, while a graph shows it: the server sends it only for machines
// whose Disk page shows a measure by process, and is told again now and then.
// { at, per: "disk" | "all", e: error } as s.procio, and each disk's last minutes as
// s.procHist: key -> [{ t, rows: [[name, pid, read B/s, written B/s, reads/s or null, writes/s or null]] }].
// ---------------------------------------------------------------------------
let dkProcTold = '';
function dkProcTell(force = false) {
  const id = typeof viewMachine === 'function' ? viewMachine() : 'local';
  const on = activeTab === 'disk' && [...dkProc.keys()].some(k => k.startsWith(`${id}|`));
  const what = on ? id : '';
  if (!force && what === dkProcTold) return;
  dkProcTold = what;
  fetch('/api/procio', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                         body: JSON.stringify({ machine: on ? id : null }) }).catch(() => { /* told again soon */ });
}
setInterval(() => { if (dkProcTold) dkProcTell(true); }, 10000);   // still wanted (a restarted server forgets)
tabListeners.push(() => dkProcTell());

function dkOnProcio(msg) {
  const s = pStore(msg.m);
  s.procio = { at: performance.now(), per: msg.per || 'disk', e: msg.e || '' };
  s.procHist = s.procHist || new Map();
  for (const [key, rows] of Object.entries(msg.disks || {})) {
    const h = s.procHist.get(key) || [];
    h.push({ t: msg.t, rows });
    while (h.length && h[0].t < msg.t - DK.keep) h.shift();
    s.procHist.set(key, h);
  }
  for (const f of perfDrawers) f();
}
perfListeners.procio.push(dkOnProcio);

// ---------------------------------------------------------------------------
// Numbers
// ---------------------------------------------------------------------------
const dkIops = v => (v >= 1e6 ? `${(v / 1e6).toFixed(2)}M` : v >= 1e4 ? `${(v / 1e3).toFixed(1)}k` : v >= 100 ? `${Math.round(v)}` : v >= 10 ? v.toFixed(0) : v.toFixed(1));
const dkMs = v => `${v >= 100 ? Math.round(v) : v >= 10 ? v.toFixed(1) : v.toFixed(2)} ms`;
const dkSize = v => (v >= 1048576 ? `${trim2(+(v / 1048576).toFixed(1))} MB` : v >= 1024 ? `${v >= 102400 ? Math.round(v / 1024) : (v / 1024).toFixed(1)} KB` : `${Math.round(v)} B`);
const dkQueue = v => (v >= 100 ? Math.round(v) : v >= 10 ? v.toFixed(1) : v.toFixed(2));
const DK_FORMAT = { bytes: pBytes, iops: v => `${dkIops(v)} IOPS`, ms: dkMs, busy: v => `${v.toFixed(1)}%` };
const DK_TOP = { bytes: pAxisBytes, iops: v => `${dkIops(v).replace(/\.0+(?=k|M|$)/, '')} IOPS`, ms: v => `${trim2(v)} ms`, busy: () => '100% busy' };

// The average over the last seconds: what happened, over how many. null if nothing did.
function dkAverage(ring, seconds, { num, den }) {
  let k = ring.after(pp.tEnd) - 1, a = 0, b = 0;
  if (k < 0) return null;
  const from = ring.T(k) - seconds;
  for (; k >= 0 && ring.T(k) > from; k--) { a += num(ring, k); b += den(ring, k); }
  return b > 1e-9 ? a / b : null;
}

// What a line shows at sample k: the average over DK.smooth seconds up to it. null if nothing happened.
function dkAt(ring, k, { num, den }) {
  let a = 0, b = 0;
  for (let j = k, from = ring.T(k) - DK.smooth; j >= 0 && ring.T(j) > from; j--) { a += num(ring, j); b += den(ring, j); }
  return b > 1e-6 ? Math.max(0, a / b) : null;
}

// The same for every sample [from, to), over DK.smooth seconds up to each.
function dkTrail(ring, from, to, { num, den }) {
  const out = new Float32Array(Math.max(0, to - from));
  let j = from, a = 0, b = 0;
  while (j > 0 && ring.T(j - 1) > ring.T(from) - DK.smooth) j--;
  for (let k = j; k < from; k++) { a += num(ring, k); b += den(ring, k); }
  for (let i = from; i < to; i++) {
    a += num(ring, i);
    b += den(ring, i);
    while (ring.T(j) <= ring.T(i) - DK.smooth) { a -= num(ring, j); b -= den(ring, j); j++; }
    out[i - from] = b > 1e-6 ? Math.max(0, a / b) : 0;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Drawing
// ---------------------------------------------------------------------------
function dkDash(on) { pctx.setLineDash(on ? [5, 3] : []); }

// A line of smoothed values: every one when sparse, the mean of each pixel column when dense.
function dkLine(ring, v, from, to, p, span, top) {
  const g = pctx, per = p.w / span, right = p.x + p.w, bottom = p.y + p.h - 1, scale = (p.h - 2) / top;
  const X = k => right - (pp.tEnd - ring.T(k)) * per, Y = x => bottom - Math.min(x, top) * scale;
  let pen = false;
  g.beginPath();
  const at = (x, y) => { if (pen) g.lineTo(x, y); else { g.moveTo(x, y); pen = true; } };
  if (to - from <= p.w) {
    for (let k = from; k < to; k++) at(X(k), Y(v[k - from]));
  } else {
    let col = null, sum = 0, n = 0;
    for (let k = from; k < to; k++) {
      const c = Math.floor(X(k));
      if (c !== col) { if (n) at(col + 0.5, Y(sum / n)); col = c; sum = n = 0; }
      sum += v[k - from];
      n++;
    }
    if (n) at(col + 0.5, Y(sum / n));
  }
  return to > from ? Y(v[to - from - 1]) : bottom;
}

function dkPanel(s, item, ring, p, span) {
  const id = dkId(s, item), proc = dkProc.get(id), pq = proc && dkSeries(proc);
  const full = item.size ? `${pBytesSize(item.used)} of ${pBytesSize(item.size)} used (${Math.round(100 * item.used / item.size)}%)` : '';
  const nw = pText(item.name, p.x, p.y + 15, 13.5, T.ink, 600, 'left', p.w * 0.4);
  const detail = [item.detail, full, item.status].filter(Boolean).join('  ·  ');
  if (detail) pText(detail, p.x + nw + 10, p.y + 15, 12, item.status ? PC.warn : T.muted, 400, 'left', p.w - nw - 20);

  const barsW = 2 * DK.bar + 2 * DK.stack + 3 * DK.gap, right = p.x + p.w;
  const plot = { x: p.x, y: p.y + DK.head, w: Math.max(120, p.w - DK.reach - DK.label - DK.gap - barsW), h: p.h - DK.head };
  const from = ring ? Math.max(0, ring.after(pp.tEnd - span) - 1) : 0, to = ring ? ring.after(pp.tEnd) : 0;
  const off = dkOff(s, item), hot = dkHot && dkHot.id === id && !off.has(dkHot.key) ? dkHot.key : null;
  // two graphs, one above the other, each with a scale of its own: throughput, then IOPS
  const h1 = Math.floor((plot.h - DK.split) / 2);
  DK_CHARTS.forEach((series, c) => {
    const g = { ...plot, y: plot.y + c * (h1 + DK.split), h: c ? plot.h - h1 - DK.split : h1 };
    const mine = pq && series.includes(pq);
    let ends = series.map(() => g.y + g.h - 1);
    if (mine) dkProcGraph(s, item, ring, g, span, pq, from, to);
    else ends = dkGraph(s, item, ring, g, span, from, to, off, series.includes(hot) ? hot : null, series);
    dkLabels(s, item, ring, g, ends, mine ? null : series.includes(hot) ? hot : null, off, id, mine ? proc : null, series);
  });
  const bx = right - barsW, live = ring && ring.n && pLive(s), b = { y: plot.y, h: plot.h };
  const size = live ? dkAverage(ring, DK.bars, DK_SIZE) : null, queue = live ? dkAverage(ring, DK.bars, DK_QUEUE) : null;
  dkSizeBar({ ...b, x: bx, w: DK.bar }, size);
  dkQueueBar({ ...b, x: bx + DK.bar + DK.gap, w: DK.bar }, queue, ring, from, to);
  const avg = q => (live ? dkAverage(ring, DK.bars, q) : null);
  let most = 0;                                    // the latency scale: what the last while needed
  if (ring && to > from) {
    const r = dkTrail(ring, from, to, DK_LAT[0]), w = dkTrail(ring, from, to, DK_LAT[1]);
    for (let i = 0; i < r.length; i++) most = Math.max(most, r[i] + w[i]);
  }
  const lat = DK_LAT.map(q => ({ key: q.key, name: q.name.replace(' latency', 's'), value: avg(q) }));
  const latTop = niceCeil(Math.max(DK_LEAST.ms, most * 1.1, lat.reduce((t, x) => t + (x.value || 0), 0) * 1.1));
  dkStack({ ...b, x: bx + 2 * (DK.bar + DK.gap), w: DK.stack }, 'Latency', lat, latTop, dkMs, live ? '' : '--');
  const busy = DK_BUSY.map(q => ({ key: q.key, name: q.name.replace('Busy ', '').replace(/^./, c => c.toUpperCase()), value: avg(q) }));
  const total = busy.reduce((t, x) => t + (x.value || 0), 0);
  dkStack({ ...b, x: bx + 2 * (DK.bar + DK.gap) + DK.stack + DK.gap, w: DK.stack }, 'Busy', busy, 100, v => `${v.toFixed(1)}%`,
          live ? `${total.toFixed(1)}%` : '--', true);
}

// One graph's lines (series). Returns where each ends, for the lines to their labels.
function dkGraph(s, item, ring, plot, span, from, to, off, hot, series) {
  const lines = [], tops = { ...DK_LEAST };
  if (ring && to > from) {
    for (const q of series) {
      const v = off.has(q.key) ? null : dkTrail(ring, from, to, q);
      lines.push(v);
      if (v) for (const x of v) if (x > tops[q.unit]) tops[q.unit] = x;
    }
  }
  for (const u of ['bytes', 'iops']) tops[u] = niceCeil(tops[u]);
  pframe(plot, 4, span, true);
  const ends = [];
  clipTo(plot);
  series.forEach((q, i) => {
    if (!lines[i]) { ends.push(plot.y + plot.h - 1); return; }
    ends.push(dkLine(ring, lines[i], from, to, plot, span, tops[q.unit]));
    pctx.globalAlpha = pDim * (hot && hot !== q.key ? 0.18 : 1);
    pctx.strokeStyle = dkColors[q.key];
    pctx.lineWidth = hot === q.key ? 2.6 : 1.6;
    pctx.lineJoin = 'round';
    dkDash(q.dash);
    pctx.stroke();
  });
  dkDash(false);
  pctx.globalAlpha = pAlpha = pDim;
  dkMarks(s, item, ring, plot, span, lines, from, to, tops, series);
  pctx.restore();
  border(plot);
  const unit = series[0].unit, shown = series.some(q => !off.has(q.key));
  const scale = shown ? `${unit === 'bytes' ? 'Throughput' : 'IOPS'}  ·  top ${DK_TOP[unit](tops[unit])}  ·  click a name to see which processes`
    : `${unit === 'bytes' ? 'Throughput' : 'IOPS'}: both lines hidden; click a dot to show one`;
  pText(scale, plot.x + 6, plot.y + 13, 10.5, T.muted, 400, 'left', plot.w - 12);
  PS.plots.push({ ...plot, kind: 'disk', s, item, ring });
  return ends;
}

// Where the pointer is: a dot on each line shown, at the moment it points at.
function dkMarks(s, item, ring, plot, span, lines, from, to, tops, series) {
  const hv = PS.hover;
  if (!hv || hv.t == null || hv.id !== s.info.id || hv.key !== item.key || !ring || to <= from) return;
  const k = nearest(ring, hv.t);
  if (k < from || k >= to) return;
  const x = plot.x + plot.w - (pp.tEnd - hv.t) * plot.w / span, bottom = plot.y + plot.h - 1;
  series.forEach((q, i) => {
    if (!lines[i]) return;
    const y = bottom - Math.min(lines[i][k - from], tops[q.unit]) * (plot.h - 2) / tops[q.unit];
    pctx.beginPath();
    pctx.arc(x, y, 3.6, 0, 2 * Math.PI);
    pctx.fillStyle = dkColors[q.key];
    pctx.fill();
    pctx.lineWidth = 1.5;
    pctx.strokeStyle = PC.plot;
    pctx.stroke();
  });
}

// A label for each line, in a fixed order, with a line from where it ends now. Its dot hides the
// line or shows it again (a hidden one is faint, with no line to it); its name shows the measure
// by process, or, when it already does, all of them again.
function dkLabels(s, item, ring, plot, ends, hot, off, id, proc, series) {
  const n = series.length, x = plot.x + plot.w + DK.reach, two = plot.h >= n * DK.row - 4;
  const step = plot.h / n, live = ring && ring.n && pLive(s);
  series.forEach((q, i) => {
    const color = dkColors[q.key], shelf = Math.round(plot.y + step * (i + 1) - (step - (two ? DK.row : DK.tight)) / 2) - 1.5;
    const hidden = off.has(q.key), chosen = proc === q.key;
    const v = live ? dkAverage(ring, DK.numbers, q) : null;
    const value = v == null ? '--' : DK_FORMAT[q.unit](v);
    pctx.globalAlpha = pDim * (proc ? (chosen ? 1 : 0.45) : hidden ? 0.35 : hot && hot !== q.key ? 0.3 : 1);
    pctx.strokeStyle = color;
    pctx.lineWidth = hot === q.key || chosen ? 2 : 1.25;
    dkDash(q.dash);
    if (!hidden && !proc) {
      pctx.beginPath();
      pctx.moveTo(plot.x + plot.w + 3, ends[i]);
      pctx.lineTo(x, shelf);
      pctx.stroke();
    }
    dkDash(false);
    pctx.beginPath();
    pctx.moveTo(x, shelf);
    pctx.lineTo(x + DK.label, shelf);
    pctx.stroke();
    if (!hidden && !proc) {
      pctx.beginPath();
      pctx.arc(plot.x + plot.w + 1, ends[i], 2.6, 0, 2 * Math.PI);
      pctx.fillStyle = color;
      pctx.fill();
    }
    // the dot that hides or shows the line: filled while it shows
    const dy = shelf - (two ? 18.5 : 7), dx = x + 5.5;
    pctx.beginPath();
    pctx.arc(dx, dy, 4.5, 0, 2 * Math.PI);
    pctx.lineWidth = 1.6;
    pctx.strokeStyle = color;
    pctx.stroke();
    if (!hidden) { pctx.fillStyle = color; pctx.fill(); }
    const name = chosen ? `${q.name} by process` : q.name, tx = x + DK.dot;
    if (two) {
      pText(name, tx, shelf - 14.5, 11.5, T.ink, 600, 'left', DK.label - DK.dot - 2);
      pText(value, tx, shelf - 3.5, 11, T.ink2, 400, 'left', DK.label - DK.dot - 2);
    } else {
      const vw = pText(value, x + DK.label - 2, shelf - 3, 11, T.ink2, 600, 'right');
      pText(name, tx, shelf - 3, 11, T.ink, 400, 'left', DK.label - vw - DK.dot - 6);
    }
    const top = shelf - (two ? DK.row : DK.tight), h = two ? DK.row : DK.tight;
    PS.plots.push({ x: x - 4, y: top, w: DK.dot, h, kind: 'diskdot', id, key: q.key, name: q.name, hidden });
    PS.plots.push({ x: tx - 1, y: top, w: DK.label - DK.dot + 5, h, kind: 'disklabel', id, key: q.key, name: q.name, hidden, chosen });
  });
  pctx.globalAlpha = pAlpha = pDim;
}

// An upright bar with its name above, its value below, and ticks beside it. at: 0 to 1.
function dkBar(b, name, value, at, color, ticks, caption = `${DK.bars} s average`, captionColor = T.muted) {
  const tx = 27, top = b.y + 18, bottom = b.y + b.h - 32, h = Math.max(10, bottom - top), bx = b.x + tx, bw = b.w - tx;
  pText(name, b.x + b.w, b.y + 10, 11.5, T.ink, 600, 'right');
  dkTicks(bx, bw, bottom, h, ticks);
  const f = Math.max(0, Math.min(1, at)), fh = h * f;
  if (fh > 0.5) {
    pctx.fillStyle = color;
    pctx.beginPath();
    pctx.roundRect(bx + 4, bottom - fh, bw - 8, fh, [Math.min(4, fh), Math.min(4, fh), 0, 0]);
    pctx.fill();
  }
  border({ x: bx, y: top, w: bw, h });
  pText(value, b.x + b.w, bottom + 15, 12, T.ink, 600, 'right');
  pText(caption, b.x + b.w, bottom + 28, 9.5, captionColor, 400, 'right');
}

function dkTicks(bx, bw, bottom, h, ticks) {
  pctx.fillStyle = PC.plot;
  pctx.fillRect(bx, bottom - h, bw, h);
  for (const [f, label] of ticks) {
    const y = Math.round(bottom - h * f) + 0.5;
    pctx.beginPath();
    pctx.moveTo(bx - 4, y);
    pctx.lineTo(bx + bw, y);
    pctx.strokeStyle = T.grid;
    pctx.lineWidth = 1;
    pctx.stroke();
    pText(label, bx - 6, y + 3.5, 9.5, T.muted, 400, 'right');
  }
}

// Parts stacked in one bar, a gap between each, each named beside it with a line to its part:
// parts [{ key, name, value }]. total: what's written at the top of the stack ('' for nothing);
// under the bar, the seconds averaged.
function dkStack(b, name, parts, top, fmt, total, percent = false) {
  const tx = 27, bw = 30, top0 = b.y + 18, bottom = b.y + b.h - 32, h = Math.max(10, bottom - top0), bx = b.x + tx;
  pText(name, bx, b.y + 10, 11.5, T.ink, 600, 'left');
  dkTicks(bx, bw, bottom, h, [0, 0.25, 0.5, 0.75, 1].map(f => [f, percent ? `${Math.round(top * f)}` : trim2(top * f)]));
  let y = bottom;
  const mids = [];
  const shown = parts.filter(x => x.value > 0);
  parts.forEach(x => {
    const sh = Math.min(y - top0, h * Math.max(0, x.value || 0) / top);
    if (sh > 0.5) {
      const last = x === shown[shown.length - 1];
      pctx.fillStyle = dkColors[x.key];
      pctx.beginPath();
      pctx.roundRect(bx + 4, y - sh + (last ? 0 : 1), bw - 8, Math.max(0.5, sh - (last ? 0 : 2)), last ? [Math.min(4, sh), Math.min(4, sh), 0, 0] : 0);
      pctx.fill();
    }
    mids.push(y - sh / 2);
    y -= sh;
  });
  border({ x: bx, y: top0, w: bw, h });
  if (total) {
    const tw = pWidth(total, 11, 600) + 8, ty = Math.max(top0 + 2, y - 15);
    pctx.fillStyle = PC.bg;
    pctx.globalAlpha = 0.85 * pDim;
    pctx.fillRect(bx + bw / 2 - tw / 2, ty, tw, 13);
    pctx.globalAlpha = pDim;
    pText(total, bx + bw / 2, ty + 10.5, 11, T.ink, 600, 'center');
  }
  const lx = bx + bw + 8, shelves = dkSpread(mids.map(m => m + 4), top0 + 12, bottom + 4, 24);
  parts.forEach((x, i) => {
    const sy = shelves[i];
    pctx.strokeStyle = dkColors[x.key];
    pctx.lineWidth = 1.25;
    pctx.beginPath();
    pctx.moveTo(bx + bw - 3, mids[i]);
    pctx.lineTo(lx, sy - 4);
    pctx.lineTo(lx + 6, sy - 4);
    pctx.stroke();
    const room = b.x + b.w - lx - 9;
    pText(x.name, lx + 9, sy - 6, 10.5, T.ink, 600, 'left', room);
    pText(x.value == null ? 'no I/O' : fmt(x.value), lx + 9, sy + 5.5, 10.5, T.ink2, 400, 'left', room);
  });
  pText(`${DK.bars} s average`, bx + bw, bottom + 15, 9.5, T.muted, 400, 'right');
}

// Places for labels that want to be at `want` (each), at least `gap` apart, inside [lo, hi], in the same order (bottom up).
function dkSpread(want, lo, hi, gap) {
  const n = want.length, out = want.map(y => Math.max(lo, Math.min(hi, y)));
  for (let i = 1; i < n; i++) out[i] = Math.min(out[i], out[i - 1] - gap);
  const short = n ? lo - out[n - 1] : 0;
  if (short > 0) for (let i = 0; i < n; i++) out[i] += short;
  return out;
}

// Sizes double from 512 bytes to 1 MB, or as far as it takes.
function dkSizeBar(b, v) {
  let top = 1048576;
  while (v > top) top *= 4;
  const log = x => Math.log2(Math.max(512, x) / 512) / Math.log2(top / 512), ticks = [];
  for (let x = 4096; x <= top; x *= 4) ticks.push([log(x), x >= 1048576 ? `${x / 1048576}M` : `${x / 1024}K`]);
  dkBar(b, 'I/O size', v == null ? 'no I/O' : dkSize(v), v == null ? 0 : log(v), dkColors.size, ticks);
}

function dkQueueBar(b, v, ring, from, to) {
  let most = v || 0, top = 1;
  if (ring && to > from) for (const x of dkTrail(ring, from, to, DK_QUEUE)) if (x > most) most = x;
  while (top < most) top *= 2;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(f => [f, trim2(top * f)]);
  dkBar(b, 'Queue', v == null ? '--' : dkQueue(v), (v || 0) / top, dkColors.queue, ticks);
}

// ---------------------------------------------------------------------------
// A measure by process: stacked, the four that did the most of it over the time shown
// at the bottom, the rest together as Other on top, each named inside its band where
// it's thickest (or along the top, if it's too thin there).
// ---------------------------------------------------------------------------
function dkProcHist(s, item) {
  const h = s.procHist;
  return h ? h.get(item.key) || h.get('*') || null : null;
}

// What the graph shows: { samples: [{ t, vals }], names (the four, then Other), est, top, latest } or null.
function dkProcData(s, item, ring, span, q) {
  const hist = dkProcHist(s, item);
  if (!hist || !hist.length) return null;
  const t0 = pp.tEnd - span - 2, samples = hist.filter(x => x.t > t0 && x.t <= pp.tEnd + 1);
  if (!samples.length) return null;
  // IOPS that only the bytes tell of (Linux counts no operations per process): the disk's, shared out by the bytes
  const est = q.unit === 'iops' && samples.some(x => x.rows.some(r => r[q.proc] == null));
  const value = (x, r) => {
    if (!est) return r[q.proc] || 0;
    const bytes = x.rows.reduce((t, o) => t + (o[q.bytes] || 0), 0), k = ring ? nearest(ring, x.t) : -1;
    const disk = k >= 0 ? dkAt(ring, k, q) || 0 : 0;
    return bytes > 0 ? disk * (r[q.bytes] || 0) / bytes : 0;
  };
  const sums = new Map();
  for (const x of samples) for (const r of x.rows) if (r[0] !== '(other)') sums.set(r[0], (sums.get(r[0]) || 0) + value(x, r));
  const names = [...sums].filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]).slice(0, DK.procs).map(([n]) => n);
  let top = DK_LEAST[q.unit];
  const out = samples.map(x => {
    const vals = names.map(() => 0);
    let other = 0;
    for (const r of x.rows) {
      const v = value(x, r), i = names.indexOf(r[0]);
      if (i >= 0) vals[i] += v; else other += v;
    }
    vals.push(other);
    top = Math.max(top, vals.reduce((a, b) => a + b, 0));
    return { t: x.t, vals };
  });
  return { samples: out, names: [...names, 'Other'], est, top: niceCeil(top * 1.08), latest: out[out.length - 1] };
}

function dkProcGraph(s, item, ring, plot, span, q, from, to) {
  pframe(plot, 4, span, true);
  const d = dkProcData(s, item, ring, span, q), pio = s.procio && performance.now() - s.procio.at < 5000 ? s.procio : null;
  if (d) {
    const per = plot.w / span, X = t => plot.x + plot.w - (pp.tEnd - t) * per, scale = (plot.h - 2) / d.top, bottom = plot.y + plot.h - 1;
    const n = d.names.length, cum = d.samples.map(x => { const c = [0]; x.vals.forEach(v => c.push(c[c.length - 1] + v)); return c; });
    clipTo(plot);
    for (let b = 0; b < n; b++) {
      pctx.beginPath();
      d.samples.forEach((x, i) => { const px = X(x.t), py = bottom - cum[i][b + 1] * scale; if (i) pctx.lineTo(px, py); else pctx.moveTo(px, py); });
      for (let i = d.samples.length - 1; i >= 0; i--) pctx.lineTo(X(d.samples[i].t), bottom - cum[i][b] * scale);
      pctx.closePath();
      pctx.fillStyle = b === n - 1 ? dkColors.other : dkColors[`p${b}`];
      pctx.globalAlpha = 0.85 * pDim;
      pctx.fill();
      pctx.globalAlpha = pDim;
      pctx.strokeStyle = PC.plot;                  // a thin gap between bands
      pctx.lineWidth = 1;
      pctx.stroke();
    }
    // names: inside each band where it's thickest, if there's room
    const along = [];
    for (let b = 0; b < n; b++) {
      let best = -1, thick = 0;
      d.samples.forEach((x, i) => { const th = x.vals[b] * scale; if (th > thick && X(x.t) > plot.x + 30) { thick = th; best = i; } });
      if (best < 0 || !d.samples.some(x => x.vals[b] > 0)) continue;
      const text = d.names[b], tw = pWidth(text, 11, 600) + 10;
      if (thick >= 15) {
        const cx = Math.max(plot.x + tw / 2 + 2, Math.min(plot.x + plot.w - tw / 2 - 2, X(d.samples[best].t)));
        const cy = bottom - (cum[best][b] + cum[best][b + 1]) / 2 * scale;
        pctx.fillStyle = PC.bg;
        pctx.globalAlpha = 0.72 * pDim;
        pctx.beginPath();
        pctx.roundRect(cx - tw / 2, cy - 8, tw, 15, 4);
        pctx.fill();
        pctx.globalAlpha = pDim;
        pText(text, cx, cy + 3.5, 11, T.ink, 600, 'center');
      } else along.push(b);
    }
    pctx.restore();
    // the key along the top: every band, with what it's doing now
    let kx = plot.x + 8;
    const ky = plot.y + 14;
    d.names.forEach((name, b) => {
      const v = d.latest.vals[b], text = `${name} ${DK_FORMAT[q.unit](v)}`;
      const tw = pWidth(text, 10.5, along.includes(b) ? 600 : 400);
      if (kx + tw + 18 > plot.x + plot.w - 110) return;
      pctx.fillStyle = b === n - 1 ? dkColors.other : dkColors[`p${b}`];
      pctx.fillRect(kx, ky - 8, 9, 9);
      pText(text, kx + 13, ky, 10.5, T.ink2, along.includes(b) ? 600 : 400, 'left');
      kx += tw + 26;
    });
    const note = [d.est ? 'shared out by each process\'s bytes (Linux counts no operations per process)' : '',
      pio && pio.per === 'all' ? 'processes\' I/O on every disk: Linux doesn\'t count it per disk' : ''].filter(Boolean).join(' · ');
    if (note) pText(note, plot.x + 8, plot.y + plot.h - 7, 10, T.muted, 400, 'left', plot.w - 16);
    pText(`top ${DK_TOP[q.unit](d.top)}`, plot.x + plot.w - 8, plot.y + plot.h - 7, 10, T.muted, 400, 'right');
  } else {
    const why = !pLive(s) ? '' : pio && pio.e ? pio.e : s.info.id !== 'local' && s.info.procio === false
      ? "Its agent doesn't tell which processes do disk I/O. Install this version's agent there to see it." : 'Finding out which processes…';
    pText(why, plot.x + plot.w / 2, plot.y + plot.h / 2, 12.5, why.startsWith('Finding') ? T.muted : PC.warn, 400, 'center', plot.w - 40);
  }
  border(plot);
  // back to all the measures
  const back = '✕ All measures', bw = pWidth(back, 11, 600) + 14, bx = plot.x + plot.w - bw - 6, by = plot.y + 4;
  pctx.fillStyle = PC.bg;
  pctx.beginPath();
  pctx.roundRect(bx, by, bw, 18, 5);
  pctx.fill();
  pctx.strokeStyle = PC.border;
  pctx.stroke();
  pText(back, bx + bw / 2, by + 13, 11, T.ink, 600, 'center');
  PS.plots.push({ x: bx, y: by, w: bw, h: 18, kind: 'diskback', id: dkId(s, item) });
  PS.plots.push({ ...plot, kind: 'diskproc', s, item, ring, q, span });
}

function dkProcTip(p, t) {
  const d = dkProcData(p.s, p.item, p.ring, p.span, p.q);
  if (!d) return '';
  let best = d.samples[0];
  for (const x of d.samples) if (Math.abs(x.t - t) < Math.abs(best.t - t)) best = x;
  const ago = Math.max(0, pp.tEnd - best.t);
  const rows = d.names.map((name, b) => `<div><span class="sw" style="background:${b === d.names.length - 1 ? dkColors.other : dkColors[`p${b}`]};border:0"></span>` +
    `${esc(name)} <b>${DK_FORMAT[p.q.unit](best.vals[b])}</b></div>`).reverse().join('');
  return `<div class="head">${esc(p.q.name)} by process <span class="faint">${esc(p.item.name)}</span></div>${rows}` +
    `<div class="faint">${ago < 1 ? 'now' : `${Math.round(ago)} s ago`}${d.est ? ' · estimated from each process\'s bytes' : ''}</div>`;
}

// What it all was at a moment: what the lines show there (each the last DK.smooth seconds), not
// the one sample under the pointer, which between bursts of I/O is mostly nothing.
function dkTip(p, t) {
  const ring = p.ring, k = nearest(ring, t);
  if (k < 0) return `<div class="head">${esc(p.item.name)}</div><div class="faint">No samples yet</div>`;
  const ago = Math.max(0, pp.tEnd - ring.T(k)), off = dkOff(p.s, p.item);
  const row = q => {
    const v = dkAt(ring, k, q);
    return `<div><span class="sw" style="background:${dkColors[q.key]};border:0"></span>${q.name} <b>${v == null ? 'no I/O' : DK_FORMAT[q.unit](v)}</b></div>`;
  };
  const size = dkAt(ring, k, DK_SIZE), queue = dkAt(ring, k, DK_QUEUE);
  const shown = DK_LINES.filter(q => !off.has(q.key));
  return `<div class="head">${esc(p.item.name)} <span class="faint">${esc(p.s.info.name)}</span></div>` +
    shown.map(row).join('') + DK_LAT.map(row).join('') + DK_BUSY.map(row).join('') +
    (off.size ? `<div class="faint">${off.size} hidden: click a dot to show it</div>` : '') +
    `<div class="sub">I/O size ${size == null ? '–' : dkSize(size)} · queue ${dkQueue(queue || 0)}</div>` +
    `<div class="faint">${DK.smooth} s up to ${ago < 0.05 ? 'now' : `${ago.toFixed(ago < 10 ? 1 : 0)} s ago`}</div>`;
}

// A label, its dot, and the way back: what a click does.
function dkLabelTip(p) {
  if (p.kind === 'diskdot') return `<div class="head">${esc(p.name)}</div><div class="faint">Click to ${p.hidden ? 'show its line' : 'hide its line'}</div>`;
  if (p.kind === 'diskback') return '<div class="faint">Back to all the measures</div>';
  return `<div class="head">${esc(p.name)}</div><div class="faint">${p.chosen ? 'Click to go back to all the measures'
    : 'Click to see which processes did it'}</div>`;
}

// How a page of them is laid out: { cols, rows, h (each), strip } for n disks in w x room.
function dkLayout(n, w, room, detailed) {
  const cols = w >= 1900 && n > 1 ? 2 : 1, rows = Math.ceil(n / cols) || 1;
  if (!detailed) return { cols, rows, h: Math.max(96, Math.min(200, (room - (rows - 1) * V.gap) / rows)) };
  return { cols, rows, h: Math.max(DK.min, Math.min(DK.max, (room - (rows - 1) * V.gap) / rows)) };
}

function dkPage(s, a, span, lay, detailed) {
  const items = s.info.disks, colGap = 28, w = (a.w - (lay.cols - 1) * colGap) / lay.cols;
  items.forEach((item, i) => {
    const p = { x: a.x + (i % lay.cols) * (w + colGap), y: a.y + Math.floor(i / lay.cols) * (lay.h + V.gap), w, h: lay.h };
    const ring = s.disks.get(item.key);
    if (detailed && (!ring || ring.ch >= 9)) dkPanel(s, item, ring, p, span);
    else {
      drawDiskStrip(s, item, ring, p, span);
      PS.plots.push({ ...p, kind: 'disk', s, item, ring });
    }
  });
}
