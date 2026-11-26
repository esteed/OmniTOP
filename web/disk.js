'use strict';

// ---------------------------------------------------------------------------
// The Disk page's panels, one per disk: throughput, IOPS, latency and busy time,
// reads and writes, as eight lines on one graph, each with a line to its label;
// and two bars for the average I/O size and queue depth. Four measures in four
// units share the graph, so each has a scale of its own, named along the top.
// The figures of a sample, as perfmon.DiskRates makes them:
//   0 busy %   1 read B/s   2 written B/s   3 reads/s   4 writes/s
//   5 ms per read   6 ms per write   7 queue depth   8 busy % reading
// Uses perf.js's samples and drawing. Every top-level name here starts with dk or DK.
// ---------------------------------------------------------------------------
const DK = { head: 22, label: 148, reach: 44, bar: 62, gap: 14, smooth: 1, numbers: 3, bars: 5, min: 252, max: 392, row: 27, tight: 15 };
const dkOne = () => 1;
// Averages are of what happened, not of the samples: latency counts each operation the same.
const DK_SERIES = [
  { key: 'tr', name: 'Read throughput', unit: 'bytes', num: (r, k) => r.V(1, k), den: dkOne },
  { key: 'tw', name: 'Write throughput', unit: 'bytes', num: (r, k) => r.V(2, k), den: dkOne, dash: true },
  { key: 'ir', name: 'Read IOPS', unit: 'iops', num: (r, k) => r.V(3, k), den: dkOne },
  { key: 'iw', name: 'Write IOPS', unit: 'iops', num: (r, k) => r.V(4, k), den: dkOne, dash: true },
  { key: 'lr', name: 'Read latency', unit: 'ms', num: (r, k) => r.V(5, k) * r.V(3, k), den: (r, k) => r.V(3, k) },
  { key: 'lw', name: 'Write latency', unit: 'ms', num: (r, k) => r.V(6, k) * r.V(4, k), den: (r, k) => r.V(4, k), dash: true },
  { key: 'br', name: 'Busy reading', unit: 'busy', num: (r, k) => r.V(8, k), den: dkOne },
  { key: 'bw', name: 'Busy writing', unit: 'busy', num: (r, k) => Math.max(0, r.V(0, k) - r.V(8, k)), den: dkOne, dash: true },
];
const DK_SIZE = { num: (r, k) => r.V(1, k) + r.V(2, k), den: (r, k) => r.V(3, k) + r.V(4, k) };
const DK_QUEUE = { num: (r, k) => r.V(7, k), den: dkOne };
const DK_LEAST = { bytes: 1e6, iops: 10, ms: 1, busy: 100 };   // the lowest top of each scale
// OKLCH: a hue for each measure, the write a step lighter and dashed
const DK_PAL = {
  light: { tr: [0.48, 0.19, 296], tw: [0.60, 0.17, 318], ir: [0.50, 0.14, 250], iw: [0.60, 0.11, 222], lr: [0.55, 0.14, 60],
           lw: [0.62, 0.15, 42], br: [0.50, 0.12, 155], bw: [0.60, 0.13, 135], size: [0.55, 0.10, 220], queue: [0.60, 0.14, 55] },
  dark: { tr: [0.68, 0.17, 296], tw: [0.78, 0.15, 322], ir: [0.68, 0.14, 250], iw: [0.80, 0.10, 218], lr: [0.82, 0.15, 82],
          lw: [0.74, 0.16, 48], br: [0.70, 0.15, 155], bw: [0.84, 0.15, 128], size: [0.72, 0.10, 220], queue: [0.80, 0.13, 70] },
};
let dkColors = {}, dkHot = null;   // dkHot: { id, key } of the label pointed at

function dkReadColors() {
  const pal = DK_PAL[darkQuery.matches ? 'dark' : 'light'];
  dkColors = Object.fromEntries(Object.entries(pal).map(([k, c]) => [k, rgb(oklch(...c))]));
}
dkReadColors();
darkQuery.addEventListener('change', dkReadColors);

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
  const full = item.size ? `${pBytesSize(item.used)} of ${pBytesSize(item.size)} used (${Math.round(100 * item.used / item.size)}%)` : '';
  const nw = pText(item.name, p.x, p.y + 15, 13.5, T.ink, 600, 'left', p.w * 0.4);
  const detail = [item.detail, full, item.status].filter(Boolean).join('  ·  ');
  if (detail) pText(detail, p.x + nw + 10, p.y + 15, 12, item.status ? PC.warn : T.muted, 400, 'left', p.w - nw - 20);

  const barsW = 2 * DK.bar + DK.gap, right = p.x + p.w;
  const plot = { x: p.x, y: p.y + DK.head, w: Math.max(120, p.w - DK.reach - DK.label - DK.gap - barsW), h: p.h - DK.head };
  const from = ring ? Math.max(0, ring.after(pp.tEnd - span) - 1) : 0, to = ring ? ring.after(pp.tEnd) : 0;
  const lines = [], tops = { ...DK_LEAST };
  if (ring && to > from) {
    for (const q of DK_SERIES) {
      const v = dkTrail(ring, from, to, q);
      lines.push(v);
      if (q.unit !== 'busy') for (const x of v) if (x > tops[q.unit]) tops[q.unit] = x;
    }
  }
  for (const u of ['bytes', 'iops', 'ms']) tops[u] = niceCeil(tops[u]);

  pframe(plot, 4, span, true);
  const hot = dkHot && dkHot.id === item.key ? dkHot.key : null;
  const ends = [];
  clipTo(plot);
  DK_SERIES.forEach((q, i) => {
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
  pctx.restore();
  pctx.globalAlpha = pAlpha = pDim;
  border(plot);
  const scales = `Top of the graph: ${['bytes', 'iops', 'ms', 'busy'].map(u => DK_TOP[u](tops[u])).join('  ·  ')}`;
  pText(scales, plot.x + 6, plot.y + 13, 10.5, T.muted, 400, 'left', plot.w - 12);

  dkLabels(s, item, ring, plot, ends, hot);
  const bx = right - barsW, live = ring && ring.n && pLive(s);
  const size = live ? dkAverage(ring, DK.bars, DK_SIZE) : null, queue = live ? dkAverage(ring, DK.bars, DK_QUEUE) : null;
  dkSizeBar({ x: bx, y: plot.y, w: DK.bar, h: plot.h }, size);
  dkQueueBar({ x: bx + DK.bar + DK.gap, y: plot.y, w: DK.bar, h: plot.h }, queue, ring, from, to);
  PS.plots.push({ ...plot, kind: 'disk', s, item, ring });
}

// A label for each line, in a fixed order, with a line from where it ends now.
function dkLabels(s, item, ring, plot, ends, hot) {
  const n = DK_SERIES.length, x = plot.x + plot.w + DK.reach, two = plot.h >= n * DK.row - 4;
  const step = plot.h / n, live = ring && ring.n && pLive(s);
  DK_SERIES.forEach((q, i) => {
    const color = dkColors[q.key], shelf = Math.round(plot.y + step * (i + 1) - (step - (two ? DK.row : DK.tight)) / 2) - 1.5;
    const v = live ? dkAverage(ring, DK.numbers, q) : null;
    const value = v == null ? (live && q.unit === 'ms' ? 'no I/O' : '--') : DK_FORMAT[q.unit](v);
    pctx.globalAlpha = pDim * (hot && hot !== q.key ? 0.3 : 1);
    pctx.strokeStyle = color;
    pctx.lineWidth = hot === q.key ? 2 : 1.25;
    dkDash(q.dash);
    pctx.beginPath();
    pctx.moveTo(plot.x + plot.w + 3, ends[i]);
    pctx.lineTo(x, shelf);
    pctx.stroke();
    dkDash(false);
    pctx.beginPath();
    pctx.moveTo(x, shelf);
    pctx.lineTo(x + DK.label, shelf);
    pctx.stroke();
    pctx.beginPath();
    pctx.arc(plot.x + plot.w + 1, ends[i], 2.6, 0, 2 * Math.PI);
    pctx.fillStyle = color;
    pctx.fill();
    if (two) {
      pText(q.name, x + 2, shelf - 14.5, 11.5, T.ink, 600, 'left', DK.label - 4);
      pText(value, x + 2, shelf - 3.5, 11, T.ink2, 400, 'left', DK.label - 4);
    } else {
      const vw = pText(value, x + DK.label - 2, shelf - 3, 11, T.ink2, 600, 'right');
      pText(q.name, x + 2, shelf - 3, 11, T.ink, 400, 'left', DK.label - vw - 8);
    }
    PS.plots.push({ x: x - 4, y: shelf - (two ? DK.row : DK.tight), w: DK.label + 8, h: two ? DK.row : DK.tight, kind: 'disklabel', id: item.key, key: q.key });
  });
  pctx.globalAlpha = pAlpha = pDim;
}

// An upright bar with its name above, its value below, and ticks beside it. at: 0 to 1.
function dkBar(b, name, value, at, color, ticks) {
  const tx = 27, top = b.y + 18, bottom = b.y + b.h - 32, h = Math.max(10, bottom - top), bx = b.x + tx, bw = b.w - tx;
  pText(name, b.x + b.w, b.y + 10, 11.5, T.ink, 600, 'right');
  pctx.fillStyle = PC.plot;
  pctx.fillRect(bx, top, bw, h);
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
  const f = Math.max(0, Math.min(1, at)), fh = h * f;
  if (fh > 0.5) {
    pctx.fillStyle = color;
    pctx.beginPath();
    pctx.roundRect(bx + 4, bottom - fh, bw - 8, fh, [Math.min(4, fh), Math.min(4, fh), 0, 0]);
    pctx.fill();
  }
  border({ x: bx, y: top, w: bw, h });
  pText(value, b.x + b.w, bottom + 15, 12, T.ink, 600, 'right');
  pText('5 s average', b.x + b.w, bottom + 28, 9.5, T.muted, 400, 'right');
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

// What it all was at a moment.
function dkTip(p, t) {
  const ring = p.ring, k = nearest(ring, t);
  if (k < 0) return `<div class="head">${esc(p.item.name)}</div><div class="faint">No samples yet</div>`;
  const ago = Math.max(0, pp.tEnd - ring.T(k));
  const row = (q, v) => `<div><span class="sw" style="background:${dkColors[q.key]};border:0"></span>${q.name} <b>${v}</b></div>`;
  const val = q => {
    const d = q.den(ring, k);
    return d > 1e-9 ? DK_FORMAT[q.unit](q.num(ring, k) / d) : 'no I/O';
  };
  const ops = DK_SIZE.den(ring, k);
  return `<div class="head">${esc(p.item.name)} <span class="faint">${esc(p.s.info.name)}</span></div>` +
    DK_SERIES.map(q => row(q, val(q))).join('') +
    `<div class="sub">I/O size ${ops > 1e-9 ? dkSize(DK_SIZE.num(ring, k) / ops) : '–'} · queue ${dkQueue(ring.V(7, k))} · ${ring.V(0, k).toFixed(1)}% busy</div>` +
    `<div class="faint">${ago < 0.05 ? 'now' : `${ago.toFixed(ago < 10 ? 1 : 0)} s ago`}</div>`;
}

// How a page of them is laid out: { cols, rows, h (each), strip } for n disks in w x room.
function dkLayout(n, w, room, detailed) {
  const cols = w >= 1760 && n > 1 ? 2 : 1, rows = Math.ceil(n / cols) || 1;
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
