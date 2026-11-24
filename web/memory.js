'use strict';

// ---------------------------------------------------------------------------
// Memory & GPU tab: all of RAM as one bar from 0 to the installed size, split
// by what each part holds and labelled on leader lines; where in physical
// memory each kind sits; GPU memory by process; and charts of utilization
// and committed memory. Uses the theme and helpers from app.js
// (T, oklch, rgb, esc, $, loadPref, savePref, activeTab, darkQuery).
// ---------------------------------------------------------------------------
const GiB = 1073741824, MiB = 1048576;
const HISTORY_S = 60;                              // seconds of samples kept for the charts
const M_INTERVALS = [50, 100, 250, 500, 1000];     // ms: 20, 10, 4, 2, 1 samples a second, as on the CPU tab
const M_TEXT_MS = 250;                             // numbers and lists redraw at most 4 times a second
const M_TAU = 0.25;                                // seconds for the bars to catch up with a sample

// Geometry, CSS px
const MPAD = 28;                                   // left and right margin
const MBAR_H = 52, MRISE = 34;                     // RAM bar height; leader-line rise to the labels
const GBAR_H = 26, GRISE = 26;                     // the same for the GPU bar
const MLBL_H = 30, MLBL_GAP = 10, MRUN = 16;       // label height, space between labels, sideways reach
const MDIM_H = 22, MAXIS_H = 22, MSTRIP_H = 24, MHEAD_H = 26, MSECTION_GAP = 26;

// What the page database says pages in use hold (MMPFNUSE_* order), and the
// kind of memory on the bar each one counts as.
const USE_NAMES = ['Process memory', 'Mapped files', 'Shared memory', 'Page tables', 'Paged pool',
  'Nonpaged pool', 'System mappings', 'Session memory', 'File system metadata', 'Locked by apps (AWE)',
  'Locked by drivers', 'Kernel stacks'];
const USE_KIND = ['proc', 'mapped', 'shared', 'ptables', 'paged', 'nonpaged', 'kernel', 'kernel', 'kernel',
  'proc', 'kernel', 'kernel'];

// The kinds of memory on the RAM bar. color: another kind's color to share.
const KINDS = {
  proc: { name: 'Process memory', tip: "Programs' private memory: their data, heaps and stacks." },
  apps: { name: 'Programs and other', color: 'proc',
    tip: "Programs' memory, and anything else in use that the counters don't single out." },
  comp: { name: 'Compressed', tip: "Pages Windows has compressed to keep them in RAM instead of writing them to the page file. It's the Memory Compression process." },
  mapped: { name: 'Mapped files', tip: 'Files mapped into memory: program code, libraries, and files that programs or the system cache have open.' },
  cache: { name: 'File cache', color: 'mapped', tip: 'File data in the system cache.' },
  shared: { name: 'Shared memory', tip: 'Memory programs share with each other, backed by the page file.' },
  paged: { name: 'Paged pool', tip: 'Kernel memory that Windows can move out to the page file.' },
  nonpaged: { name: 'Nonpaged pool', tip: 'Kernel memory that has to stay in RAM, for drivers and Windows itself.' },
  ptables: { name: 'Page tables', tip: "The tables that translate each program's addresses to physical memory." },
  kernel: { name: 'Other kernel memory', tip: 'Kernel stacks, system mappings, file system metadata, and pages drivers have locked.' },
  drivers: { name: 'Drivers', color: 'kernel', tip: 'Driver code and data in RAM.' },
  modified: { name: 'Modified', tip: 'Changed data waiting to be written to disk before its memory can be used again.' },
  standby: { name: 'Standby', tip: 'Cached files and data no program is using right now. Windows hands it out at once when a program needs memory.' },
  free: { name: 'Free', tip: 'Memory holding nothing, ready to use.' },
  avail: { name: 'Available', color: 'standby', tip: 'Memory programs can get right away: standby and free.' },
  hw: { name: 'Hardware reserved', tip: "Set aside by the firmware for devices such as graphics. Windows can't use it." },
};
const IN_USE = new Set(['proc', 'apps', 'comp', 'mapped', 'cache', 'shared', 'paged', 'nonpaged', 'ptables', 'kernel', 'drivers']);

// OKLCH; checked for color-vision deficiencies between neighbours on the bar
const MPAL = {
  light: {
    proc: [0.56, 0.13, 152], comp: [0.77, 0.11, 152], mapped: [0.44, 0.09, 172], shared: [0.67, 0.13, 132],
    paged: [0.55, 0.16, 355], nonpaged: [0.75, 0.11, 355], ptables: [0.44, 0.13, 350], kernel: [0.64, 0.15, 328],
    modified: [0.80, 0.15, 78], standby: [0.58, 0.13, 248], free: [0.93, 0.012, 240], hw: [0.72, 0.004, 90],
    gpuA: [0.56, 0.14, 45], gpuB: [0.76, 0.12, 65], gpuOther: [0.40, 0.015, 60],
  },
  dark: {
    proc: [0.66, 0.14, 152], comp: [0.84, 0.10, 152], mapped: [0.53, 0.10, 172], shared: [0.75, 0.14, 132],
    paged: [0.64, 0.16, 355], nonpaged: [0.81, 0.10, 355], ptables: [0.53, 0.14, 350], kernel: [0.72, 0.15, 328],
    modified: [0.83, 0.14, 78], standby: [0.66, 0.13, 248], free: [0.36, 0.014, 240], hw: [0.52, 0.004, 90],
    gpuA: [0.68, 0.15, 45], gpuB: [0.84, 0.11, 65], gpuOther: [0.52, 0.015, 60],
  },
};
let MC = {};                                       // palette name -> css color
const kindOf = k => KINDS[k] || { name: k, tip: '' };
const kindColor = k => MC[kindOf(k).color || k] || MC.hw;

const mstage = $('mstage'), mcv = $('mcv'), mctx = mcv.getContext('2d'), mtip = $('mtip');
const mem = {
  last: null,            // latest sample from the server
  segs: [], gsegs: [],   // RAM and GPU bars: [{ key, bytes, shown }], left to right
  map: null,             // latest address map: { slice, cells }
  hist: [],              // [{ t, g3d, cpu, commit, ram }] for the last HISTORY_S seconds, oldest first
  interval: 1000,        // ms between samples, as the server reports it
  restored: false,       // the rate chosen last time has been put back
  textAt: 0,             // when the numbers and lists last redrew
  commitLimit: 0,        // bytes that can be committed: RAM plus page file
  commit: 0,             // committed bytes, eased like the bars
  kindsKey: '',          // the kinds in the legend
  hover: null, mouse: null, hits: [],
};
let mdpr = 1, mW = 0, mH = 0, UC = {}, ML = null;

function fmtGB(b) {
  const g = b / GiB;
  return g >= 100 ? g.toFixed(0) : g >= 10 ? g.toFixed(1) : g.toFixed(2);
}
function fmtSize(b) {
  if (b >= GiB) return `${fmtGB(b)} GB`;
  const m = b / MiB;
  return m >= 10 ? `${Math.round(m)} MB` : m >= 1 ? `${m.toFixed(1)} MB` : `${Math.max(0, Math.round(b / 1024))} KB`;
}
const capacity = b => String(Math.round(b / GiB));   // installed sizes: 32, 128, 24
const pct = f => `${Math.round(f * 100)}%`;          // f is a fraction
function share(b, total) {                           // a share that stays readable when it's small
  const p = total > 0 ? 100 * b / total : 0;
  return p >= 10 ? `${Math.round(p)}%` : p >= 0.1 ? `${p.toFixed(1)}%` : p > 0 ? '<0.1%' : '0%';
}
const msum = a => a.reduce((s, x) => s + x, 0);
const mclamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

// ---------------------------------------------------------------------------
// Samples
// ---------------------------------------------------------------------------
function ramParts(r) {   // the RAM bar's kinds and sizes, left to right
  const out = r.uses.map(([key, bytes]) => ({ key, bytes }));
  if (r.lists) {
    out.push({ key: 'modified', bytes: r.lists.modified }, { key: 'standby', bytes: msum(r.lists.standby) },
      { key: 'free', bytes: r.lists.free });
  } else {
    out.push({ key: 'avail', bytes: r.avail });
  }
  out.push({ key: 'hw', bytes: Math.max(0, r.installed - r.total) });
  return out;
}

function gpuParts(g) {
  const out = g.procs.map(([name, bytes], i) => ({ key: `p:${name}`, name, bytes, tone: i % 2 ? 'gpuB' : 'gpuA' }));
  if (g.unowned > 0) out.push({ key: 'g:other', name: 'Not owned by one app', bytes: g.unowned, tone: 'gpuOther' });
  const taken = msum(out.map(s => s.bytes));
  out.push({ key: 'g:free', name: 'Free', bytes: Math.max(0, g.vram - taken), tone: 'free' });
  return out;
}

// New sizes to ease towards, keeping what's shown for kinds already there.
function retarget(old, parts) {
  const was = new Map(old.map(s => [s.key, s.shown]));
  return parts.map(p => ({ ...p, shown: was.has(p.key) ? was.get(p.key) : (old.length ? 0 : p.bytes) }));
}

function onSys(m) {
  mem.last = m;
  syncRate(m.iv);
  const ram = m.ram, gpu = m.gpu;
  const hadMap = !!mem.map, hadGpu = mem.gsegs.length > 0;
  if (!ram) mem.map = null;
  else if (ram.map) mem.map = ram.map;   // a few times a second; samples between keep the last
  else if (ram.err) mem.map = null;
  mem.segs = ram && ram.uses ? retarget(mem.segs, ramParts(ram)) : [];
  mem.gsegs = gpu ? retarget(mem.gsegs, gpuParts(gpu)) : [];
  if (!mem.commit && m.commit) mem.commit = m.commit.used;

  const inUse = msum(mem.segs.filter(s => IN_USE.has(s.key)).map(s => s.bytes));
  const g3d = gpu ? (gpu.engines.find(e => e[0] === '3D') || [])[1] : undefined;
  const t = m.t ?? performance.now() / 1000;
  const prev = mem.hist[mem.hist.length - 1];
  if (prev && (t < prev.t || t - prev.t > HISTORY_S)) mem.hist = [];   // the server restarted, or a long gap
  mem.hist.push({ t, g3d: g3d ?? null, cpu: m.cpu ?? null,
                  commit: m.commit ? m.commit.used : null, ram: ram && ram.uses ? inUse : null });
  while (mem.hist.length && mem.hist[0].t < t - HISTORY_S) mem.hist.shift();
  mem.commitLimit = m.commit ? m.commit.limit : 0;

  const kinds = mem.segs.map(s => s.key).join(',') + (mem.map ? '|map' : '') + (mem.gsegs.length ? '|gpu' : '');
  if (kinds !== mem.kindsKey) {
    mem.kindsKey = kinds;
    renderLegend();
  }
  if (!ML || hadMap !== !!mem.map || hadGpu !== mem.gsegs.length > 0) mlayout();
  // Bars and charts follow every sample; text redraws less often so it stays readable.
  const now = performance.now();
  if (now - mem.textAt >= M_TEXT_MS) {
    mem.textAt = now;
    renderMemStats(inUse);
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
// Layout: system memory, then where it sits, then GPU memory, top to bottom
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
  const x0 = MPAD, x1 = Math.max(MPAD + 100, mW - MPAD);
  const zone = MRISE + MLBL_H, gzone = GRISE + MLBL_H;
  const ramH = MHEAD_H + zone + MBAR_H + zone + 8 + 3 * MDIM_H + MAXIS_H;
  let stripH = mem.map ? MHEAD_H + MSTRIP_H + MAXIS_H : 0;
  let gpuH = mem.gsegs.length ? MHEAD_H + gzone + GBAR_H + MAXIS_H : 0;
  let gpuLabels = true;
  const room = mH - $('mlegend').offsetHeight - 28;
  const need = () => ramH + (stripH && MSECTION_GAP + stripH) + (gpuH && MSECTION_GAP + gpuH);
  // When it doesn't all fit: GPU labels go first, then the GPU bar (the side
  // panel lists GPU memory by process too), then the address map.
  if (need() > room && gpuH) { gpuH -= gzone; gpuLabels = false; }
  if (need() > room) gpuH = 0;
  if (need() > room) stripH = 0;
  const slack = Math.max(0, room - need());
  const gap = MSECTION_GAP + Math.min(24, slack / 4);
  let y = 14 + Math.min(40, Math.max(0, room - need() - 2 * (gap - MSECTION_GAP)) / 2);

  ML = { x0, x1, w: x1 - x0 };
  ML.ram = { headY: y + 9, bar: { y: y + MHEAD_H + zone, h: MBAR_H } };
  const b = ML.ram.bar;
  ML.ram.dimY = [0, 1, 2].map(i => b.y + MBAR_H + zone + 8 + i * MDIM_H + MDIM_H / 2);
  ML.ram.axisY = b.y + MBAR_H + zone + 8 + 3 * MDIM_H + 4;
  y += ramH;
  ML.strip = null;
  if (stripH) {
    y += gap;
    ML.strip = { headY: y + 9, y: y + MHEAD_H, h: MSTRIP_H, axisY: y + MHEAD_H + MSTRIP_H + 4 };
    y += stripH;
  }
  ML.gpu = null;
  if (gpuH) {
    y += gap;
    const top = y + MHEAD_H + (gpuLabels ? gzone : 0);
    ML.gpu = { headY: y + 9, labels: gpuLabels, bar: { y: top, h: GBAR_H }, axisY: top + GBAR_H + 4 };
  }
}

// ---------------------------------------------------------------------------
// Drawing helpers
// ---------------------------------------------------------------------------
function mtext(str, x, y, size, color, align = 'left', weight = 400) {
  mctx.font = `${weight} ${size}px ${T.font}`;
  mctx.textAlign = align;
  mctx.textBaseline = 'middle';
  mctx.fillStyle = color;
  mctx.fillText(str, x, y);
  return mctx.measureText(str).width;
}
function mwidth(str, size, weight = 400) {
  mctx.font = `${weight} ${size}px ${T.font}`;
  return mctx.measureText(str).width;
}

function hatch(x, y, w, h, color) {   // diagonal lines over a filled box
  mctx.save();
  mctx.beginPath();
  mctx.rect(x, y, w, h);
  mctx.clip();
  mctx.strokeStyle = color;
  mctx.lineWidth = 1;
  mctx.beginPath();
  for (let d = -h; d < w; d += 5) {
    mctx.moveTo(x + d, y + h);
    mctx.lineTo(x + d + h, y);
  }
  mctx.stroke();
  mctx.restore();
}

// Segment positions from the sizes shown, filling the bar exactly.
function geom(segs, x0, w) {
  const total = msum(segs.map(s => Math.max(0, s.shown))) || 1;
  let acc = 0;
  return segs.map(s => {
    const a = x0 + w * acc / total;
    acc += Math.max(0, s.shown);
    return { s, x0: a, x1: x0 + w * acc / total };
  });
}

function niceStep(total) {   // a tick every 1, 2, 4... GB, eight ticks at most
  let step = GiB / 4;
  while (total / step > 8) step *= 2;
  return step;
}

function drawAxis(y, total, x0, w) {
  const step = niceStep(total);
  mctx.strokeStyle = T.grid;
  mctx.lineWidth = 1;
  mctx.beginPath();
  mctx.moveTo(x0, y + 0.5);
  mctx.lineTo(x0 + w, y + 0.5);
  mctx.stroke();
  const n = Math.floor(total / step + 1e-6);
  for (let i = 0; i <= n; i++) {
    const v = i * step, x = Math.round(x0 + w * v / total) + 0.5;
    mctx.strokeStyle = T.muted;
    mctx.beginPath();
    mctx.moveTo(x, y);
    mctx.lineTo(x, y + 4);
    mctx.stroke();
    const label = i === n ? `${+(v / GiB).toFixed(2)} GB` : `${+(v / GiB).toFixed(2)}`;
    mtext(label, x, y + 13, 11, T.muted, i === 0 ? 'left' : i === n && x > x0 + w - 30 ? 'right' : 'center');
  }
}

// A section's heading: the name, then details in muted text if they fit.
function drawHead(y, title, sub) {
  const tw = mtext(title, ML.x0, y, 13, T.ink, 'left', 600);
  if (!sub) return;
  const room = ML.x1 - (ML.x0 + tw + 10);
  if (mwidth(sub, 12) <= room) mtext(sub, ML.x0 + tw + 10, y, 12, T.muted);
}

function hit(x0, y0, x1, y1, h) { mem.hits.push({ x0, y0, x1, y1, ...h }); }

// ---------------------------------------------------------------------------
// Labels on leader lines
// ---------------------------------------------------------------------------
// Spread labels along a row without overlaps, each as near as it can get to
// where it wants to be: overlapping labels merge into a cluster centred on
// their wishes. items: [{ want (left x), w }] in anchor order. False if the
// row is too short for them all.
function dodge(items, lo, hi) {
  const need = msum(items.map(it => it.w)) + MLBL_GAP * Math.max(0, items.length - 1);
  if (need > hi - lo) return false;
  const cl = [];
  for (const it of items) {
    cl.push({ items: [it], w: it.w, sum: it.want + it.w / 2, n: 1 });
    for (;;) {
      const c = cl[cl.length - 1], p = cl[cl.length - 2];
      c.x = mclamp(c.sum / c.n - c.w / 2, lo, hi - c.w);
      if (!p || p.x + p.w + MLBL_GAP <= c.x) break;
      p.items.push(...c.items);
      p.w += MLBL_GAP + c.w;
      p.sum += c.sum;
      p.n += c.n;
      cl.pop();
    }
  }
  for (const c of cl) {
    let x = c.x;
    for (const it of c.items) {
      it.lx = x;
      x += it.w + MLBL_GAP;
    }
  }
  return true;
}

// Place labels for a bar's segments on the given sides ('up', 'down'). Neighbours
// alternate sides so crowded small segments spread out; labels that still
// don't fit are left off, smallest first (hover and the legend still name them).
function placeLabels(list, sides, label) {
  const items = list.filter(g => g.s.bytes > 0 && g.x1 - g.x0 >= 0.01).map(g => {
    const [name, value] = label(g.s);
    return { g, name, value, ax: (g.x0 + g.x1) / 2, w: Math.ceil(Math.max(mwidth(name, 12, 600), mwidth(value, 11))) + 2 };
  });
  const lo = ML.x0 - MPAD + 6, hi = ML.x1 + MPAD - 6;
  const mid = (ML.x0 + ML.x1) / 2, half = ML.w / 2;
  const bySide = sides.map(() => []);
  items.forEach((it, i) => bySide[i % sides.length].push(it));
  const placed = [];
  let spill = [];
  bySide.forEach((own, si) => {
    const row = [...own, ...spill].sort((a, b) => a.ax - b.ax);
    spill = [];
    for (;;) {
      for (const it of row) {   // labels lean outwards, so leaders fan out at an angle
        const lean = mclamp((it.ax - mid) / half * 2, -1, 1);
        it.want = it.ax + lean * (it.w / 2 + MRUN) - it.w / 2;
      }
      if (dodge(row, lo, hi)) break;
      let k = 0;
      row.forEach((it, i) => { if (it.g.s.bytes < row[k].g.s.bytes) k = i; });
      spill.push(row.splice(k, 1)[0]);   // the next side may have room for it
    }
    lean(row, lo, hi);
    for (const it of row) {
      it.side = sides[si];
      placed.push(it);
    }
  });
  return placed;
}

// A label straight above (or below) its anchor gets a vertical leader. Slide
// it sideways into any free room so the leader leaves the bar at an angle.
function lean(row, lo, hi) {
  const MIN = 12;
  row.forEach((it, i) => {
    const l = it.lx, r = l + it.w;
    if (Math.min(Math.abs(it.ax - l), Math.abs(it.ax - r)) >= MIN) return;   // the leader is already at an angle
    const left = i ? row[i - 1].lx + row[i - 1].w + MLBL_GAP : lo;
    const right = i < row.length - 1 ? row[i + 1].lx - MLBL_GAP : hi;
    const toRight = it.ax + MIN, toLeft = it.ax - MIN - it.w;   // the box just past the anchor
    const fitsR = toRight + it.w <= right, fitsL = toLeft >= left;
    if (fitsR && (!fitsL || it.ax - l <= r - it.ax)) it.lx = toRight;
    else if (fitsL) it.lx = toLeft;
  });
}

function mdrawLabels(placed, bar, rise, hoverKey, kind) {
  for (const it of placed) {
    const up = it.side === 'up';
    const edge = up ? bar.y : bar.y + bar.h;
    const shelf = up ? bar.y - rise : bar.y + bar.h + rise;
    const l = it.lx, r = it.lx + it.w;
    const ex = it.ax <= l ? l : it.ax >= r ? r : (it.ax - l < r - it.ax ? l : r);
    const hot = hoverKey === it.g.s.key;
    mctx.strokeStyle = hot ? T.hl : T.lead;
    mctx.lineWidth = hot ? 1.5 : 1;
    mctx.beginPath();
    mctx.moveTo(it.ax, edge + (up ? -3 : 3));
    mctx.lineTo(ex, shelf);
    mctx.moveTo(l, shelf);
    mctx.lineTo(r, shelf);
    mctx.stroke();
    mctx.beginPath();
    mctx.arc(it.ax, edge + (up ? -3 : 3), 2, 0, Math.PI * 2);
    mctx.fillStyle = hot ? T.hl : T.ink2;
    mctx.fill();
    const align = ex === r ? 'right' : 'left', tx = align === 'right' ? r : l;
    const ny = up ? shelf - 21 : shelf + 9, vy = up ? shelf - 7 : shelf + 23;
    mtext(it.name, tx, ny, 12, T.ink, align, 600);
    mtext(it.value, tx, vy, 11, T.ink2, align);
    hit(l - 2, up ? shelf - 30 : shelf, r + 2, up ? shelf + 2 : shelf + 30, { kind, key: it.g.s.key });
  }
}

// ---------------------------------------------------------------------------
// The bars
// ---------------------------------------------------------------------------
function drawSegments(list, bar, colorOf, hoverKey, kind) {
  const { y, h } = bar;
  mctx.save();
  mctx.beginPath();
  mctx.roundRect(ML.x0, y, ML.w, h, 4);
  mctx.clip();
  for (const g of list) {
    const x0 = Math.round(g.x0), x1 = Math.round(g.x1);
    if (x1 <= x0 && g.x1 - g.x0 < 0.3) continue;
    const w = Math.max(1, x1 - x0);
    mctx.fillStyle = colorOf(g.s);
    mctx.fillRect(x0, y, w, h);
    if (g.s.key === 'hw' || g.s.key === 'g:other') hatch(x0, y, w, h, T.bg);
    if (w >= 4 && g !== list[list.length - 1]) {   // a hairline of background between segments
      mctx.fillStyle = T.bg;
      mctx.fillRect(x0 + w - 1, y, 1, h);
    }
  }
  mctx.restore();
  mctx.strokeStyle = T.grid;
  mctx.lineWidth = 1;
  mctx.beginPath();
  mctx.roundRect(ML.x0 - 0.5, y - 0.5, ML.w + 1, h + 1, 4.5);
  mctx.stroke();
  // Hover targets: thin segments get a few pixels either side, and win over their neighbours.
  const order = [...list].sort((a, b) => (b.x1 - b.x0) - (a.x1 - a.x0));
  for (const g of order) {
    const c = (g.x0 + g.x1) / 2;
    hit(Math.min(g.x0, c - 3), y - 2, Math.max(g.x1, c + 3), y + h + 2, { kind, key: g.s.key });
  }
  const hv = list.find(g => g.s.key === hoverKey);
  if (hv) moutline(hv.x0, hv.x1, bar);
}

function moutline(x0, x1, bar) {
  mctx.strokeStyle = T.hl;
  mctx.lineWidth = 2;
  const w = Math.max(2, x1 - x0);
  mctx.strokeRect(x0 - 1, bar.y - 1, w + 2, bar.h + 2);
}

// A dimension line under the bar: |—— text ——|, for kinds that add up.
function drawDim(x0, x1, y, name, value, color, key, over) {
  const lo = ML.x0 - MPAD + 4, hi = ML.x1 + MPAD - 4;
  const end = Math.min(x1, over ? hi - 6 : x1);
  const hot = mem.hover && mem.hover.kind === 'dim' && mem.hover.key === key;
  mctx.strokeStyle = hot ? T.hl : color;
  mctx.lineWidth = hot ? 2 : 1.5;
  mctx.beginPath();
  mctx.moveTo(x0, y);
  mctx.lineTo(end, y);
  mctx.moveTo(x0 + 0.5, y - 5);
  mctx.lineTo(x0 + 0.5, y + 5);
  if (over) {   // more than the scale: an arrowhead past the end
    mctx.moveTo(end - 6, y - 4);
    mctx.lineTo(end, y);
    mctx.lineTo(end - 6, y + 4);
  } else {
    mctx.moveTo(end - 0.5, y - 5);
    mctx.lineTo(end - 0.5, y + 5);
  }
  mctx.stroke();
  const nw = mwidth(name, 12, 600), vw = mwidth(value, 12), tw = nw + 6 + vw;
  let tx;
  if (end - x0 >= tw + 20) tx = (x0 + end) / 2 - tw / 2;
  else if (end + 8 + tw <= hi) tx = end + 8;
  else tx = Math.max(lo, x0 - 8 - tw);
  if (tx > x0 && tx + tw < end) {   // cut the line under the text
    mctx.fillStyle = T.bg;
    mctx.fillRect(tx - 5, y - 8, tw + 10, 16);
  }
  mtext(name, tx, y, 12, T.ink, 'left', 600);
  mtext(value, tx + nw + 6, y, 12, T.ink2);
  hit(Math.min(x0, tx), y - 9, Math.max(end, tx + tw), y + 9, { kind: 'dim', key });
}

function drawRam() {
  const m = mem.last, r = m.ram, sec = ML.ram, bar = { x: ML.x0, ...sec.bar };
  const usable = r.lists ? 'by what it holds' : 'in use and available';
  drawHead(sec.headY, 'System memory',
    `${capacity(r.installed)} GB installed · ${(r.total / GiB).toFixed(1)} GB usable by Windows · ${usable}`);
  const list = geom(mem.segs, ML.x0, ML.w);
  const hk = mem.hover && mem.hover.kind === 'seg' ? mem.hover.key : null;
  drawSegments(list, bar, s => kindColor(s.key), hk, 'seg');
  const placed = placeLabels(list, ['up', 'down'], s => [kindOf(s.key).name, `${fmtSize(s.bytes)} · ${share(s.bytes, r.installed)}`]);
  mdrawLabels(placed, bar, MRISE, hk, 'seg');

  // Dimension lines: in use, available, cached (Task Manager's words), committed
  const at = key => list.find(g => g.s.key === key);
  const uses = list.filter(g => IN_USE.has(g.s.key));
  const usedEnd = uses.length ? uses[uses.length - 1].x1 : ML.x0;
  const inUse = msum(uses.map(g => g.s.bytes));
  const step = niceStep(r.installed);   // light gridlines through the dimension rows
  mctx.strokeStyle = T.grid;
  mctx.lineWidth = 1;
  mctx.beginPath();
  for (let v = 0; v <= r.installed + 1; v += step) {
    const x = Math.round(ML.x0 + ML.w * v / r.installed) + 0.5;
    mctx.moveTo(x, sec.dimY[0] - MDIM_H / 2);
    mctx.lineTo(x, sec.axisY);
  }
  mctx.stroke();
  drawDim(ML.x0, usedEnd, sec.dimY[0], 'In use', fmtSize(inUse), MC.proc, 'inuse');
  if (r.lists) {
    const mod = at('modified'), sb = at('standby'), fr = at('free');
    drawDim(sb.x0, fr.x1, sec.dimY[0], 'Available', fmtSize(r.lists.free + msum(r.lists.standby)), MC.standby, 'avail');
    drawDim(mod.x0, sb.x1, sec.dimY[1], 'Cached', fmtSize(r.lists.modified + msum(r.lists.standby)), MC.modified, 'cached');
  } else {
    const av = at('avail');
    drawDim(av.x0, av.x1, sec.dimY[0], 'Available', fmtSize(r.avail), MC.standby, 'avail');
  }
  if (m.commit) {
    const x = ML.x0 + ML.w * mem.commit / r.installed;
    drawDim(ML.x0, x, sec.dimY[2], 'Committed',
      `${fmtSize(m.commit.used)} of ${capacity(m.commit.limit)} GB`, UC.commit, 'commit', x > ML.x1);
  }
  drawAxis(sec.axisY, r.installed, ML.x0, ML.w);
  if (mem.hover && mem.hover.kind === 'dim') {   // what a dimension line adds up
    const k = mem.hover.key;
    const span = k === 'inuse' ? [ML.x0, usedEnd] : k === 'avail' ? [(at('standby') || at('avail')).x0, (at('free') || at('avail')).x1]
      : k === 'cached' ? [at('modified').x0, at('standby').x1] : null;
    if (span) moutline(span[0], span[1], bar);
  }
}

// Where each kind sits: physical address space from 0 to the end of RAM, one
// column per slice, its height split by how that slice's RAM is used.
function drawStrip() {
  const sec = ML.strip, map = mem.map, cells = map.cells, n = cells.length;
  const end = n * map.slice;
  drawHead(sec.headY, 'By physical address',
    `where each kind sits, from byte 0 to ${fmtGB(end)} GB of address space · ${fmtSize(map.slice)} a column`);
  const { y, h } = sec;
  const X = i => Math.round(ML.x0 + ML.w * i / n);
  mctx.fillStyle = T.gap;
  mctx.fillRect(ML.x0, y, ML.w, h);
  for (let i = 0; i < n; i++) {
    const c = cells[i], x0 = X(i), w = Math.max(1, X(i + 1) - x0);
    if (!c) continue;   // no RAM here: the background shows through
    const ram = c[0] / 1000;
    let top = y + h;
    const band = (f, color) => {
      const bh = h * ram * f / 1000;
      if (bh <= 0) return;
      mctx.fillStyle = color;
      mctx.fillRect(x0, top - bh, w, bh);
      top -= bh;
    };
    // In use, split by what it holds most; the rest counts as the main use.
    const tops = [];
    for (let j = 5; j + 1 < c.length; j += 2) tops.push([c[j], c[j + 1]]);
    if (!tops.length) tops.push([0, 0]);
    tops[0][1] += Math.max(0, c[1] - msum(tops.map(t => t[1])));
    for (const [use, f] of tops) band(f, MC[USE_KIND[use]] || MC.proc);
    band(c[2], MC.modified);
    band(c[3], MC.standby);
    band(c[4], MC.free);
  }
  mctx.strokeStyle = T.grid;
  mctx.lineWidth = 1;
  mctx.strokeRect(ML.x0 - 0.5, y - 0.5, ML.w + 1, h + 1);
  hit(ML.x0, y - 2, ML.x1, y + h + 2, { kind: 'cell' });
  if (mem.hover && mem.hover.kind === 'cell' && mem.hover.i != null) {
    const i = mem.hover.i;
    moutline(X(i), Math.max(X(i) + 1, X(i + 1)), { y, h });
  }
  drawAxis(sec.axisY, end, ML.x0, ML.w);
}

function drawGpu() {
  const g = mem.last.gpu, sec = ML.gpu, bar = { x: ML.x0, ...sec.bar };
  drawHead(sec.headY, 'GPU memory', `${g.name} · ${capacity(g.vram)} GB · by process`);
  const list = geom(mem.gsegs, ML.x0, ML.w);
  const hk = mem.hover && mem.hover.kind === 'gseg' ? mem.hover.key : null;
  drawSegments(list, bar, s => MC[s.tone], hk, 'gseg');
  if (sec.labels) {
    const placed = placeLabels(list, ['up'], s => [s.name, `${fmtSize(s.bytes)} · ${share(s.bytes, g.vram)}`]);
    mdrawLabels(placed, bar, GRISE, hk, 'gseg');
  }
  drawAxis(sec.axisY, g.vram, ML.x0, ML.w);
}

function mdraw() {
  mctx.setTransform(1, 0, 0, 1, 0, 0);
  mctx.clearRect(0, 0, mcv.width, mcv.height);
  mctx.setTransform(mdpr, 0, 0, mdpr, 0, 0);
  mem.hits = [];
  const m = mem.last;
  if (!ML || !m || !mem.segs.length) {
    mtext(m ? '' : 'Waiting for the first sample…', mW / 2, mH / 2, 13, T.muted, 'center');
    return;
  }
  drawRam();
  if (ML.strip && mem.map) drawStrip();
  if (ML.gpu && m.gpu && mem.gsegs.length) drawGpu();
}

let mLast = performance.now();
function mframe(t) {
  const dt = Math.min(0.1, (t - mLast) / 1000);
  mLast = t;
  requestAnimationFrame(mframe);
  if (activeTab !== 'mem') return;
  if ((window.devicePixelRatio || 1) !== mdpr) mlayout();
  const k = 1 - Math.exp(-dt / M_TAU);
  for (const list of [mem.segs, mem.gsegs]) for (const s of list) s.shown += (s.bytes - s.shown) * k;
  if (mem.last && mem.last.commit) mem.commit += (mem.last.commit.used - mem.commit) * k;
  mdraw();
}

// ---------------------------------------------------------------------------
// Hover
// ---------------------------------------------------------------------------
function memAt(mx, my) {
  for (let i = mem.hits.length - 1; i >= 0; i--) {   // the last drawn is on top
    const h = mem.hits[i];
    if (mx < h.x0 || mx > h.x1 || my < h.y0 || my > h.y1) continue;
    if (h.kind === 'cell') {
      const n = mem.map.cells.length;
      return { kind: 'cell', i: mclamp(Math.floor((mx - ML.x0) / ML.w * n), 0, n - 1) };
    }
    return { kind: h.kind, key: h.key };
  }
  return null;
}
// Thin segments win over their wide neighbours: their targets were added last.

const swatch = color => `<span class="sw" style="background:${color}"></span>`;
const tipHead = (color, title, sub = '') => `<div class="head">${color ? swatch(color) : ''}${esc(title)}${sub ? ` <span class="faint">${esc(sub)}</span>` : ''}</div>`;
const tipDesc = text => `<div class="desc">${esc(text)}</div>`;

function segTip(key) {
  const r = mem.last.ram, s = mem.segs.find(x => x.key === key);
  if (!s) return '';
  const k = kindOf(key);
  let html = tipHead(kindColor(key), k.name) + `<div>${fmtSize(s.bytes)} · ${share(s.bytes, r.installed)} of RAM</div>` + tipDesc(k.tip);
  if (key === 'standby' && r.lists && (r.lists.standby[0] || r.lists.standby[2])) {
    const [core, normal, reserve] = r.lists.standby;
    html += `<div class="sub">Core ${fmtSize(core)} · Normal priority ${fmtSize(normal)} · Reserve ${fmtSize(reserve)}</div>`;
  }
  if (key === 'paged' && r.pagedAll > s.bytes) {
    html += `<div class="sub">${fmtSize(r.pagedAll)} in all: ${fmtSize(r.pagedAll - s.bytes)} of it is in the page file</div>`;
  }
  if (key === 'kernel' && r.kernel && r.kernel.length) {
    html += r.kernel.filter(([, b]) => b >= MiB).sort((a, b) => b[1] - a[1])
      .map(([i, b]) => `<div class="sub">${esc(USE_NAMES[i])} ${fmtSize(b)}</div>`).join('');
  }
  if (key === 'hw') html += `<div class="sub">${fmtGB(r.installed)} GB installed, ${fmtGB(r.total)} GB usable</div>`;
  if (key === 'apps' && r.err) html += `<div class="faint desc">A finer split needs the memory map: ${esc(r.err)}</div>`;
  return html;
}

function dimTip(key) {
  const m = mem.last, r = m.ram;
  if (key === 'commit') {
    const c = m.commit;
    let html = tipHead(UC.commit, 'Committed', `${fmtSize(c.used)} of ${fmtSize(c.limit)}`) +
      tipDesc("Memory Windows has promised to programs, kept in RAM or the page file. Some of it is never touched, so it can be larger than what's in use. The limit is RAM plus the page file.") +
      `<div class="sub">Peak since Windows started: ${fmtSize(c.peak)}</div>`;
    for (const [path, size, used] of c.files || []) {
      html += `<div class="sub">Page file ${esc(path)}: ${fmtSize(size)}, ${fmtSize(used)} in use</div>`;
    }
    return html;
  }
  const uses = mem.segs.filter(s => IN_USE.has(s.key));
  if (key === 'inuse') {
    const total = msum(uses.map(s => s.bytes));
    return tipHead(MC.proc, 'In use', fmtSize(total)) +
      tipDesc('Memory programs, drivers and Windows are using.') +
      [...uses].sort((a, b) => b.bytes - a.bytes).map(s =>
        `<div class="sub">${swatch(kindColor(s.key))}${esc(kindOf(s.key).name)} ${fmtSize(s.bytes)}</div>`).join('');
  }
  if (key === 'avail') {
    const total = r.lists ? r.lists.free + msum(r.lists.standby) : r.avail;
    return tipHead(MC.standby, 'Available', fmtSize(total)) +
      tipDesc('Standby plus free: memory programs can get right away.');
  }
  const total = r.lists.modified + msum(r.lists.standby);
  return tipHead(MC.modified, 'Cached', fmtSize(total)) +
    tipDesc('Modified plus standby: what Task Manager calls Cached.');
}

function cellTip(i) {
  const map = mem.map, c = map.cells[i];
  const a = i * map.slice / GiB, b = (i + 1) * map.slice / GiB;
  const range = `${a.toFixed(2)}–${b.toFixed(2)} GB`;
  if (!c) {
    return tipHead('', range, 'physical address') +
      tipDesc('No RAM at these addresses. The range is kept for hardware such as the graphics card.');
  }
  const p = v => `${Math.round(v / 10)}%`;
  let html = tipHead('', range, 'physical address') +
    `<div>${swatch(MC.proc)}In use ${p(c[1])} · ${swatch(MC.modified)}Modified ${p(c[2])} · ${swatch(MC.standby)}Standby ${p(c[3])} · ${swatch(MC.free)}Free ${p(c[4])}</div>`;
  for (let j = 5; j + 1 < c.length; j += 2) {
    html += `<div class="sub">${swatch(MC[USE_KIND[c[j]]])}${esc(USE_NAMES[c[j]] || 'Other')} ${p(c[j + 1])}</div>`;
  }
  if (c[0] < 995) html += `<div class="faint">Only ${p(c[0])} of this range is RAM; the rest is kept for hardware</div>`;
  return html;
}

function gsegTip(key) {
  const g = mem.last.gpu, s = mem.gsegs.find(x => x.key === key);
  if (!s) return '';
  const html = tipHead(MC[s.tone], s.name) + `<div>${fmtSize(s.bytes)} · ${share(s.bytes, g.vram)} of GPU memory</div>`;
  if (key === 'g:other') return html + tipDesc('Driver memory, and surfaces apps share, such as the desktop.');
  if (key === 'g:free') return html;
  return html + tipDesc('GPU memory this program has resident in the card.');
}

function showMemTip() {
  const hv = mem.hover, m = mem.last;
  if (!hv || !m || !m.ram) { mtip.hidden = true; return; }
  const html = hv.kind === 'seg' ? segTip(hv.key) : hv.kind === 'dim' ? dimTip(hv.key)
    : hv.kind === 'cell' ? cellTip(hv.i) : hv.kind === 'gseg' && m.gpu ? gsegTip(hv.key) : '';
  if (!html) { mtip.hidden = true; return; }
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
  mcv.style.cursor = mem.hover ? 'default' : '';
  showMemTip();
});
mcv.addEventListener('mouseleave', () => { mem.hover = null; mem.mouse = null; mtip.hidden = true; });

// ---------------------------------------------------------------------------
// Header numbers, legend, side lists, notices
// ---------------------------------------------------------------------------
function renderMemStats(inUse) {
  const m = mem.last, h = mem.hist[mem.hist.length - 1];
  const r = m.ram && m.ram.uses ? m.ram : null;
  $('ms-ram').textContent = r ? `${fmtGB(inUse)} / ${capacity(r.installed)} GB` : '–';
  $('ms-vram').textContent = m.gpu ? `${fmtGB(m.gpu.used)} / ${capacity(m.gpu.vram)} GB` : '–';
  const g3d = h.g3d != null ? pct(h.g3d / 100) : '–', cpu = h.cpu != null ? pct(h.cpu / 100) : '–';
  $('ms-3d').textContent = $('key-3d').textContent = g3d;
  $('ms-cpu').textContent = $('key-cpu').textContent = cpu;
  const c = m.commit;
  $('ms-commit').textContent = c ? `${fmtGB(c.used)} / ${capacity(c.limit)} GB` : '–';
  $('key-commit').textContent = c ? `${fmtGB(c.used)} GB` : '–';
  $('key-ramuse').textContent = r ? `${fmtGB(inUse)} GB` : '–';
  $('key-limit').textContent = c ? `${capacity(c.limit)} GB` : '–';
  $('ms-commit').parentElement.title = c ? `Peak since Windows started: ${fmtGB(c.peak)} GB` : '';
}

function renderLegend() {
  const items = mem.segs.map(s => `<span class="item">${swatch(kindColor(s.key))}${esc(kindOf(s.key).name)}</span>`);
  if (mem.gsegs.length) {
    items.push(`<span class="item"><span class="sw gpu" style="background:linear-gradient(90deg, ${MC.gpuA} 50%, ${MC.gpuB} 50%)"></span>GPU memory by process</span>`);
  }
  items.push('<span class="item muted">Hover for details</span>');
  $('mlegend').innerHTML = items.join('');
  if (ML) mlayout();   // the legend's height sets the room above it
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
  if (gpu && gpu.unowned >= MiB) {   // driver memory and surfaces shared between apps
    rows.push(meterRow('Not owned by one app', fmtSize(gpu.unowned), gpu.unowned / gpu.vram).replace('<li>', '<li class="other">'));
  }
  $('gprocs').innerHTML = rows.length ? rows.join('') : none;
}

function renderMemMsg() {
  const m = mem.last;
  let text = '';
  if (m.err) text = `Memory and GPU readings aren't available: ${m.err}`;
  else if (m.ram && m.ram.err && !mem.map) text = `${m.ram.err} Showing totals from the performance counters, without where in physical memory each kind sits.`;
  $('mmsg').textContent = text;
  $('mmsg').hidden = !text;
}

// ---------------------------------------------------------------------------
// Line charts over the last 60 seconds: utilization, and committed memory
// ---------------------------------------------------------------------------
const CH = { l: 44, r: 6, t: 8, b: 18 };   // plot margins, px

function readMemColors() {
  const cs = getComputedStyle(document.documentElement);
  const v = n => cs.getPropertyValue(n).trim();
  UC = { g3d: v('--c-3d'), cpu: v('--c-cpu'), commit: v('--c-commit'), ram: v('--text-2') };
  const pal = MPAL[darkQuery.matches ? 'dark' : 'light'];
  MC = Object.fromEntries(Object.entries(pal).map(([k, c]) => [k, rgb(oklch(...c))]));
  if (mem.segs.length) renderLegend();   // the legend's swatches change too
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
readMemColors();
// The tab may open before a resize notice arrives: lay out then too.
tabListeners.push(name => { if (name === 'mem') { mlayout(); drawCharts(); } });
darkQuery.addEventListener('change', () => { readMemColors(); drawCharts(); });
new ResizeObserver(mlayout).observe(mstage);
requestAnimationFrame(mframe);
