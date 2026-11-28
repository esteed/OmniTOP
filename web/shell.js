'use strict';

// ---------------------------------------------------------------------------
// The app's frame: the Local and Remote tabs, the tabs below them, Escape, and
// the stream of data. Loaded last.
//
// nav.top is the tab: local or remote. nav.machine is the machine looked at: local,
// a remote one's id, or null for the list of them. activeTab is the page showing:
// overview, cpu, disk, network, mem, net (ports), or remotes (the list).
// ---------------------------------------------------------------------------
const PAGES = { overview: 'Overview', cpu: 'CPU', disk: 'Disk', network: 'Network', mem: 'Memory', net: 'Ports' };
const REMOTE_PAGES = ['overview', 'cpu', 'disk', 'network', 'mem', 'net'];
const HOME = { top: 'local', machine: 'local', page: 'overview' };
const topButtons = [...document.querySelectorAll('.toptab')], subButtons = [...document.querySelectorAll('.subtab')];
let nav = {};

function viewMachine() { return nav.machine || 'local'; }

function go(want) {
  const next = { ...HOME, ...want };
  if (next.top === 'local') next.machine = 'local';
  else if (!want.machine) { next.machine = null; next.page = 'remotes'; }
  else if (!REMOTE_PAGES.includes(next.page)) next.page = 'overview';
  const samePage = next.page === nav.page, sameMachine = next.machine === nav.machine;
  nav = next;
  activeTab = nav.page;
  for (const b of topButtons) {
    const on = b.dataset.top === nav.top;
    b.classList.toggle('on', on);
    b.setAttribute('aria-selected', String(on));
    b.tabIndex = on ? 0 : -1;
  }
  const detail = nav.top === 'remote' && nav.machine;
  $('subtabs').hidden = nav.page === 'remotes';
  $('machine-head').hidden = !detail;
  for (const b of subButtons) {
    const on = b.dataset.page === nav.page;
    b.classList.toggle('on', on);
    b.setAttribute('aria-selected', String(on));
    b.tabIndex = on ? 0 : -1;
    b.hidden = nav.top === 'remote' && !REMOTE_PAGES.includes(b.dataset.page);
  }
  $('tab-remotes').hidden = nav.page !== 'remotes';
  $('tab-perf').hidden = !VPAGES.includes(nav.page);
  $('tab-mem').hidden = nav.page !== 'mem';
  $('tab-net').hidden = nav.page !== 'net';
  state.hover = null;
  tip.hidden = true;
  if (nav.page === 'net') portsShow(viewMachine());
  shellHead();
  if (!sameMachine || !samePage) tellView();
  if (!samePage || !sameMachine) for (const f of tabListeners) f(activeTab);
}

// Tell the server what's looked at, so each machine sends only as much as that needs.
function tellView() {
  fetch('/api/view', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ machine: nav.top === 'remote' ? nav.machine : null, numbers: rLook === 'numbers', page: nav.page }),
  }).catch(() => { /* it's sent less often then */ });
}

// Back to what a fresh start shows: this PC's overview.
function goHome() {
  setPaused(false);
  if (state.paused) $('ctl-pause').click();
  if (mem.pin != null) clearPin();
  for (const p of [...bubOpenNow.keys()]) bubClose(p);
  resetView();
  if (document.activeElement && document.activeElement !== document.body) document.activeElement.blur();
  go(HOME);
  vstage.scrollTop = 0;
}

// The machine named beside the tabs while one is looked at closely
function shellHead() {
  if (nav.top !== 'remote' || !nav.machine) return;
  const s = pm.get(nav.machine), info = s && s.info;
  if (!info) return;
  $('machine-icon').innerHTML = osIcon(info.os, info.state === 'down');
  $('machine-name').textContent = info.name;
  $('machine-sub').textContent = info.state === 'down' ? 'Connection Issues' : info.system || info.address;
  $('machine-sub').classList.toggle('issue', info.state === 'down');
  $('machine-head').title = [info.name, info.system, info.address, info.status].filter(Boolean).join('\n');
}
perfMetaListeners.push(meta => {
  if (nav.machine && nav.machine !== 'local' && !meta.machines.some(m => m.id === nav.machine)) go({ top: 'remote' });   // removed
  for (const id of [...mems.keys()]) if (!meta.machines.some(m => m.id === id)) dropMem(id);
  shellHead();
});

document.querySelector('.toptabs').addEventListener('click', ev => {
  const b = ev.target.closest('.toptab');
  if (b) go(b.dataset.top === 'local' ? HOME : { top: 'remote' });
});
$('subtabs').addEventListener('click', ev => {
  if (ev.target.closest('#machine-back')) { go({ top: 'remote' }); return; }
  const b = ev.target.closest('.subtab');
  if (b) go({ ...nav, page: b.dataset.page });
});
function arrowKeys(list, pick) {
  return ev => {
    if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight' || !ev.target.closest('[role=tab]')) return;
    const shown = list.filter(b => !b.hidden), i = shown.findIndex(b => b.classList.contains('on'));
    const next = shown[(i + (ev.key === 'ArrowRight' ? 1 : shown.length - 1)) % shown.length];
    ev.preventDefault();
    ev.stopPropagation();
    pick(next);
    next.focus();
  };
}
document.querySelector('.toptabs').addEventListener('keydown', arrowKeys(topButtons, b => b.click()));
$('subtabs').addEventListener('keydown', arrowKeys(subButtons, b => b.click()));
$('logo').addEventListener('click', goHome);

document.addEventListener('keydown', ev => {
  if (ev.key !== 'Escape' || ev.defaultPrevented || document.querySelector('dialog[open]')) return;
  if (nav.top === 'local' || nav.machine) {
    ev.preventDefault();
    goHome();
  }
});

// ---------------------------------------------------------------------------
// The server
// ---------------------------------------------------------------------------
async function loadInfo() {
  try {
    const info = await (await fetch('/api/info', { cache: 'no-store' })).json();
    state.mode = info.mode;
    state.services = info.services || {};
    if (info.dynamic) {   // the grid's ephemeral band follows this PC's range
      portsLocal.dyn = { start: info.dynamic.start, end: info.dynamic.end };
      if (portsOf === 'local') { state.dyn = portsLocal.dyn; layersDirty = true; }
    }
    if (info.tick > 0) setTick(info.tick);
    document.title = `OmniTOP · ${info.host}`;
    if (info.version) {   // top right, beside the Live / Demo data light
      $('version').textContent = info.version;
      $('version').title = `OmniTOP version ${info.version}`;
      $('version').hidden = false;
    }
    // Put back the sample rate chosen last time.
    const want = loadPref('rate');
    if (!rateRestored && RATES.includes(want) && Math.round(1 / info.tick) !== want) {
      $('ctl-rate').value = String(want);
      postRate(want).catch(() => { $('ctl-rate').value = String(Math.round(1 / tick)); });
    }
    rateRestored = true;
  } catch {
    // The stream's reconnect will retry; names just stay blank meanwhile.
  }
  renderStatus();
}

function connect() {
  const es = new EventSource('/events');
  es.onopen = () => {
    state.connected = true;
    if (!state.mode) loadInfo();
    tellView();   // a server that has restarted doesn't know
    portsNetSent.clear();
    portsNetLook(true);
    renderStatus();
  };
  es.onmessage = ev => { if (portsOf === 'local') onTick(JSON.parse(ev.data)); };   // this PC's ports
  es.addEventListener('rports', ev => {   // a remote machine's, while its Ports page shows
    const m = JSON.parse(ev.data);
    if (m.id === portsOf) onTick(m);
  });
  es.addEventListener('probe', ev => onProbe(JSON.parse(ev.data)));
  // scan: watch.js; sys and rmem: memory.js; perf, perfmeta and lat: perf.js
  for (const [kind, listeners] of [['scan', scanListeners], ['sys', sysListeners], ['rmem', rmemListeners],
                                   ['perf', perfListeners.perf], ['perfmeta', perfListeners.perfmeta],
                                   ['lat', perfListeners.lat], ['found', perfListeners.found], ['procio', perfListeners.procio]]) {
    es.addEventListener(kind, ev => {
      const m = JSON.parse(ev.data);
      for (const f of listeners) f(m);
    });
  }
  es.onerror = () => { state.connected = false; renderStatus(); };
}

go(HOME);
loadInfo();
connect();
