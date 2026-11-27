'use strict';

// ---------------------------------------------------------------------------
// The Remote tab: the machines watched, each with its CPU, network and memory; smaller
// and two to a row if there are dozens, and only numbers if there are hundreds; and the
// form that adds, changes and removes them, with how to install the agent. Uses perf.js's graphs, memory.js's bar, and go() and
// nav from shell.js. Every top-level name here starts with r or R (or is osIcon),
// so none can clash with the other scripts (they all share one global scope).
// ---------------------------------------------------------------------------
const R = { gap: 18, title: 20, narrow: 700, graph: 116, strip: 36,   // full
  row: 14, mini: 34, minGap: 12, minNet: 104,                         // compact
  compactFrom: 12, numbersFrom: 100 };                               // how many machines are dozens, and hundreds
const R_LOOKS = ['auto', 'full', 'compact', 'numbers'];
const rList = $('rlist'), rScroll = $('rscroll'), rTip = $('rtip');
const rCards = new Map();      // machine id -> { el, S, visible, hits, memHover, mouse, key }
let rMachines = [];            // as the settings have them
let rQueued = false, rWish = R_LOOKS.includes(loadPref('remote.look')) ? loadPref('remote.look') : 'auto', rLook = '', rNumbersAt = 0;

// ---------------------------------------------------------------------------
// Icons
// ---------------------------------------------------------------------------
const R_WINDOWS = '<svg viewBox="0 0 64 64" aria-hidden="true"><g fill="#1f9bef"><rect x="7" y="7" width="23.5" height="23.5" rx="1.5"/>' +
  '<rect x="33.5" y="7" width="23.5" height="23.5" rx="1.5"/><rect x="7" y="33.5" width="23.5" height="23.5" rx="1.5"/><rect x="33.5" y="33.5" width="23.5" height="23.5" rx="1.5"/></g></svg>';
const R_LINUX = '<svg viewBox="0 0 64 64" aria-hidden="true">' +
  '<path d="M32 4c-8 0-12.5 6-12 14 .3 5 .2 8-2.5 12C13 36 10 41.5 10 47c0 4 2 7 5 9h34c3-2 5-5 5-9 0-5.5-3-11-7.5-17-2.7-4-2.8-7-2.5-12C44.5 10 40 4 32 4z" fill="#23252b" stroke="#6c7078" stroke-width="1.2"/>' +
  '<path d="M32 26c-6 0-9.5 6-11.5 12.5C18.5 45 20 55 32 55s13.5-10 11.5-16.5C41.5 32 38 26 32 26z" fill="#f4f4ef"/>' +
  '<ellipse cx="27.3" cy="17" rx="3.9" ry="5" fill="#f4f4ef"/><ellipse cx="36.7" cy="17" rx="3.9" ry="5" fill="#f4f4ef"/>' +
  '<circle cx="28.5" cy="18" r="1.9" fill="#15161a"/><circle cx="35.5" cy="18" r="1.9" fill="#15161a"/>' +
  '<path d="M32 20.5c-3 0-6.5 2-6.5 4 0 1.8 4 4.5 6.5 4.5s6.5-2.7 6.5-4.5c0-2-3.5-4-6.5-4z" fill="#f5a623"/>' +
  '<path d="M26 24.8c2 .9 4 1.3 6 1.3s4-.4 6-1.3" fill="none" stroke="#a86600" stroke-width="1"/>' +
  '<path d="M13 49c3-2 6 2 8.5 5.5S24 60 19 60s-11-1-12-4 3-5 6-7zM51 49c-3-2-6 2-8.5 5.5S40 60 45 60s11-1 12-4-3-5-6-7z" fill="#f5a623"/></svg>';
const R_UNKNOWN = '<svg viewBox="0 0 64 64" aria-hidden="true"><rect x="6" y="10" width="52" height="35" rx="4" fill="none" stroke="currentColor" stroke-width="3.5"/>' +
  '<path d="M24 55h16M32 45v10" stroke="currentColor" stroke-width="3.5" stroke-linecap="round"/>' +
  '<text x="32" y="36" text-anchor="middle" font-size="24" font-weight="600" fill="currentColor">?</text></svg>';
const R_CROSS = '<svg class="os-x" viewBox="0 0 64 64" aria-hidden="true"><path d="M9 9 55 55M55 9 9 55"/></svg>';

// The icon of a machine's operating system, crossed out if it can't be reached.
function osIcon(os, down) {
  return `<span class="os-icon${down ? ' down' : ''}">${os === 'windows' ? R_WINDOWS : os === 'linux' ? R_LINUX : R_UNKNOWN}${down ? R_CROSS : ''}</span>`;
}

// ---------------------------------------------------------------------------
// The list
// ---------------------------------------------------------------------------
const rSeen = new IntersectionObserver(entries => {
  for (const e of entries) {
    const c = rCards.get(e.target.dataset.id);
    if (c) c.visible = e.isIntersecting;
  }
  rInvalidate();
}, { root: rScroll, rootMargin: '120px' });

function rCard(id) {
  const el = document.createElement('li');
  el.className = 'rcard';
  el.dataset.id = id;
  el.innerHTML = '<button type="button" class="rwho"><span class="ric"></span><span class="rname"><span class="rn"></span><span class="rip"></span></span>' +
    '<span class="rsub"></span><span class="rscan" hidden></span></button>' +
    '<div class="rgraphs"><canvas></canvas></div>' +
    '<span class="rnum"><span>CPU</span><b class="rcpu">–</b></span><span class="rnum"><span>Disk</span><b class="rdisk">–</b></span>' +
    '<div class="redit"><button type="button" data-do="up" title="Move up" aria-label="Move up">▲</button>' +
    '<button type="button" data-do="down" title="Move down" aria-label="Move down">▼</button>' +
    '<button type="button" data-do="edit" title="Edit or remove" aria-label="Edit or remove">✎</button></div>';
  const c = { el, S: makeSurface(el.querySelector('canvas')), visible: false, hits: null, memHover: null, mouse: null, key: '',
    box: el.querySelector('.rgraphs'), cpu: el.querySelector('.rcpu'), disk: el.querySelector('.rdisk') };
  c.S.cv.addEventListener('mousemove', ev => { c.mouse = ev; rHover(id, c); });
  c.S.cv.addEventListener('mouseleave', () => { c.mouse = c.memHover = c.S.hover = null; rTip.hidden = true; rInvalidate(); });
  rCards.set(id, c);
  rSeen.observe(el);
  return c;
}

// Cards follow the machines the server describes.
function rSync(meta) {
  const machines = meta.machines.filter(m => m.id !== 'local'), ids = new Set(machines.map(m => m.id));
  for (const [id, c] of rCards) {
    if (ids.has(id)) continue;
    rSeen.unobserve(c.el);
    c.el.remove();
    rCards.delete(id);
  }
  machines.forEach((m, i) => {
    const c = rCards.get(m.id) || rCard(m.id);
    if (rList.children[i] !== c.el) rList.insertBefore(c.el, rList.children[i] || null);
    const down = m.state === 'down', sub = down ? 'Connection Issues' : m.state === 'connecting' ? 'Connecting…' : m.system || m.address;
    const ip = rIp(m), key = JSON.stringify([m.name, m.os, m.state, sub, m.status, m.system, m.address, ip]);
    if (key === c.key) return;
    c.key = key;
    const who = c.el.querySelector('.rwho');
    c.el.classList.toggle('down', down);
    c.el.querySelector('.ric').innerHTML = osIcon(m.os, down);
    c.el.querySelector('.rn').textContent = m.name.replace(/([._-])(?=.)/g, '$1\u200b');   // long names break after these
    c.el.querySelector('.rip').textContent = ip === m.name ? '' : ip.replace(/([.:])(?=.)/g, '$1\u200b');
    c.el.querySelector('.rsub').textContent = sub;
    who.title = [m.name, ip, m.system, m.address !== ip ? m.address : '', m.status, 'Click for details'].filter(Boolean).join('\n');
    who.setAttribute('aria-label', [m.name, ip, m.system, down ? `Connection Issues: ${m.status}` : '', 'details'].filter(Boolean).join(', '));
  });
  rSetLook(rWish !== 'auto' ? rWish : machines.length >= R.numbersFrom ? 'numbers' : machines.length >= R.compactFrom ? 'compact' : 'full');
  $('rempty').hidden = machines.length > 0;
  rList.hidden = !machines.length;
  const ok = machines.filter(m => m.state === 'ok').length, away = machines.filter(m => m.state === 'down').length;
  $('rcount').textContent = !machines.length ? '' : `${machines.length} machine${machines.length === 1 ? '' : 's'} · ${ok} connected` +
    (away ? ` · ${away} with connection issues` : '');
  $('rproblem').textContent = meta.problem || '';
  $('rproblem').hidden = !meta.problem;
  rInvalidate();
}
perfMetaListeners.push(rSync);

// The address a machine is reached at: the one its name resolved to, else what it was added with.
function rIp(m) {
  if (m.ip) return m.ip;
  const host = (m.address || '').replace(/^\[(.*)\](:\d+)?$/, '$1').replace(/^([^:]+):\d+$/, '$1');
  return host;
}

function rSetLook(look) {
  if (look === rLook) return;
  rLook = look;
  for (const x of R_LOOKS) rList.classList.toggle(x, x === look);
  rTip.hidden = true;
  tellView();   // machines shown as numbers needn't send more than that
  rInvalidate();
}
$('rctl-look').value = rWish;
$('rctl-look').addEventListener('change', ev => {
  rWish = ev.target.value;
  savePref('remote.look', rWish);
  if (pp.meta) rSync(pp.meta);
});

function rInvalidate() {
  if (rQueued || activeTab !== 'remotes') return;
  rQueued = true;
  requestAnimationFrame(() => { rQueued = false; if (activeTab === 'remotes') rDraw(); });
}
perfDrawers.push(rInvalidate);
tabListeners.push(name => { rTip.hidden = true; if (name === 'remotes') { rScroll.scrollTop = 0; rInvalidate(); } });
new ResizeObserver(rInvalidate).observe(rScroll);

function rDraw() {
  if (rLook === 'numbers') { rNumbers(); return; }
  let fading = false;
  for (const [id, c] of rCards) {
    if (!c.visible) continue;
    if (rLook === 'compact') rDrawCompact(id, c); else rDrawCard(id, c);
    fading = fading || c.S.fading;
  }
  if (fading) rInvalidate();
}

// Numbers only: CPU, and the busiest disk, twice a second.
function rNumbers() {
  const now = performance.now();
  if (now - rNumbersAt < 500) return;
  rNumbersAt = now;
  for (const [id, c] of rCards) {
    const s = pm.get(id);
    if (!c.visible || !s || !s.info) continue;
    const live = pLive(s), cpu = live && s.cpu ? s.cpu.avg(0, pp.tEnd) : null;
    let disk = null, which = '';
    if (live) {
      for (const i of s.info.disks) {
        const ring = s.disks.get(i.key), v = ring ? ring.avg(0, pp.tEnd) : null;
        if (v != null && (disk == null || v > disk)) { disk = v; which = i.name; }
      }
    }
    rNumber(c.cpu, cpu, '');
    rNumber(c.disk, disk, which && s.info.disks.length > 1 ? `The busiest disk: ${which}` : '');
  }
}
function rNumber(el, v, title) {
  const text = v == null ? '–' : `${Math.round(v)}%`;
  if (el.textContent !== text) el.textContent = text;
  el.classList.toggle('high', v != null && v >= 90);
  if (el.parentElement.title !== title) el.parentElement.title = title;
}

// Dozens of machines: a line of text over a small graph each for CPU, every adapter, and memory.
function rDrawCompact(id, c) {
  const s = pm.get(id), info = s && s.info, w = c.box.clientWidth;
  if (!info || !w) return;
  const span = pVisible(), h = R.row + R.mini, y = R.row, live = pLive(s);
  const cpuW = Math.round((w - 2 * R.minGap) * 0.24), memW = Math.round((w - 2 * R.minGap) * 0.3), netW = w - cpuW - memW - 2 * R.minGap;
  startSurface(c.S, w, h);
  if (!live) pctx.globalAlpha = pAlpha = pDim = 0.4;
  const line = (x, lw, name, value, color) => {
    const vw = value ? pText(value, x + lw, 10, 11, color || T.ink2, 600, 'right') : 0;
    pText(name, x, 10, 11, T.muted, 400, 'left', Math.max(8, lw - vw - 6));
  };
  const total = live && s.cpu ? s.cpu.avg(0, pp.tEnd) : null;
  line(0, cpuW, 'CPU', total == null ? '--' : `${Math.round(total)}%`, PC.line);
  drawCpuGrid(s, { x: 0, y, w: cpuW, h: R.mini }, span, false, false);
  const netX = cpuW + R.minGap, fit = Math.max(1, Math.floor((netW + 6) / (R.minNet + 6))), shown = info.nets.slice(0, fit);
  const each = (netW - 6 * (shown.length - 1)) / Math.max(1, shown.length), more = info.nets.length - shown.length;
  if (!shown.length) {
    line(netX, netW, 'Network', '');
    pframe({ x: netX, y, w: netW, h: R.mini }, 1, span, false);
    border({ x: netX, y, w: netW, h: R.mini });
  }
  shown.forEach((item, i) => {
    const x = netX + i * (each + 6), ring = s.nets.get(item.key), p = { x, y, w: each, h: R.mini };
    const on = stripLive(s, item, ring) && !item.lost;
    const up = on ? `↑${pBitsTiny(ring.avg(1, pp.tEnd))}` : '', down = on ? `↓${pBitsTiny(ring.avg(0, pp.tEnd))}` : item.lost ? 'disconnected' : '';
    const uw = up ? pText(up, x + each, 10, 10.5, PC.tx, 400, 'right') : 0;
    const dw = down ? pText(down, x + each - uw - (uw ? 5 : 0), 10, 10.5, on ? PC.rx : T.muted, 400, 'right') : 0;
    const room = each - uw - dw - 12;
    const name = i === shown.length - 1 && more > 0 ? `${item.name} +${more}` : item.name;
    if (room > 14) pText(name, x, 10, 10.5, T.muted, 400, 'left', room);
    drawNetStrip(s, item, ring, p, span, false);
    PS.plots.push({ ...p, kind: 'net', s, item, ring });
  });
  const memX = netX + netW + R.minGap, m = memFor(id).last;
  c.hits = null;
  if (m && m.ram && m.ram.uses) {
    const used = msum(memFor(id).segs.filter(x => IN_USE.has(x.key)).map(x => x.bytes));
    line(memX, memW, 'Memory', live ? `${fmtGB(used)} of ${capacity(m.ram.installed)} GB` : '--');
    c.hits = drawMemBar(pctx, id, memX, y, memW, 'bar', c.memHover, { barH: R.mini });
  } else {
    line(memX, memW, 'Memory', '--');
    pframe({ x: memX, y, w: memW, h: R.mini }, 1, span, false);
    if (live && !info.memory) pText('not sent by its agent', memX + 6, y + R.mini / 2 + 4, 11, T.muted, 400, 'left', memW - 10);
  }
  pctx.globalAlpha = pAlpha = pDim = 1;
  if (!live) veil(info.status, { x: 0, y, w, h: R.mini });
  drawCrosshair(span);
}

// CPU | network | memory, side by side, or the memory below the others when there's no room.
function rDrawCard(id, c) {
  const s = pm.get(id), info = s && s.info, w = c.box.clientWidth;
  if (!info || !w) return;
  const narrow = w < R.narrow, span = pVisible();
  const graph = Math.max(R.graph, info.nets.length * R.strip + (info.nets.length - 1) * PL.stripGap);
  const h = narrow ? R.title + graph + R.gap + R.title + MEMBAR_LABELS : R.title + Math.max(MEMBAR_LABELS, graph);
  const memW = narrow ? w : Math.round((w - 2 * R.gap) * 0.5);
  const cpuW = Math.round((narrow ? w - R.gap : w - 2 * R.gap - memW) * 0.44), netW = (narrow ? w - R.gap : w - 2 * R.gap - memW) - cpuW;
  const netX = cpuW + R.gap, memX = narrow ? 0 : netX + netW + R.gap, memY = narrow ? R.title + graph + R.gap : 0;
  const bodyH = narrow ? graph : h - R.title;
  startSurface(c.S, w, h);
  const live = pLive(s);
  if (!live) pctx.globalAlpha = pAlpha = pDim = 0.4;
  drawCpuTitle(s, 0, 13, cpuW, 'CPU', info.threads ? threadsText(info.threads) : '', 13);
  drawCpuGrid(s, { x: 0, y: R.title, w: cpuW, h: bodyH }, span, false);
  pText('Network', netX, 13, 13, T.ink, 600);
  drawStrips(s, 'net', { x: netX, y: R.title, w: netW, h: bodyH }, span);
  const m = memFor(id).last;
  pText('Memory', memX, memY + 13, 13, T.ink, 600);
  c.hits = null;
  if (m && m.ram && m.ram.uses) {
    const used = msum(memFor(id).segs.filter(x => IN_USE.has(x.key)).map(x => x.bytes));
    const text = live ? `${fmtSize(used)} of ${capacity(m.ram.installed)} GB in use` : '--';
    pText(text, memX + memW, memY + 13, 12.5, T.ink2, 400, 'right');
    c.hits = drawMemBar(pctx, id, memX, memY + R.title + Math.max(0, (bodyH - MEMBAR_LABELS) / 2) * (narrow ? 0 : 1), memW, 'labels', c.memHover);
  } else if (live) {
    pframe({ x: memX, y: memY + R.title, w: memW, h: narrow ? MEMBAR_LABELS : bodyH }, 1, span, false);
    pText(info.memory ? 'Waiting for the first sample…' : info.note, memX + 12, memY + R.title + 24, 12.5, T.ink2, 400, 'left', memW - 24);
    if (!info.memory) pText('The ✎ button under its name shows how.', memX + 12, memY + R.title + 44, 12.5, T.muted, 400, 'left', memW - 24);
  }
  pctx.globalAlpha = pAlpha = pDim = 1;
  if (!live) veil(info.status, { x: 0, y: R.title, w, h: h - R.title });
  drawCrosshair(span);
}
const threadsText = n => `${n} thread${n === 1 ? '' : 's'}`;

function rHover(id, c) {
  const ev = c.mouse, r = c.S.cv.getBoundingClientRect(), mx = ev.clientX - r.left, my = ev.clientY - r.top;
  c.memHover = c.hits ? memBarAt(c.hits, mx, my) : null;
  const p = c.memHover ? null : plotAt(c.S, mx, my);
  if (c.memHover) c.S.hover = null;
  const html = c.memHover ? memBarTip(id, c.memHover) : p ? plotTip(p, c.S.hover.t) : '';
  rTip.hidden = !html;
  if (html) {
    rTip.innerHTML = html;
    placeTip(rTip, rScroll, ev);
  }
  rInvalidate();
}

rList.addEventListener('click', ev => {
  const card = ev.target.closest('.rcard');
  if (!card) return;
  if (ev.target.closest('.rscan')) { scanNoteDismiss(card.dataset.id); return; }   // its port scan note, not the machine
  const id = card.dataset.id, b = ev.target.closest('[data-do]');
  if (ev.target.closest('.rwho') || rLook === 'numbers' && !b) go({ top: 'remote', machine: id, page: 'overview' });
  else if (b && b.dataset.do === 'edit') rOpen(id);
  else if (b) rPost({ action: 'move', id, by: b.dataset.do === 'up' ? -1 : 1 });
});

// Delete on a machine dismisses its port scan note, as a click on the note does.
rList.addEventListener('keydown', ev => {
  const who = ev.target.closest('.rwho');
  if (who && ev.key === 'Delete' && !who.querySelector('.rscan').hidden) {
    ev.preventDefault();
    scanNoteDismiss(who.closest('.rcard').dataset.id);
  }
});

// The keys that scroll, wherever the focus is. (Up and Down change the rate on other pages.)
document.addEventListener('keydown', ev => {
  if (activeTab !== 'remotes' || ev.ctrlKey || ev.altKey || ev.metaKey || document.querySelector('dialog[open]')) return;
  const page = rScroll.clientHeight * 0.9, to = {
    ArrowUp: rScroll.scrollTop - 60, ArrowDown: rScroll.scrollTop + 60, PageUp: rScroll.scrollTop - page,
    PageDown: rScroll.scrollTop + page, Home: 0, End: rScroll.scrollHeight }[ev.key];
  if (to === undefined) return;
  ev.preventDefault();
  rScroll.scrollTo({ top: to });
});

// ---------------------------------------------------------------------------
// Agents found on the local networks (discover.py): offered above the list, to add with a
// click, or to have it not offer again; and a line under the list about the looking.
// ---------------------------------------------------------------------------
let rFound = null, rFoundSkew = 0;
const R_KIND = { omnitop: 'OmniTOP agent', old: "OmniTOP 1.9's agent: no memory", cpugraph: "CPU Graph's agent: no memory" };

function rOnFound(v) {
  rFound = v;
  rFoundSkew = v.now - Date.now() / 1000;
  rFoundDraw();
}
perfListeners.found.push(rOnFound);
fetch('/api/found', { cache: 'no-store' }).then(r => (r.ok ? r.json() : null)).then(v => { if (v) rOnFound(v); }).catch(() => { /* they come anyway */ });

// What's found and still not watched: the server leaves out what is, but a machine added a moment ago may not be in its view yet.
function rFoundList() {
  if (!rFound) return [];
  const watched = new Set((pp.meta ? pp.meta.machines : []).flatMap(m => {
    const odd = m.address.match(/^\[.*\]:(\d+)$/) || (m.address.split(':').length === 2 && m.address.match(/:(\d+)$/));
    const port = odd ? Number(odd[1]) : 7820;
    return [`${rIp(m)}:${port}`, `${m.ip}:${port}`];
  }));
  return rFound.found.filter(f => !watched.has(`${f.address}:${f.port}`));
}

function rFoundDraw() {
  const v = rFound, list = rFoundList();
  $('rfound').hidden = !list.length;
  $('rf-all').hidden = list.length < 2;
  $('rf-list').innerHTML = list.map(f => {
    const where = f.port === 7820 ? f.address : `${f.address}:${f.port}`;
    const what = `${R_KIND[f.kind] || 'agent'}${f.agent ? ` ${f.agent}` : ''}`;
    return `<li data-address="${esc(f.address)}" data-port="${f.port}" data-os="${esc(f.os)}">${osIcon(f.os, false)}` +
      `<div class="rf-who"><b>${esc(f.host || f.address)}</b><span>${esc(where)} · <span class="${f.kind === 'omnitop' ? '' : 'old'}" style="display:inline">${esc(what)}</span></span></div>` +
      `<button type="button" data-found="add">Add</button>` +
      `<button type="button" class="rf-x" data-found="ignore" title="Don't offer it again" aria-label="Don't offer ${esc(f.host || f.address)} again">✕</button></li>`;
  }).join('');
  const s = $('rf-status');
  s.hidden = !v;
  if (!v) return;
  const nets = v.networks.length ? v.networks.join(', ') : 'this PC\'s networks';
  const ago = v.last ? Math.max(0, Date.now() / 1000 + rFoundSkew - v.last) : null;
  const when = ago == null ? '' : ago < 90 ? ' · last looked just now' : ` · last looked ${Math.round(ago / 60)} min ago`;
  s.innerHTML = !v.on ? 'Not looking for agents on your network. <button type="button" data-found="on">Look for them</button>'
    : (v.busy ? `Looking for agents on ${esc(nets)}…` : `Looks for agents on ${esc(nets)} every ${Math.round(v.every / 60)} minutes${when}.`) +
      (v.error ? ` <span class="bad">${esc(v.error)}</span>` : '') +
      (v.busy ? '' : ' <button type="button" data-found="look">Look now</button>') + ' <button type="button" data-found="off">Stop looking</button>';
  rCountText();
}
perfMetaListeners.push(() => { if (rFound) rFoundDraw(); });
setInterval(() => { if (rFound && activeTab === 'remotes') rFoundDraw(); }, 30000);   // "last looked ... ago"

function rCountText() {
  const n = rFoundList().length, el = $('rcount');
  el.dataset.found = n ? ` · ${n} more found on your network` : '';
}

async function rFoundPost(body) {
  try {
    const res = await fetch('/api/found', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (res.ok) rOnFound(await res.json());
  } catch { /* the next event says how it is */ }
}

async function rFoundAdd(li) {
  li.querySelectorAll('button').forEach(b => { b.disabled = true; });
  const a = await rPost({ action: 'add', name: '', address: li.dataset.address, port: Number(li.dataset.port), os: li.dataset.os });
  if (a.error) {
    li.querySelector('.rf-who span').textContent = a.error;
    li.querySelectorAll('button').forEach(b => { b.disabled = false; });
  }
}

document.addEventListener('click', async ev => {
  const b = ev.target.closest('[data-found]');
  if (!b || !b.closest('#tab-remotes')) return;
  const li = b.closest('li'), what = b.dataset.found;
  if (what === 'add') await rFoundAdd(li);
  else if (what === 'ignore') await rFoundPost({ action: 'ignore', address: li.dataset.address, port: Number(li.dataset.port) });
  else if (what === 'look') await rFoundPost({ action: 'look' });
  else if (what === 'on' || what === 'off') await rFoundPost({ action: 'on', on: what === 'on' });
});
$('rf-all').addEventListener('click', async () => {
  for (const li of [...$('rf-list').children]) await rFoundAdd(li);
});

// ---------------------------------------------------------------------------
// The form
// ---------------------------------------------------------------------------
const rDialog = $('rdialog'), rForm = $('rform');
let rEditing = null, rTestNo = 0, rAsking = false, rOldAgent = '';   // rOldAgent: "linux" or "windows" if it runs an agent that's to be replaced

async function rPost(body) {
  try {
    const res = await fetch('/api/machines', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch {
    return { error: "OmniTOP isn't responding. Try again in a moment.", field: '' };
  }
}

async function rOpen(id) {
  rEditing = null;
  if (id) {
    try {
      rMachines = (await (await fetch('/api/machines', { cache: 'no-store' })).json()).machines;
    } catch { return; }
    rEditing = rMachines.find(m => m.id === id);
    if (!rEditing) return;
  }
  const m = rEditing || { name: '', address: '', port: 7820, os: '' };
  const info = id && pm.get(id) && pm.get(id).info;
  $('rtitle').textContent = rEditing ? `Edit ${info ? info.name : m.name || m.address}` : 'Add a remote machine';
  rForm.elements.name.value = m.name;
  rForm.elements.address.value = m.address;
  rForm.elements.port.value = m.port;
  rForm.elements.os.value = (info && info.state === 'ok' && info.os) || m.os || 'windows';
  $('rsave').textContent = rEditing ? 'Save' : 'Add machine';
  $('rremove').hidden = !rEditing;
  rOldAgent = info && info.state === 'ok' && !info.memory ? info.os : '';
  rAskRemove(false);
  rErrors({});
  rResult('', '');
  rHelp();
  rDialog.showModal();
  rForm.elements[rEditing ? 'name' : 'address'].focus();
}

const rFields = () => ({ name: rForm.elements.name.value, address: rForm.elements.address.value,
                         port: rForm.elements.port.value, os: rForm.elements.os.value });

function rErrors({ error, field }) {
  for (const f of ['name', 'address', 'port']) {
    const bad = error && field === f;
    $(`rerr-${f}`).textContent = bad ? error : '';
    rForm.elements[f].setAttribute('aria-invalid', String(!!bad));
    if (bad) rForm.elements[f].focus();
  }
  $('rerr-').textContent = error && !['name', 'address', 'port'].includes(field) ? error : '';
}

function rResult(kind, text) {
  $('rresult').className = 'rresult ' + kind;
  $('rresult').textContent = text;
}

async function rTest() {
  const no = ++rTestNo;
  rErrors({});
  rResult('busy', 'Connecting…');
  $('rtest').disabled = true;
  const a = await rPost({ action: 'test', ...rFields() });
  if (no !== rTestNo) return;
  $('rtest').disabled = false;
  if (a.field !== undefined) { rErrors(a); rResult('', ''); return; }
  if (!a.ok) { rResult('bad', `${a.error}. Check that the agent is installed there, and that its firewall lets TCP port ${rForm.elements.port.value || 7820} in.`); return; }
  rOldAgent = a.memory ? '' : a.os;
  if (a.os) rForm.elements.os.value = a.os;
  rHelp();
  rResult('good', `Connected to ${a.host || 'it'}: ${[a.system || (a.os === 'windows' ? 'Windows' : 'Linux'), a.cpus && threadsText(Number(a.cpus)), `agent ${a.agent}`].filter(Boolean).join(', ')}.` +
    (a.memory ? '' : a.os === 'windows' ? ' That agent is too old to send memory: the steps here replace it.'
      : " It's CPU Graph's agent, which sends no memory: the steps here replace it."));
}

async function rSave(ev) {
  ev.preventDefault();
  if (rAsking) return;
  rErrors({});
  const a = await rPost(rEditing ? { action: 'update', id: rEditing.id, ...rFields() } : { action: 'add', ...rFields() });
  if (a.error) { rErrors(a); return; }
  rDialog.close();
}

function rAskRemove(on) {   // Remove asks first, in place
  rAsking = on;
  $('rremove').textContent = on ? 'Remove it' : 'Remove';
  $('rremove').classList.toggle('danger', on);
  $('rcancel').textContent = on ? 'Keep it' : 'Cancel';
  $('rsave').hidden = $('rtest').hidden = on;
  $('rask').hidden = !on;
}

async function rRemove() {
  if (!rAsking) { rAskRemove(true); return; }
  const a = await rPost({ action: 'remove', id: rEditing.id });
  if (a.error) { rAskRemove(false); rErrors(a); return; }
  rDialog.close();
}

rForm.addEventListener('submit', rSave);
$('rtest').addEventListener('click', rTest);
$('rremove').addEventListener('click', rRemove);
$('rcancel').addEventListener('click', () => { if (rAsking) rAskRemove(false); else rDialog.close(); });
rDialog.addEventListener('close', () => { rTestNo++; $('rtest').disabled = false; });
rForm.addEventListener('input', ev => { if (ev.target.name === 'address' || ev.target.name === 'port') rOldAgent = ''; rResult('', ''); rTestNo++; $('rtest').disabled = false; rHelp(); });
for (const b of document.querySelectorAll('[data-add-machine]')) b.addEventListener('click', () => rOpen(null));

// ---------------------------------------------------------------------------
// How to install the agent
// ---------------------------------------------------------------------------
function rHelp() {
  const os = rForm.elements.os.value, port = Number(rForm.elements.port.value) || 7820;
  const host = rForm.elements.address.value.trim() || 'the-machine', odd = port !== 7820;
  const shell = /^[A-Za-z0-9._:%-]+$/.test(host) ? host : 'the-machine';   // only what's safe to paste
  const target = shell.includes(':') ? `[${shell}]` : shell;
  const code = text => `<span class="code"><code>${esc(text)}</code><button type="button" data-copy="${esc(text)}">Copy</button></span>`;
  const step = (...parts) => `<li>${parts.join('')}</li>`;
  let html;
  if (os === 'linux') {
    html = '<h3>Installing the agent on Linux</h3><ol>' +
      step('Get the agent: one Python file, for Python 3.6 or later.<br><button type="button" data-get="linux">Save omnitop-agent.py</button> <span class="rsaved" id="rsaved"></span>') +
      step("Copy it to the machine, with an account you can sign in to there:", code(`scp omnitop-agent.py you@${target}:`)) +
      step('Sign in, and install it:', code(`ssh -t you@${shell} sudo python3 omnitop-agent.py --install${odd ? ` --port ${port}` : ''}${rOldAgent === 'linux' ? ' --replace' : ''}`),
        `It starts with the system (systemd), as an account with no rights of its own, and opens TCP port ${port} if firewalld or ufw is running.`,
        rOldAgent === 'linux' ? '<br><code>--replace</code> stops the cpugraph-agent it runs now, which has that port.'
          : '<br>If it already runs cpugraph-agent, add <code>--replace</code>.') +
      step('Come back here and choose <b>Test connection</b>.') +
      '</ol><p class="rnote">No sudo there, or no Python (a minimal Debian, say)? Sign in as root (<code>su -</code>), ' +
      '<code>apt install python3</code> if it\'s missing, and run the same command without <code>sudo</code>.</p>' +
      '<p class="rnote">To remove it: <code>sudo python3 omnitop-agent.py --uninstall</code></p>';
  } else {
    html = '<h3>Installing the agent on Windows</h3>' +
      (rOldAgent === 'windows' ? '<p class="rnote">This PC runs an older agent, which sends no memory. These steps replace it.</p>' : '') + '<ol>' +
      step('The agent is part of OmniTOP. Copy <b>omniTop.exe</b> to the PC, on a share or a USB stick, say.' +
        (pp.desktop ? '<br><button type="button" data-get="exe">Show omniTop.exe</button> <span class="rsaved" id="rsaved"></span>'
          : '<br><a href="https://github.com/esteed/OmniTOP/releases/latest" target="_blank" rel="noopener">Download it</a>')) +
      step('On that PC, open a terminal (Windows key, then type cmd) in the folder it\'s in, and run:', code(`omniTop.exe --install-agent${odd ? ` --agent-port ${port}` : ''}`),
        `Windows asks for administrator rights. The agent is copied to Program Files, starts with Windows with no one signed in, and TCP port ${port} is opened on private and domain networks.`) +
      step('Come back here and choose <b>Test connection</b>.') +
      '</ol><p class="rnote">To remove it: <code>omniTop.exe --uninstall-agent</code></p>';
  }
  html += '<p class="rnote">The agent only reads counters and runs no commands. Its connection isn\'t encrypted or protected by a password: use it on networks you trust.</p>';
  if ($('rhelp').dataset.html !== html) {
    $('rhelp').dataset.html = html;
    $('rhelp').innerHTML = html;
  }
}

$('rhelp').addEventListener('click', async ev => {
  const copy = ev.target.closest('[data-copy]'), get = ev.target.closest('[data-get]');
  if (copy) {
    await rCopy(copy.dataset.copy);
    copy.textContent = 'Copied';
    setTimeout(() => { copy.textContent = 'Copy'; }, 1500);
  } else if (get) {
    if (!pp.desktop) {   // a browser: an ordinary download
      const a = document.createElement('a');
      a.href = '/omnitop-agent.py';
      a.download = 'omnitop-agent.py';
      a.click();
      return;
    }
    try {
      const api = window.pywebview.api, done = await (get.dataset.get === 'exe' ? api.show_program() : api.save_linux_agent());
      $('rsaved').textContent = done.error || (get.dataset.get === 'exe' ? '' : `Saved in ${done.path}`);
    } catch { $('rsaved').textContent = "That didn't work."; }
  }
});

async function rCopy(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {   // no clipboard access: the old way, inside the dialog so it can take the focus
    const ta = document.createElement('textarea');
    ta.value = text;
    rDialog.append(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
}
