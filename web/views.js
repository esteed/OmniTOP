'use strict';

// ---------------------------------------------------------------------------
// The pages of one machine, this PC or a remote one: Overview, CPU, Disk and
// Network, drawn on one canvas. (Memory is memory.js's, Ports app.js's.) Uses
// perf.js's graphs, disk.js's panels, memory.js's bar, and viewMachine() and activeTab.
// Every top-level name here starts with v or V, so none can clash with the
// other scripts (they all share one global scope).
// ---------------------------------------------------------------------------
const VPAGES = ['overview', 'cpu', 'disk', 'network'];
const vstage = $('vstage'), vtip = $('vtip'), vS = makeSurface($('vcv'));
const V = { margin: 16, top: 30, gap: 16, title: 26, strip: 96, stripMin: 64, foot: 24,
  netRow: 148, netGap: 12, netCol: 28, barsGap: 18 };   // the Network page: each adapter's graph, then its bars
let vQueued = false, vMem = null, vMemHover = null, vMouse = null;

function vInvalidate() {
  if (vQueued || !VPAGES.includes(activeTab)) return;
  vQueued = true;
  requestAnimationFrame(() => { vQueued = false; if (VPAGES.includes(activeTab)) vDraw(); });
}
perfDrawers.push(vInvalidate);

const vColumns = (items, w) => (w >= 1500 && items > 8 ? 3 : w >= 860 && items > 3 ? 2 : 1);

function vDraw() {
  const r = vstage.getBoundingClientRect();
  if (!r.width || !r.height) return;
  const s = pm.get(viewMachine()), info = s && s.info;
  const w = vstage.clientWidth, view = r.height, x = V.margin, inner = w - 2 * V.margin, span = pVisible();
  const right = w - V.margin;

  // How tall it is: the window, or more if the strips need it
  let h = view, cols = 1, rows = 0, lay = null;
  const detailed = info && info.io !== false;
  if (info && activeTab === 'disk') {
    const foot = vSilent(info) ? V.foot : 0;
    lay = dkLayout(info.disks.length, inner, view - V.top - V.margin - foot, detailed);
    h = Math.max(view, V.top + lay.rows * lay.h + (lay.rows - 1) * V.gap + V.margin + foot);
  } else if (info && activeTab === 'network') {
    const n = info.nets.length;
    cols = vColumns(n, inner);
    rows = Math.ceil(n / cols);
    h = Math.max(view, V.top + rows * V.netRow + (rows - 1) * V.netGap + V.margin);
  } else if (info && activeTab === 'overview') {
    h = Math.max(view, V.top + V.title + 2 * V.gap + MEMBAR_FULL + Math.max(150, info.nets.length * V.stripMin) + V.margin);
  }
  startSurface(vS, w, h);
  vMem = null;
  if (!info) {
    pText('Waiting for the first sample…', w / 2, view / 2, 13, T.muted, 400, 'center');
    return;
  }
  if (!pLive(s)) pctx.globalAlpha = pAlpha = pDim = 0.4;
  const local = info.id === 'local', source = pp.meta && pp.meta.source === 'utility' ? 'Processor utility' : 'CPU time';
  const cpuText = local && activeTab !== 'disk' && activeTab !== 'network' ? source : '';
  drawTopBar(x, right, 20, span, cpuText, ...vNote(info));
  if (activeTab === 'overview') vOverview(s, { x, y: V.top, w: inner, h: h - V.top - V.margin }, span);
  else if (activeTab === 'cpu') {
    drawCpuTitle(s, x, V.top + 16, inner, info.name, info.detail);
    drawCpuGrid(s, { x, y: V.top + V.title, w: inner, h: h - V.top - V.title - V.margin }, span, pp.threads);
  } else if (activeTab === 'disk') {
    dkProcTell();                                  // the machine shown may be another now
    if (info.disks.length) dkPage(s, { x, y: V.top, w: inner }, span, lay, detailed);
    else drawStrips(s, 'disk', { x, y: V.top, w: inner, h: 120 }, span);
    if (vSilent(info)) pText(vSilent(info), x, h - 10, 12, T.muted, 400, 'left', inner);
  } else {
    const room = h - V.top - V.margin;
    const most = rows * 260 + (rows - 1) * V.netGap;   // a lone strip needn't be taller than this
    vNetPage(s, { x, y: V.top, w: inner, h: rows ? Math.min(room, most) : room }, span, cols);
  }
  pctx.globalAlpha = pAlpha = pDim = 1;
  if (!pLive(s)) veil(info.status, { x, y: V.top, w: inner, h: activeTab === 'overview' ? 2 * V.title + Math.max(150, h - V.top - MEMBAR_FULL - 100) : view - V.top });
  drawCrosshair(span);
  if (vS.fading) vInvalidate();
}

function vNote(info) {
  if (pp.meta && pp.meta.sourceNote && info.id === 'local') return [pp.meta.sourceNote, PC.warn];
  if (activeTab === 'disk' && info.io === false && info.state === 'ok') {
    return ["Its agent sends only throughput and busy time. Install this version's agent there to see the rest.", PC.warn];
  }
  if (activeTab === 'network' && pLive(pm.get(info.id))) {
    const lat = pLat(pm.get(info.id));
    if (info.lat && !info.lat.can) return [info.lat.note, PC.warn];
    if (lat && lat.e) return [`Latency: ${lat.e}`, PC.warn];
  }
  if (performance.now() - pp.started > 20000) return ['', ''];
  const keys = activeTab === 'cpu' ? `C  A${info.id === 'local' ? '  M' : ''}` : 'A';
  return [`Keys: Space  ↑ ↓  ← →  ${keys}  Esc`, T.muted];
}

const vSilent = info => (info.silent && info.silent.length ? `No activity figures for ${info.silent.join(', ')}` : '');

// CPU and network side by side, the memory bar below: what each machine in the list
// shows, with room for all of it.
function vOverview(s, a, span) {
  const info = s.info, top = a.y + V.title;
  const graphs = a.h - V.title - 2 * V.gap - MEMBAR_FULL;
  const cpuW = Math.round((a.w - V.gap) * 0.42), netX = a.x + cpuW + V.gap, netW = a.w - cpuW - V.gap;
  drawCpuTitle(s, a.x, a.y + 16, cpuW, info.name, [info.system, info.detail].filter(Boolean).join(' · '));
  drawCpuGrid(s, { x: a.x, y: top, w: cpuW, h: graphs }, span, false);
  pText('Network adapters', netX, a.y + 16, 12.5, T.ink2);
  drawStrips(s, 'net', { x: netX, y: top, w: netW, h: graphs }, span);
  const my = top + graphs + 2 * V.gap;
  vMem = drawMemBar(pctx, info.id, a.x + 6, my, a.w - 12, 'full', vMemHover, { edge: 16 });
  if (!vMem) {
    pText('System memory', a.x, my + 9, 13, T.ink, 600);
    pText(info.id !== 'local' && !info.memory && pLive(s) ? info.note : pLive(s) ? 'Waiting for the first sample…' : '', a.x, my + 34, 12.5, T.ink2, 400, 'left', a.w);
  }
}

// ---------------------------------------------------------------------------
// The Network page: each adapter's graph, and at its end two bars for the last few
// seconds (latency.py): the latency to Google, Microsoft and AT&T, stacked, with
// their average on top, and the jitter, averaged over the three.
// ---------------------------------------------------------------------------
const VL = { latW: 150, stackW: 34, ticks: 27, labelGap: 8, gap: 14 };
const V_LAT_PAL = {
  light: { jit: [0.56, 0.15, 305], google: [0.52, 0.14, 255], microsoft: [0.62, 0.14, 155], att: [0.74, 0.14, 75], gateway: [0.6, 0.02, 250] },
  dark: { jit: [0.76, 0.14, 305], google: [0.66, 0.13, 255], microsoft: [0.74, 0.14, 155], att: [0.84, 0.13, 80], gateway: [0.72, 0.02, 250] },
};
let vLatColors = {};
function vReadLatColors() {
  vLatColors = Object.fromEntries(Object.entries(V_LAT_PAL[darkQuery.matches ? 'dark' : 'light']).map(([k, c]) => [k, rgb(oklch(...c))]));
}
vReadLatColors();
darkQuery.addEventListener('change', vReadLatColors);
const vSiteColor = name => vLatColors[name] || vLatColors.gateway;

function vNetPage(s, a, span, cols) {
  const items = s.info.nets;
  if (!items.length) { drawStrips(s, 'net', a, span, cols); return; }
  const rows = Math.ceil(items.length / cols), lat = pLat(s);
  const h = (a.h - (rows - 1) * V.netGap) / rows, w = (a.w - (cols - 1) * V.netCol) / cols, barsW = VL.latW + VL.gap + DK.bar;
  items.forEach((item, i) => {
    const p = { x: a.x + (i % cols) * (w + V.netCol), y: a.y + Math.floor(i / cols) * (h + V.netGap), w, h };
    const strip = { ...p, w: Math.max(80, p.w - barsW - V.barsGap) }, ring = s.nets.get(item.key);
    const gw = lat && lat.g[item.key];
    drawNetStrip(s, item, ring, strip, span, true, gw ? `gateway ${gw}` : '');
    PS.plots.push({ ...strip, kind: 'net', s, item, ring });
    vLatBars(s, item, { x: strip.x + strip.w + V.barsGap, y: p.y, w: barsW, h: p.h }, lat);
  });
}

// What the bars of an adapter say: { avg, jitter, lost, pings, gw, why, sites: [{ name, label, host, avg, jitter, lost, n, method }] }.
// avg is the sites' averages averaged, jitter their jitters; why is the caption when there's no figure.
function vLatOf(s, item, lat) {
  const one = lat && lat.n[item.key], gw = lat ? lat.g[item.key] || '' : '';
  const out = { avg: null, jitter: null, lost: 0, pings: 0, gw, why: '', sites: [] };
  if (!s.info.lat || !s.info.lat.can) out.why = 'not measured';
  else if (!pLive(s) || item.lost) out.why = item.lost ? 'no link' : '';
  else if (!lat) out.why = 'measuring…';
  else if (lat.e && !one) out.why = "can't measure";
  else if (!gw && !one) out.why = 'no gateway';
  else if (!one) out.why = 'measuring…';
  else {
    Object.assign(out, { avg: one.avg, jitter: one.jitter, lost: one.lost, pings: one.pings });
    const known = new Map((lat.sites || []).map(([name, label, host]) => [name, { label, host }]));
    const order = [...known.keys()].filter(n => one.sites[n]).concat(Object.keys(one.sites).filter(n => !known.has(n)));
    out.sites = order.map(name => {
      const [avg, jitter, lost, n, method] = one.sites[name], k = known.get(name);
      return { name, label: k ? k.label : name === 'gateway' ? 'Gateway' : name, host: k ? k.host : name === 'gateway' ? gw : '', avg, jitter, lost, n, method };
    });
    if (out.avg == null) out.why = 'no replies';
  }
  return out;
}

// The top of a bar's scale: 1, 2 or 5 times a power of ten (at least 1 ms), so that the last minute's figures fit,
// with room above them.
function vLatTop(s, key, now, at, room = 1) {
  let most = (now || 0) * room;
  for (const x of (s.latHist && s.latHist.get(key)) || []) if (x[at] * room > most) most = x[at] * room;
  return niceCeil(Math.max(1, most));
}

function vLatBars(s, item, b, lat) {
  const v = vLatOf(s, item, lat), w = lat ? lat.w : 3;
  const ticks = top => [0, 0.25, 0.5, 0.75, 1].map(f => [f, trim2(top * f)]);
  const lostText = v.lost ? `${Math.round(100 * v.lost / v.pings)}% lost` : '';
  vLatStack({ x: b.x, y: b.y, w: VL.latW, h: b.h }, s, item, v, w);
  const jt = vLatTop(s, item.key, v.jitter, 2);
  dkBar({ x: b.x + VL.latW + VL.gap, y: b.y, w: DK.bar, h: b.h }, 'Jitter', v.jitter == null ? '--' : dkMs(v.jitter), (v.jitter || 0) / jt,
        vLatColors.jit, ticks(jt), v.why || lostText || `${w} s average`, lostText && !v.why ? PC.warn : T.muted);
  PS.plots.push({ ...b, kind: 'netbars', s, item, lat: v, secs: w });   // not w: that's its width
}

// The sites' latencies stacked, a gap between each, the average of them on top, and each named
// beside the bar with a line to its part.
function vLatStack(b, s, item, v, w) {
  const top0 = b.y + 18, bottom = b.y + b.h - 32, h = Math.max(10, bottom - top0), bx = b.x + VL.ticks, bw = VL.stackW;
  const total = v.sites.reduce((t, x) => t + (x.avg || 0), 0), top = vLatTop(s, item.key, total, 3, 1.2);
  pText('Latency', bx, b.y + 10, 11.5, T.ink, 600, 'left');
  pctx.fillStyle = PC.plot;
  pctx.fillRect(bx, top0, bw, h);
  for (const [f, label] of [0, 0.25, 0.5, 0.75, 1].map(f => [f, trim2(top * f)])) {
    const y = Math.round(bottom - h * f) + 0.5;
    pctx.beginPath();
    pctx.moveTo(bx - 4, y);
    pctx.lineTo(bx + bw, y);
    pctx.strokeStyle = T.grid;
    pctx.lineWidth = 1;
    pctx.stroke();
    pText(label, bx - 6, y + 3.5, 9.5, T.muted, 400, 'right');
  }
  // the parts, bottom up
  let y = bottom;
  const mids = [];
  v.sites.forEach((x, i) => {
    const sh = Math.min(y - top0, h * (x.avg || 0) / top);
    if (sh > 0.5) {
      const last = i === v.sites.length - 1 || v.sites.slice(i + 1).every(o => !o.avg);
      pctx.fillStyle = vSiteColor(x.name);
      pctx.beginPath();
      pctx.roundRect(bx + 4, y - sh + (last ? 0 : 1), bw - 8, Math.max(0.5, sh - (last ? 0 : 2)), last ? [Math.min(4, sh), Math.min(4, sh), 0, 0] : 0);
      pctx.fill();
    }
    mids.push(y - sh / 2);
    y -= sh;
  });
  border({ x: bx, y: top0, w: bw, h });
  // the average of them, on top
  if (v.avg != null && total > 0) {
    const text = `avg ${dkMs(v.avg)}`, tw = pWidth(text, 11, 600) + 8, ty = Math.max(top0 + 2, y - 15);
    pctx.fillStyle = PC.bg;
    pctx.globalAlpha = 0.85 * pDim;
    pctx.fillRect(bx + bw / 2 - tw / 2, ty, tw, 13);
    pctx.globalAlpha = pDim;
    pText(text, bx + bw / 2, ty + 10.5, 11, T.ink, 600, 'center');
  }
  // the names, beside, spread out so they don't overlap
  const lx = bx + bw + VL.labelGap, row = 22, shelves = vSpread(mids.map(m => m + 4), top0 + 12, bottom + 4, row);
  v.sites.forEach((x, i) => {
    const sy = shelves[i];
    pctx.strokeStyle = vSiteColor(x.name);
    pctx.lineWidth = 1.25;
    pctx.beginPath();
    pctx.moveTo(bx + bw - 3, mids[i]);
    pctx.lineTo(lx, sy - 4);
    pctx.lineTo(lx + 6, sy - 4);
    pctx.stroke();
    const room = b.x + b.w - lx - 9;
    pText(x.label, lx + 9, sy - 6, 10.5, T.ink, 600, 'left', room);
    pText(x.avg == null ? 'no replies' : `${dkMs(x.avg)}${x.method === 'tcp' ? ' TCP' : ''}`, lx + 9, sy + 5.5, 10.5, T.ink2, 400, 'left', room);
  });
  const lost = v.lost ? `${Math.round(100 * v.lost / v.pings)}% lost` : '';
  pText(v.why || `${w} s average`, bx + bw, bottom + 15, 10, v.why ? T.muted : T.ink2, 400, 'right');
  if (lost && !v.why) pText(lost, bx + bw, bottom + 28, 9.5, PC.warn, 400, 'right');
}

// Places for labels that want to be at `want` (each), at least `gap` apart, inside [lo, hi], in the same order.
function vSpread(want, lo, hi, gap) {
  const n = want.length, out = want.map(y => Math.max(lo, Math.min(hi, y)));
  for (let i = 1; i < n; i++) out[i] = Math.min(out[i], out[i - 1] - gap);   // bottom up: each above the one before
  const short = n ? lo - out[n - 1] : 0;
  if (short > 0) for (let i = 0; i < n; i++) out[i] += short;                 // past the top: all down a little
  return out;
}

function vBarsTip(p) {
  const v = p.lat, head = `<div class="head">${esc(p.item.name)} <span class="faint">${esc(p.s.info.name)}</span></div>`;
  const what = '<div class="faint">Latency: round trips from this adapter to each site, averaged over the last ' +
    `${p.secs} s; on top, the average of the sites. Jitter: how much one round trip to a site differs from the next, on average over the sites.</div>`;
  if (v.avg != null) {
    const lost = v.lost ? `${v.lost} of ${v.pings} lost` : 'none lost';
    const rows = v.sites.map(x => `<div><span class="sw" style="background:${vSiteColor(x.name)};border:0"></span>${esc(x.label)} ` +
      `<span class="faint">${esc(x.host)}</span> <b>${x.avg == null ? 'no replies' : dkMs(x.avg)}</b>` +
      `${x.jitter == null ? '' : ` · jitter ${dkMs(x.jitter)}`}${x.lost ? ` · ${x.lost} of ${x.n} lost` : ''}${x.method === 'tcp' ? ' · by TCP handshake: it doesn\'t answer pings' : ''}</div>`).join('');
    return `${head}${rows}<div class="sub">Average <b>${dkMs(v.avg)}</b> · jitter <b>${v.jitter == null ? '--' : dkMs(v.jitter)}</b> · ` +
      `${v.pings} round trips, ${lost}${v.gw ? ` · gateway ${esc(v.gw)}` : ''}</div>${what}`;
  }
  const why = { 'not measured': (p.s.info.lat && p.s.info.lat.note) || 'Not measured',
    'no gateway': "It has no default gateway (IPv4): it isn't the way to the internet, so there's nothing to time from it.",
    'no replies': `No site has answered in the last ${p.secs} s.`,
    "can't measure": esc(pLat(p.s) ? pLat(p.s).e : ''), 'measuring…': 'Measuring: the first round trips are on their way.',
    'no link': 'It has no link.' }[v.why] || '';
  return `${head}<div class="faint">${why}</div>${what}`;
}

// ---------------------------------------------------------------------------
// Hover
// ---------------------------------------------------------------------------
function vHover() {
  const ev = vMouse;
  if (!ev) return;
  const r = vS.cv.getBoundingClientRect(), mx = ev.clientX - r.left, my = ev.clientY - r.top;
  vMemHover = vMem && activeTab === 'overview' ? memBarAt(vMem, mx, my) : null;
  const p = vMemHover ? null : plotAt(vS, mx, my);
  if (vMemHover) vS.hover = null;
  dkHot = p && p.kind === 'disklabel' ? { id: p.id, key: p.key } : null;   // its line stands out
  const label = p && ['disklabel', 'diskdot', 'diskback'].includes(p.kind);
  const html = vMemHover ? memBarTip(viewMachine(), vMemHover) : !p ? '' : label ? dkLabelTip(p)
    : p.kind === 'netbars' ? vBarsTip(p) : plotTip(p, vS.hover.t);
  vS.cv.style.cursor = label ? 'pointer' : '';
  vtip.hidden = !html;
  if (html) {
    vtip.innerHTML = html;
    placeTip(vtip, vstage, ev);
  }
  vInvalidate();
}
vS.cv.addEventListener('mousemove', ev => { vMouse = ev; vHover(); });
vS.cv.addEventListener('mouseleave', () => { vLeave(); vInvalidate(); });
// A disk's labels: the dot hides its line or shows it again, the name shows it by process (or all of
// them again), and so does the way back.
vS.cv.addEventListener('click', ev => {
  const r = vS.cv.getBoundingClientRect(), p = plotAt(vS, ev.clientX - r.left, ev.clientY - r.top);
  if (!p || !['disklabel', 'diskdot', 'diskback'].includes(p.kind)) return;
  if (p.kind === 'diskdot') dkToggle(p.id, p.key);
  else if (p.kind === 'diskback') { dkProc.delete(p.id); dkProcTell(); } else dkProcToggle(p.id, p.key);
  vMouse = ev;
  vDraw();      // now, so the label's new state is what the tip describes
  vHover();
});
function vLeave() {
  vMouse = vMemHover = vS.hover = dkHot = null;
  vtip.hidden = true;
  vS.cv.style.cursor = '';
}

// Which controls the page has
function vControls() {
  const local = viewMachine() === 'local', cpu = activeTab === 'cpu';
  $('pctl-source').hidden = !(local && cpu);
  $('pctl-threads').hidden = !cpu;
  $('pctl-avg-box').hidden = !(cpu || activeTab === 'overview');
}
tabListeners.push(name => {
  vLeave();
  if (!VPAGES.includes(name)) return;
  vControls();
  vstage.scrollTop = 0;
  vInvalidate();
});
new ResizeObserver(vInvalidate).observe(vstage);
