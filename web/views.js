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
const V = { margin: 16, top: 30, gap: 16, title: 26, strip: 96, stripMin: 64, foot: 24 };
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
    h = Math.max(view, V.top + rows * V.strip + (rows - 1) * PL.stripGap + V.margin);
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
    if (info.disks.length) dkPage(s, { x, y: V.top, w: inner }, span, lay, detailed);
    else drawStrips(s, 'disk', { x, y: V.top, w: inner, h: 120 }, span);
    if (vSilent(info)) pText(vSilent(info), x, h - 10, 12, T.muted, 400, 'left', inner);
  } else {
    const room = h - V.top - V.margin;
    const most = rows * 260 + (rows - 1) * PL.stripGap;   // a lone strip needn't be taller than this
    drawStrips(s, 'net', { x, y: V.top, w: inner, h: rows ? Math.min(room, most) : room }, span, cols);
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
  const html = vMemHover ? memBarTip(viewMachine(), vMemHover) : p && p.kind !== 'disklabel' ? plotTip(p, vS.hover.t) : '';
  vtip.hidden = !html;
  if (html) {
    vtip.innerHTML = html;
    placeTip(vtip, vstage, ev);
  }
  vInvalidate();
}
vS.cv.addEventListener('mousemove', ev => { vMouse = ev; vHover(); });
vS.cv.addEventListener('mouseleave', () => { vLeave(); vInvalidate(); });
function vLeave() {
  vMouse = vMemHover = vS.hover = dkHot = null;
  vtip.hidden = true;
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
