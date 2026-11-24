'use strict';

// ---------------------------------------------------------------------------
// Memory & GPU tab: all of RAM as one bar from 0 to the installed size, split
// by what each part holds and labelled on leader lines; memory by program;
// the memory modules, and what each physical address holds; GPU memory by
// process; and charts of utilization and committed memory. Uses the theme and
// helpers from app.js (T, oklch, rgb, rgba, esc, $, loadPref, savePref,
// activeTab, darkQuery) and the module pictures from dimm.js.
// ---------------------------------------------------------------------------
const GiB = 1073741824, MiB = 1048576;
const HISTORY_S = 60;                              // seconds of samples kept for the charts
const M_INTERVALS = [50, 100, 250, 500, 1000];     // ms: 20, 10, 4, 2, 1 samples a second, as on the CPU tab
const M_TEXT_MS = 250;                             // numbers and lists redraw at most 4 times a second
const M_TAU = 0.25;                                // seconds for the bars to catch up with a sample

// Geometry, CSS px
const MPAD = 28;                                   // left and right margin
const MBAR_H = 52, MRISE = 34;                     // RAM bar height; leader-line rise to the labels
const GBAR_H = 26, GRISE = 26;                     // the same for the programs and GPU bars
const MLBL_H = 30, MLBL_GAP = 10, MRUN = 16;       // label height, space between labels, sideways reach
const MDIM_H = 22, MAXIS_H = 22, MHEAD_H = 26, MSECTION_GAP = 26;

// What the page database says pages in use hold (MMPFNUSE_* order)
const USE_NAMES = ['Process memory', 'Mapped files', 'Shared memory', 'Page tables', 'Paged pool',
  'Nonpaged pool', 'System mappings', 'Session memory', 'File system metadata', 'Locked by apps (AWE)',
  'Locked by drivers', 'Kernel stacks'];

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
    progA: [0.56, 0.13, 152], progB: [0.77, 0.11, 152], progOther: [0.40, 0.015, 152],
  },
  dark: {
    proc: [0.66, 0.14, 152], comp: [0.84, 0.10, 152], mapped: [0.53, 0.10, 172], shared: [0.75, 0.14, 132],
    paged: [0.64, 0.16, 355], nonpaged: [0.81, 0.10, 355], ptables: [0.53, 0.14, 350], kernel: [0.72, 0.15, 328],
    modified: [0.83, 0.14, 78], standby: [0.66, 0.13, 248], free: [0.36, 0.014, 240], hw: [0.52, 0.004, 90],
    gpuA: [0.68, 0.15, 45], gpuB: [0.84, 0.11, 65], gpuOther: [0.52, 0.015, 60],
    progA: [0.66, 0.14, 152], progB: [0.84, 0.10, 152], progOther: [0.50, 0.015, 152],
  },
};
let MC = {};                                       // palette name -> css color
const kindOf = k => KINDS[k] || { name: k, tip: '' };
const kindColor = k => MC[kindOf(k).color || k] || MC.hw;

const mstage = $('mstage'), mcv = $('mcv'), mctx = mcv.getContext('2d'), mtip = $('mtip');
const mem = {
  last: null,            // latest sample from the server
  segs: [], psegs: [], gsegs: [],   // RAM, programs and GPU bars: [{ key, bytes, shown }], left to right
  map: null,             // latest address map: { slice, cells }
  dimms: null,           // memory slots and modules, from the firmware
  phys: null,            // where RAM is: [[start, end]] physical addresses
  space: null,           // the address space drawn: { ranges, end }
  pin: null,             // an address to point out
  page: null,            // what that address's page holds: { address, info }
  copied: null,          // the address copied last, for a moment
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
const fmtModule = b => (b % GiB === 0 ? `${b / GiB} GB` : fmtSize(b));   // module sizes: 16 GB, not 16.0 GB
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
  out.push({ key: 'hw', bytes: Math.max(0, r.installed - r.total), hatch: true });
  return out;
}

// Programs by private working set, then what isn't any one program's, on the
// RAM bar's scale (0 to the installed size), so the two line up.
function progParts(r, p) {
  const out = p.top.map(([name, bytes, ws, commit, procs], i) =>
    ({ key: `r:${name}`, name, bytes, ws, commit, procs, tone: i % 2 ? 'progB' : 'progA' }));
  if (p.other[0] > 0) {
    out.push({ key: 'r:other', name: 'Other programs', bytes: p.other[0], procs: p.other[1], count: p.other[2],
               tone: 'progOther', hatch: true });
  }
  const inUse = msum(r.uses.map(u => u[1]));
  out.push({ key: 'r:shared', name: 'Shared and system', bytes: Math.max(0, inUse - p.total), tone: 'paged' },
    { key: 'r:idle', name: 'Not in use', bytes: Math.max(0, r.total - inUse), tone: 'free' },
    { key: 'r:hw', name: 'Hardware reserved', bytes: Math.max(0, r.installed - r.total), tone: 'hw', hatch: true });
  return out;
}

function gpuParts(g) {
  const out = g.procs.map(([name, bytes], i) => ({ key: `p:${name}`, name, bytes, tone: i % 2 ? 'gpuB' : 'gpuA' }));
  if (g.unowned > 0) {
    out.push({ key: 'g:other', name: 'Not owned by one app', bytes: g.unowned, tone: 'gpuOther', hatch: true });
  }
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
  const shape = () => [!!mem.map, !!mem.phys, !!mem.dimms, mem.dimms ? mem.dimms.modules.length : 0,
    mem.psegs.length > 0, mem.gsegs.length > 0].join();
  const before = shape();
  if (!ram) mem.map = mem.dimms = mem.phys = mem.space = null;
  else {
    if (ram.map) mem.map = ram.map;   // a few times a second; samples between keep the last
    else if (ram.err) mem.map = null;
    if (ram.dimms) mem.dimms = ram.dimms;   // every few seconds: these don't change
    if (ram.phys) mem.phys = ram.phys;
    if (ram.page && ram.page.address === mem.pin) mem.page = ram.page;   // not another window's
    if (ram.dimms || ram.phys || ram.map && !mem.phys) mem.space = physSpace();
  }
  mem.segs = ram && ram.uses ? retarget(mem.segs, ramParts(ram)) : [];
  mem.psegs = ram && ram.uses && ram.progs ? retarget(mem.psegs, progParts(ram, ram.progs)) : [];
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

  const kinds = mem.segs.map(s => s.key).join(',') + '|' + shape();
  if (kinds !== mem.kindsKey) {
    mem.kindsKey = kinds;
    renderLegend();
  }
  if (!ML || before !== shape()) mlayout();
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
// Layout, top to bottom: system memory, memory by program, the memory
// modules and physical addresses, GPU memory. The tab scrolls when it's
// taller than the window.
// ---------------------------------------------------------------------------
const MSTRIP_H = 26, DIMM_MIN_W = 120, DIMM_MAX_W = 330, DIMM_GAP = 12, CHAN_GAP = 26;
const CHAN_H = 20, STRIP_GAP = 28, PAXIS_H = 38, NOTE_H = 20;
const slotGroup = m => `${m.socket}|${m.channel}`;

// Sticks in slot order, in as few rows as keep each at least DIMM_MIN_W wide,
// with extra space between channels.
function layoutSticks(mods, top) {
  const gapsIn = row => row.reduce((g, m, i) => g + (i ? (slotGroup(m) !== slotGroup(row[i - 1]) ? CHAN_GAP : DIMM_GAP) : 0), 0);
  const widthFor = row => (ML.w - gapsIn(row)) / row.length;
  let perRow = mods.length;
  while (perRow > 1 && widthFor(mods.slice(0, perRow)) < DIMM_MIN_W) perRow--;
  perRow = Math.ceil(mods.length / Math.ceil(mods.length / perRow));   // rows of about the same length
  const items = [];
  let y = top;
  for (let k = 0; k < mods.length; k += perRow) {
    const row = mods.slice(k, k + perRow);
    const w = Math.floor(Math.min(DIMM_MAX_W, widthFor(mods.slice(0, perRow)), widthFor(row)));
    let x = ML.x0 + (ML.w - (w * row.length + gapsIn(row))) / 2;
    y += CHAN_H;
    row.forEach((m, j) => {
      if (j) x += slotGroup(m) !== slotGroup(row[j - 1]) ? CHAN_GAP : DIMM_GAP;
      items.push({ x, y, w, h: STICK_H, m, i: k + j, row: k });
      x += w;
    });
    y += STICK_H + 8;
  }
  return { items, bottom: y - 8 };
}

function mlayout() {
  const r = mstage.getBoundingClientRect();
  if (!r.width || !r.height) return;
  mW = mstage.clientWidth;   // inside the scroll bar
  mH = r.height;
  const x0 = MPAD, x1 = Math.max(MPAD + 100, mW - MPAD);
  const zone = MRISE + MLBL_H, gzone = GRISE + MLBL_H;
  ML = { x0, x1, w: x1 - x0 };
  let y = 16;

  ML.ram = { headY: y + 9, bar: { y: y + MHEAD_H + zone, h: MBAR_H } };
  const b = ML.ram.bar;
  ML.ram.dimY = [0, 1, 2].map(i => b.y + MBAR_H + zone + 8 + i * MDIM_H + MDIM_H / 2);
  ML.ram.axisY = b.y + MBAR_H + zone + 8 + 3 * MDIM_H + 4;
  y = ML.ram.axisY + MAXIS_H - 4;

  ML.progs = null;
  if (mem.psegs.length) {
    y += MSECTION_GAP;
    const top = y + MHEAD_H + gzone;
    ML.progs = { headY: y + 9, bar: { y: top, h: GBAR_H }, axisY: top + GBAR_H + gzone + 4 };
    y = ML.progs.axisY + MAXIS_H - 4;
  }

  ML.phys = null;
  if (mem.space) {
    y += MSECTION_GAP;
    const p = { headY: y + 9 };
    y += MHEAD_H;
    if (mem.dimms && mem.dimms.modules.length) {
      p.sticks = layoutSticks(mem.dimms.modules, y);
      y = p.sticks.bottom + STRIP_GAP;
    } else y += 14;   // room for an address pointed out
    p.strip = { y, h: MSTRIP_H };
    p.axisY = p.strip.y + MSTRIP_H + 4;
    p.noteY = p.axisY + PAXIS_H + 4;
    ML.phys = p;
    y = p.noteY + NOTE_H - 10;
  }

  ML.gpu = null;
  if (mem.gsegs.length) {
    y += MSECTION_GAP;
    const top = y + MHEAD_H + gzone;
    ML.gpu = { headY: y + 9, labels: true, bar: { y: top, h: GBAR_H }, axisY: top + GBAR_H + 4 };
    y = ML.gpu.axisY + MAXIS_H - 4;
  }

  const legend = $('mlegend');
  legend.style.top = (y + 18) + 'px';
  const h = Math.max(mH, y + 18 + legend.offsetHeight + 14);
  mdpr = window.devicePixelRatio || 1;
  mcv.width = Math.round(mW * mdpr);
  mcv.height = Math.round(h * mdpr);
  mcv.style.width = mW + 'px';
  mcv.style.height = h + 'px';
  ML.h = h;
}

// ---------------------------------------------------------------------------
// Drawing helpers
// ---------------------------------------------------------------------------
function mtext(str, x, y, size, color, align = 'left', weight = 400, font = T.font) {
  mctx.font = `${weight} ${size}px ${font}`;
  mctx.textAlign = align;
  mctx.textBaseline = 'middle';
  mctx.fillStyle = color;
  mctx.fillText(str, x, y);
  return mctx.measureText(str).width;
}
function mwidth(str, size, weight = 400, font = T.font) {
  mctx.font = `${weight} ${size}px ${font}`;
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
    if (g.s.hatch) hatch(x0, y, w, h, T.bg);
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

// Memory by program: the biggest programs, the rest, and what no one program owns.
function drawProgs() {
  const r = mem.last.ram, p = r.progs, sec = ML.progs, bar = { x: ML.x0, ...sec.bar };
  const count = p.top.length + p.other[2];
  drawHead(sec.headY, 'Memory by program',
    `the ${p.top.length} biggest of ${count} programs, by private working set · on the same scale as the bar above`);
  const list = geom(mem.psegs, ML.x0, ML.w);
  const hk = mem.hover && mem.hover.kind === 'pseg' ? mem.hover.key : null;
  drawSegments(list, bar, s => MC[s.tone], hk, 'pseg');
  const placed = placeLabels(list, ['up', 'down'], s => [s.name, `${fmtSize(s.bytes)} · ${share(s.bytes, r.installed)}`]);
  mdrawLabels(placed, bar, GRISE, hk, 'pseg');
  drawAxis(sec.axisY, r.installed, ML.x0, ML.w);
}

// ---------------------------------------------------------------------------
// Physical memory: the modules in their slots, and what each address holds.
// Addresses are real physical addresses: RAM starts and ends where Windows
// says it does, holes included.
// ---------------------------------------------------------------------------
const PAGE_SIZE = 4096;
// What the page database says pages in use hold -> the RAM bar's kinds
const USE_KIND = ['proc', 'mapped', 'shared', 'ptables', 'paged', 'nonpaged', 'kernel', 'kernel', 'kernel',
  'proc', 'kernel', 'kernel'];
const PAGE_LISTS = ['Free, zeroed', 'Free', 'Standby', 'Modified', 'Modified, not to be written', 'Bad', 'In use', 'In use, being moved'];
const RANKS = { 1: 'Single rank', 2: 'Dual rank', 4: 'Quad rank', 8: 'Octal rank' };
// Colors for the modules, one hue per channel, two tones for the slots in it
const CHAN_PAL = {
  light: [[0.58, 0.11, 200], [0.55, 0.15, 295], [0.66, 0.15, 55], [0.60, 0.12, 120], [0.58, 0.15, 10], [0.62, 0.11, 240]],
  dark: [[0.70, 0.11, 200], [0.68, 0.14, 295], [0.76, 0.14, 55], [0.72, 0.12, 120], [0.70, 0.14, 10], [0.74, 0.10, 240]],
};

function slotName(m, i) {
  const parts = [m.channel && !/channel/i.test(m.locator) ? `Channel ${m.channel}` : m.bank, m.locator];
  return parts.filter(Boolean).join(' · ') || `Slot ${i + 1}`;
}

// Where RAM is in physical address space: [start, end) ranges, and the end of the last.
function physSpace() {
  let ranges = mem.phys;
  if (!ranges || !ranges.length) {
    const map = mem.map, d = mem.dimms;
    ranges = [];
    if (map) {
      map.cells.forEach((c, i) => {
        if (!c) return;
        const last = ranges[ranges.length - 1];
        if (last && last[1] === i * map.slice) last[1] = (i + 1) * map.slice;
        else ranges.push([i * map.slice, (i + 1) * map.slice]);
      });
    } else if (d && d.arrays.length) ranges = d.arrays;
    if (!ranges.length) ranges = [[0, mem.last.ram.installed]];
  }
  return { ranges, end: ranges[ranges.length - 1][1] };
}

const addrX = a => ML.x0 + ML.w * a / mem.space.end;

// Each filled module's color: a hue per channel, two tones for the slots in it.
function moduleColors(items) {
  const filled = items.filter(it => it.m.size);
  const groups = [...new Set(filled.map(it => slotGroup(it.m)))];
  const pal = CHAN_PAL[darkQuery.matches ? 'dark' : 'light'];
  const nth = new Map(), colors = new Map();
  for (const it of filled) {
    const g = groups.indexOf(slotGroup(it.m)), k = nth.get(g) || 0;
    nth.set(g, k + 1);
    const [L, C, h] = pal[g % pal.length];
    colors.set(it.i, oklch(k % 2 ? Math.min(0.9, L + 0.12) : L, C, h));
  }
  return colors;
}

function hexAddr(a) {   // wide enough for the highest address, so they line up
  const digits = Math.max(8, Math.ceil(Math.log2(mem.space.end + 1) / 4));
  return '0x' + Math.max(0, Math.floor(a)).toString(16).toUpperCase().padStart(digits, '0');
}
const hexRange = (s, e) => `${hexAddr(s)} – ${hexAddr(e - 1)}`;   // both ends included, as debuggers show ranges
const hexPage = a => '0x' + Math.floor(a / PAGE_SIZE).toString(16).toUpperCase();

function physSummary(d) {
  const { end } = mem.space;
  const to = `${hexAddr(0)} to ${hexAddr(end - 1)}`;
  if (!d) return `by physical address, ${to}`;
  const filled = d.modules.filter(m => m.size);
  const sizes = filled.map(m => m.size);
  const same = sizes.length && sizes.every(s => s === sizes[0]);
  const types = [...new Set(filled.map(m => m.type).filter(Boolean))];
  const what = filled.length
    ? `${same ? `${filled.length} × ${fmtModule(sizes[0])}` : sizes.map(fmtModule).join(' + ')}${types.length === 1 ? ` ${types[0]}` : ''}`
    : 'No modules reported';
  return [what, `${filled.length} of ${d.slots} slots`, to].join(' · ');
}

function fitText(str, maxW, size, weight = 400, font = T.font) {
  if (mwidth(str, size, weight, font) <= maxW) return str;
  while (str.length > 1 && mctx.measureText(str + '…').width > maxW) str = str.slice(0, -1);
  return str + '…';
}

function drawStick(it, color, hot) {
  const M = STICK_MARGIN, pic = stickPicture(it.m, slotName(it.m, it.i), it.w, color, mdpr);
  const snap = v => Math.round(v * mdpr) / mdpr;
  mctx.drawImage(pic, snap(it.x - M), snap(it.y - M), pic.width / mdpr, pic.height / mdpr);
  hit(it.x, it.y, it.x + it.w, it.y + it.h, { kind: 'dimm', key: it.i });
  if (hot) {
    mctx.strokeStyle = T.hl;
    mctx.lineWidth = 2;
    mctx.beginPath();
    mctx.roundRect(it.x - 3, it.y - 2, it.w + 6, it.h + 5, 6);
    mctx.stroke();
  }
}

function drawPhys() {
  const p = ML.phys, hv = mem.hover;
  drawHead(p.headY, 'Physical memory', physSummary(mem.dimms));
  if (p.sticks) {
    const colors = moduleColors(p.sticks.items);
    drawChannels(p.sticks.items);
    for (const it of p.sticks.items) drawStick(it, colors.get(it.i) || [128, 128, 128], !!hv && hv.kind === 'dimm' && hv.key === it.i);
  }
  // What each address holds
  const { y, h } = p.strip;
  mctx.fillStyle = T.gap;
  mctx.fillRect(ML.x0, y, ML.w, h);
  if (mem.map) drawCells(y, h);
  else {
    mctx.fillStyle = MC.hw;
    for (const [s, e] of mem.space.ranges) mctx.fillRect(addrX(s), y, Math.max(1, addrX(e) - addrX(s)), h);
  }
  hit(ML.x0, y - 2, ML.x1, y + h + 2, { kind: 'cell' });
  mctx.strokeStyle = T.grid;
  mctx.lineWidth = 1;
  mctx.strokeRect(ML.x0 - 0.5, y - 0.5, ML.w + 1, h + 1);
  if (hv && hv.kind === 'cell') {
    const at = cellAt(hv.a);
    moutline(addrX(at.s), addrX(at.e), { y, h });
  }
  drawAddrAxis(p.axisY);
  const note = mem.pin != null ? pinText() : mem.map ? 'What each physical address holds. Gaps have no RAM.'
    : 'Where RAM is. What each address holds needs administrator rights.';
  mtext(fitText(note, ML.w, 11.5), ML.x0, p.noteY, 11.5, mem.pin != null ? T.ink : T.muted);
  if (mem.pin != null) drawPin(p, addrX(mem.pin));
}

// Channel names over each run of slots on one channel
function drawChannels(items) {
  const multiSocket = new Set(mem.dimms.modules.map(m => m.socket)).size > 1;
  for (let a = 0; a < items.length;) {
    let b = a;
    while (b + 1 < items.length && items[b + 1].row === items[a].row && slotGroup(items[b + 1].m) === slotGroup(items[a].m)) b++;
    const m = items[a].m;
    const name = [multiSocket && m.socket ? `CPU ${m.socket}` : '', m.channel ? `Channel ${m.channel}` : ''].filter(Boolean).join(' · ');
    if (name) {
      const x0 = items[a].x, x1 = items[b].x + items[b].w, ly = items[a].y - 11;
      mctx.strokeStyle = T.grid;
      mctx.lineWidth = 1;
      mctx.beginPath();
      mctx.moveTo(x0, ly + 4.5);
      mctx.lineTo(x1, ly + 4.5);
      mctx.stroke();
      const tw = mwidth(name, 11, 600), mid = (x0 + x1) / 2;
      mctx.fillStyle = T.bg;
      mctx.fillRect(mid - tw / 2 - 6, ly - 6, tw + 12, 12);
      mtext(name, mid, ly, 11, T.ink2, 'center', 600);
    }
    a = b + 1;
  }
}

// Addresses in hex under the strip, with GB below.
function drawAddrAxis(y) {
  const { end } = mem.space, gap = mwidth(hexAddr(end), 10.5, 400, UC.mono) + 16;
  let step = 16 * MiB;
  while (ML.w * step / end < gap) step *= 2;
  mctx.strokeStyle = T.grid;
  mctx.lineWidth = 1;
  mctx.beginPath();
  mctx.moveTo(ML.x0, y + 0.5);
  mctx.lineTo(ML.x1, y + 0.5);
  mctx.stroke();
  for (let a = 0; a <= end; a += step) {
    const x = Math.round(addrX(a)) + 0.5, tx = mclamp(x, gap / 2 - 6, mW - gap / 2 + 6);   // whole, at the edges too
    mctx.strokeStyle = T.muted;
    mctx.beginPath();
    mctx.moveTo(x, y);
    mctx.lineTo(x, y + 4);
    mctx.stroke();
    mtext(hexAddr(a), tx, y + 12, 10.5, T.ink2, 'center', 400, UC.mono);
    mtext(`${+(a / GiB).toFixed(3)} GB`, tx, y + 26, 10.5, T.muted, 'center');
  }
}

// The address typed in, pointed out on the strip
function drawPin(p, x) {
  const text = hexAddr(mem.pin), tw = mwidth(text, 11, 600, UC.mono);
  const ty = p.strip.y - 14, tx = mclamp(x, tw / 2 + 12, mW - tw / 2 - 12);
  mctx.strokeStyle = T.hl;
  mctx.lineWidth = 1.5;
  mctx.beginPath();
  mctx.moveTo(x, p.strip.y - 5);
  mctx.lineTo(x, p.strip.y + p.strip.h);
  mctx.stroke();
  mctx.fillStyle = T.hl;
  mctx.beginPath();
  mctx.roundRect(tx - tw / 2 - 7, ty - 9, tw + 14, 18, 4);
  mctx.fill();
  mtext(text, tx, ty + 0.5, 11, T.bg, 'center', 600, UC.mono);
}

// What its page holds, in words
function pinText() {
  const a = mem.pin, out = [hexAddr(a), `page frame ${hexPage(a)}`];
  if (!mem.space.ranges.some(([s, e]) => a >= s && a < e)) return [...out, 'no RAM at this address: it belongs to hardware or the firmware'].join(' · ');
  if (!mem.map) return [...out, 'RAM'].join(' · ');
  const info = mem.page && mem.page.address === a && mem.page.info;
  if (info) {
    const [list, use, priority, va] = info;
    out.push(PAGE_LISTS[list]);
    if (list >= 2 && USE_NAMES[use]) out.push(USE_NAMES[use]);
    if (list === 2 || list >= 6) out.push(`priority ${priority}`);
    if (va) out.push(`mapped at virtual address 0x${va}`);
  }
  return out.join(' · ');
}

// One column per slice of address space, its height split by how that
// slice's RAM is used.
function drawCells(y, h) {
  const { cells, slice } = mem.map, { end } = mem.space;
  const X = i => Math.round(addrX(Math.min(end, i * slice)));
  for (let i = 0; i < cells.length; i++) {
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
}

// What's drawn at an address: { s, e, c (the cell, or null) }. Without a map
// it's the RAM range, or the gap between ranges.
function cellAt(a) {
  const map = mem.map, { end, ranges } = mem.space;
  if (map) {
    const i = Math.min(map.cells.length - 1, Math.floor(a / map.slice));
    return { s: i * map.slice, e: Math.min(end, (i + 1) * map.slice), c: map.cells[i] };
  }
  const r = ranges.find(q => a >= q[0] && a < q[1]);
  return r ? { s: r[0], e: r[1], c: null } : { ...gapAt(a), c: null };
}

function gapAt(a) {   // the addresses without RAM around an address
  let s = 0, e = mem.space.end;
  for (const [x, y] of mem.space.ranges) {
    if (y <= a) s = Math.max(s, y);
    if (x > a) e = Math.min(e, x);
  }
  return { s, e };
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
  if (ML.progs && mem.psegs.length) drawProgs();
  if (ML.phys && mem.space) drawPhys();
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
  for (const list of [mem.segs, mem.psegs, mem.gsegs]) for (const s of list) s.shown += (s.bytes - s.shown) * k;
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
    if (h.kind === 'cell') return { kind: 'cell', a: mclamp((mx - ML.x0) / ML.w * mem.space.end, 0, mem.space.end - 1) };
    return { kind: h.kind, key: h.key };
  }
  return null;
}

const swatch = color => `<span class="sw" style="background:${color}"></span>`;
const tipHead = (color, title, sub = '') => `<div class="head">${color ? swatch(color) : ''}${esc(title)}${sub ? ` <span class="faint">${esc(sub)}</span>` : ''}</div>`;
const tipDesc = text => `<div class="desc">${esc(text)}</div>`;
const tipMono = text => `<span class="mono">${esc(text)}</span>`;

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

function cellTip(a) {
  const at = cellAt(a), c = at.c, p = v => `${Math.round(v / 10)}%`;
  const head = `<div class="head">${tipMono(hexRange(at.s, at.e))}</div>`;
  const size = `<div class="sub">${fmtSize(at.e - at.s)} · page frames ${hexPage(at.s)}–${hexPage(at.e - 1)}</div>`;
  const ram = mem.space.ranges.filter(([s, e]) => s < at.e && e > at.s);
  if (!ram.length) {
    const g = gapAt(a);
    return head + size + tipDesc('No RAM at these addresses: they belong to hardware such as the graphics card, or to the firmware.') +
      `<div class="sub">The whole gap: ${tipMono(hexRange(g.s, g.e))} (${fmtSize(g.e - g.s)})</div>`;
  }
  let html = head + size;
  if (c) {
    html += `<div>${swatch(MC.proc)}In use ${p(c[1])} · ${swatch(MC.modified)}Modified ${p(c[2])} · ${swatch(MC.standby)}Standby ${p(c[3])} · ${swatch(MC.free)}Free ${p(c[4])}</div>`;
    for (let j = 5; j + 1 < c.length; j += 2) {
      html += `<div class="sub">${swatch(MC[USE_KIND[c[j]]])}${esc(USE_NAMES[c[j]] || 'Other')} ${p(c[j + 1])}</div>`;
    }
  }
  if (ram.length > 1 || ram[0][0] > at.s || ram[0][1] < at.e) {   // only some of it is RAM
    html += ram.slice(0, 4).map(([s, e]) => `<div class="sub">RAM at ${tipMono(hexRange(Math.max(s, at.s), Math.min(e, at.e)))}</div>`).join('') +
      (ram.length > 4 ? `<div class="faint">and ${ram.length - 4} more ranges</div>` : '');
  }
  return html + `<div class="faint">${mem.copied && performance.now() < mem.copied.until ? `Copied ${esc(mem.copied.text)}` : 'Click to copy the start address'}</div>`;
}

function gsegTip(key) {
  const g = mem.last.gpu, s = mem.gsegs.find(x => x.key === key);
  if (!s) return '';
  const html = tipHead(MC[s.tone], s.name) + `<div>${fmtSize(s.bytes)} · ${share(s.bytes, g.vram)} of GPU memory</div>`;
  if (key === 'g:other') return html + tipDesc('Driver memory, and surfaces apps share, such as the desktop.');
  if (key === 'g:free') return html;
  return html + tipDesc('GPU memory this program has resident in the card.');
}

function psegTip(key) {
  const r = mem.last.ram, s = mem.psegs.find(x => x.key === key);
  if (!s) return '';
  let html = tipHead(MC[s.tone], s.name) + `<div>${fmtSize(s.bytes)} · ${share(s.bytes, r.installed)} of RAM</div>`;
  if (key === 'r:other') {
    return html + `<div class="sub">${s.count} more programs, ${s.procs} processes</div>` +
      tipDesc('The private working sets of every program not shown on its own.');
  }
  if (key === 'r:shared') {
    return html + tipDesc("Memory in use that no one program owns: files mapped into memory, memory programs share, and Windows' own memory (pools, page tables, drivers).");
  }
  if (key === 'r:idle') return html + tipDesc('Modified, standby and free memory: see the bar at the top.');
  if (key === 'r:hw') return html + tipDesc(KINDS.hw.tip);
  html += `<div class="sub">${s.procs} process${s.procs === 1 ? '' : 'es'} · working set ${fmtSize(s.ws)} · committed ${fmtSize(s.commit)}</div>`;
  return html + tipDesc("Private working set: RAM only this program's processes use, as in Task Manager's Memory column. The working set adds memory it shares with other programs; committed is what it has been promised, in RAM or the page file.");
}

function dimmTip(i) {
  const d = mem.dimms, m = d && d.modules[i];
  if (!m) return '';
  const slot = slotName(m, i);
  if (!m.size) {
    return tipHead('', m.size === 0 ? 'Empty slot' : 'Module size not reported', slot) +
      (d.max ? `<div class="sub">This system takes up to ${fmtSize(d.max)} in ${d.slots} slots</div>` : '');
  }
  const rows = [
    [m.part ? `Part ${m.part}` : '', m.maker && m.maker !== m.brand ? `made by ${m.maker}` : ''],
    [m.serial ? `Serial number ${m.serial}` : 'No serial number reported', ''],
    [`${fmtModule(m.size)} ${[m.type, m.form].filter(Boolean).join(' ')}`.trim(), ''],
    [m.configured && m.speed && m.configured !== m.speed ? `Runs at ${m.configured} MT/s; rated ${m.speed} MT/s`
      : m.configured || m.speed ? `${m.configured || m.speed} MT/s` : '', ''],
    [[RANKS[m.rank], m.volts ? `${(m.volts / 1000).toFixed(2)} V` : '', m.width ? `${m.width}-bit` : '', m.ecc ? 'ECC' : '']
      .filter(Boolean).join(' · '), ''],
  ];
  let html = tipHead('', m.brand || m.maker || 'Memory module', slot);
  for (const [a, b] of rows) if (a || b) html += `<div class="sub">${esc([a, b].filter(Boolean).join(' · '))}</div>`;
  return html;
}

function showMemTip() {
  const hv = mem.hover, m = mem.last;
  if (!hv || !m || !m.ram) { mtip.hidden = true; return; }
  const html = hv.kind === 'seg' ? segTip(hv.key) : hv.kind === 'dim' ? dimTip(hv.key)
    : hv.kind === 'cell' && mem.space ? cellTip(hv.a) : hv.kind === 'pseg' ? psegTip(hv.key)
    : hv.kind === 'dimm' ? dimmTip(hv.key) : hv.kind === 'gseg' && m.gpu ? gsegTip(hv.key) : '';
  if (!html) { mtip.hidden = true; return; }
  mtip.innerHTML = html;
  mtip.hidden = false;
  // The tip sits in the scrolling area: place it in content coordinates, inside what's visible.
  const sr = mstage.getBoundingClientRect(), top = mstage.scrollTop;
  let x = mem.mouse[0] - sr.left + 14, y = mem.mouse[1] - sr.top + top + 14;
  if (x + mtip.offsetWidth > mstage.clientWidth - 8) x -= mtip.offsetWidth + 28;
  if (y + mtip.offsetHeight > top + mstage.clientHeight - 8) y -= mtip.offsetHeight + 28;
  mtip.style.left = Math.max(4, x) + 'px';
  mtip.style.top = Math.max(top + 4, y) + 'px';
}

mcv.addEventListener('mousemove', ev => {
  const r = mcv.getBoundingClientRect();
  mem.mouse = [ev.clientX, ev.clientY];
  mem.hover = memAt(ev.clientX - r.left, ev.clientY - r.top);
  mcv.style.cursor = !mem.hover ? '' : mem.hover.kind === 'cell' ? 'pointer' : 'default';
  showMemTip();
});
mcv.addEventListener('mouseleave', () => { mem.hover = null; mem.mouse = null; mtip.hidden = true; });
mcv.addEventListener('click', () => {
  if (mem.hover && mem.hover.kind === 'cell') copyText(hexAddr(cellAt(mem.hover.a).s));
});

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {   // no clipboard access: the old way
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.append(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  mem.copied = { text, until: performance.now() + 2000 };
  showMemTip();
}

// ---------------------------------------------------------------------------
// The address box: point out a physical address typed in hex, as a debugger
// shows it, and say what its page holds
// ---------------------------------------------------------------------------
function lookUp(address) {
  fetch('/api/sys', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ address }),
  }).catch(() => { /* it's still pointed out */ });
}

function pinAddress(str) {
  const t = str.trim().replace(/^0x/i, '').replace(/[`'_\s,]/g, '').replace(/h$/i, '').replace(/^0+(?=.)/, '');
  const a = /^[0-9a-f]{1,13}$/i.test(t) ? parseInt(t, 16) : NaN;
  const box = $('mctl-addr');
  const ok = mem.space && ML && ML.phys && a < mem.space.end;
  box.classList.toggle('bad', !ok);
  if (!ok) {
    box.title = mem.space ? `An address in hex, up to ${hexAddr(mem.space.end - 1)}` : 'No physical addresses to show yet';
    return;
  }
  mem.pin = a;
  lookUp(a);
  const p = ML.phys, top = p.strip.y - 40, bottom = p.noteY + 20;   // bring it into view
  if (top < mstage.scrollTop || bottom > mstage.scrollTop + mH) mstage.scrollTop = Math.max(0, bottom - mH);
}

function clearPin() {
  mem.pin = mem.page = null;
  lookUp(null);
  const box = $('mctl-addr');
  box.value = '';
  box.classList.remove('bad');
}

$('mctl-addr').addEventListener('keydown', ev => {
  if (ev.key === 'Enter') { if (ev.target.value.trim()) pinAddress(ev.target.value); else clearPin(); }
  else if (ev.key === 'Escape') clearPin();
  else return;
  ev.preventDefault();
});
$('mctl-addr').addEventListener('input', ev => ev.target.classList.remove('bad'));

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
  const pair = (a, b, label) => `<span class="item"><span class="sw" style="background:linear-gradient(90deg, ${a} 50%, ${b} 50%)"></span>${label}</span>`;
  const items = mem.segs.map(s => `<span class="item">${swatch(kindColor(s.key))}${esc(kindOf(s.key).name)}</span>`);
  if (mem.psegs.length) items.push(pair(MC.progA, MC.progB, 'Programs'));
  if (mem.gsegs.length) items.push(pair(MC.gpuA, MC.gpuB, 'GPU memory by process'));
  items.push('<span class="item muted">Hover for details · scroll for more</span>');
  $('mlegend').innerHTML = items.join('');
  if (ML) mlayout();   // the legend sits below everything, so the height depends on it
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
  UC = { g3d: v('--c-3d'), cpu: v('--c-cpu'), commit: v('--c-commit'), ram: v('--text-2'), mono: v('--mono') };
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
