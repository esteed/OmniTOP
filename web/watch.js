'use strict';
// Ports page extras: the details bubbles for pinned ports, and the
// port-scan alerts, of this PC and remote machines. Uses app.js: $, esc, ports,
// state, svcName, fmtRate, pinned, windowSpan, loadPref, savePref, stage,
// activeTab, fitAround, portsOf, listListeners, scanListeners, tabListeners,
// portsListeners; and later scripts' pm, rCards, nav and viewMachine.
// Every top-level name here starts with bub or scan, so none can clash with
// the other scripts (they all share one global scope).

// ---------------------------------------------------------------------------
// Details bubbles: rest the pointer on a pinned port for 2 s
// ---------------------------------------------------------------------------
const BUB_DELAY = 2000;      // ms on a pinned port before its bubble opens
const BUB_MAX = 4;           // bubbles open at once
const BUB_REFRESH = 3000;    // ms between refreshes of an open bubble's processes
const bubOpenNow = new Map();   // port -> { el, data, timer }
const bubTray = $('bubbles'), bubNote = $('bubble-note'), bubList = $('top');
let bubHoverPort = null, bubArmedFor = null, bubTimer = 0, bubNoteTimer = 0;

// The list is rebuilt every second, so the port under the pointer is tracked
// by number, not by element.
bubList.addEventListener('mouseover', ev => {
  const li = ev.target.closest('li[data-port]');
  bubHover(li ? Number(li.dataset.port) : null);
});
bubList.addEventListener('mouseleave', () => bubHover(null));
listListeners.push(() => { bubArm(); bubLive(); });

function bubHover(p) {
  if (p === bubHoverPort) return;
  bubHoverPort = p;
  bubArm();
}

// Start (or stop) the 2-second wait for the port under the pointer. Called
// on every move between rows and after each list redraw, which catches a
// port being pinned or unpinned while the pointer stays on it.
function bubArm() {
  const p = bubHoverPort;
  if (p == null || !pinned(p) || bubOpenNow.has(p)) {
    clearTimeout(bubTimer);
    bubTimer = 0;
    bubArmedFor = null;
    return;
  }
  if (bubArmedFor === p) return;   // already waiting for this one
  clearTimeout(bubTimer);
  bubArmedFor = p;
  bubTimer = setTimeout(() => {
    bubTimer = 0;
    bubArmedFor = null;
    if (bubHoverPort === p && pinned(p) && !bubOpenNow.has(p)) bubShow(p);
  }, BUB_DELAY);
}

function bubShow(p) {
  if (bubOpenNow.size >= BUB_MAX) {
    bubSay(p, `Up to ${BUB_MAX} detail bubbles can be open at once. Close one with × to open another.`);
    return;
  }
  const el = document.createElement('div');
  el.className = 'bubble';
  el.dataset.port = p;
  el.setAttribute('role', 'dialog');
  el.setAttribute('aria-label', `Port ${p} details`);
  el.innerHTML = `<button type="button" class="bub-x" aria-label="Close" title="Close">✕</button>` +
    `<div class="bub-body"><div class="bub-head"></div><div class="bub-live"></div>` +
    `<div class="bub-procs"><p class="faint">Looking up the processes…</p></div></div>` +
    `<span class="bub-tail" aria-hidden="true"><i></i><i></i><i></i></span>`;
  el.querySelector('.bub-x').addEventListener('click', () => bubClose(p));
  el.addEventListener('mouseenter', () => { state.focus = p; });
  el.addEventListener('mouseleave', () => { if (state.focus === p) state.focus = null; });
  bubTray.append(el);   // the tray runs right to left: the first bubble sits next to the list
  const b = { el, data: null, timer: 0 };
  bubOpenNow.set(p, b);
  bubHead(p, b);
  bubLive();
  bubFetch(p);
}

function bubClose(p) {
  const b = bubOpenNow.get(p);
  if (!b) return;
  clearTimeout(b.timer);
  b.el.remove();
  bubOpenNow.delete(p);
  if (state.focus === p) state.focus = null;
  bubArm();   // the pointer may still be on that port
}

// A short message beside the list row the pointer is on.
function bubSay(p, msg) {
  const main = bubTray.parentElement.getBoundingClientRect();
  const row = bubList.querySelector(`li[data-port="${p}"]`);
  const r = row ? row.getBoundingClientRect() : main;
  bubNote.textContent = msg;
  bubNote.hidden = false;
  bubNote.style.top = Math.max(8, r.top - main.top) + 'px';
  bubNote.style.right = Math.max(8, main.right - r.left + 10) + 'px';
  clearTimeout(bubNoteTimer);
  bubNoteTimer = setTimeout(() => { bubNote.hidden = true; }, 4000);
}

async function bubFetch(p) {
  const b = bubOpenNow.get(p);
  if (!b) return;
  try {
    const of = portsOf === 'local' ? '' : `&machine=${encodeURIComponent(portsOf)}`;
    const res = await fetch(`/api/port?p=${p}${of}`, { cache: 'no-store' });
    if (!res.ok) {
      const why = res.status === 503 ? (await res.json().catch(() => ({}))).error : '';
      throw new Error(why || `HTTP ${res.status}`);
    }
    const data = await res.json();
    if (bubOpenNow.get(p) !== b) return;   // closed meanwhile
    if (data.error) throw new Error(data.error);
    b.data = data;
    bubProcs(p, b);
  } catch (err) {
    if (!b.data && bubOpenNow.get(p) === b) {
      b.el.querySelector('.bub-procs').innerHTML = `<p class="faint">Couldn’t look up this port’s processes` +
        `${portsOf !== 'local' && err.message && !/^HTTP/.test(err.message) ? `: ${esc(err.message)}` : ''}.</p>`;
    }
  }
  if (bubOpenNow.get(p) === b) b.timer = setTimeout(() => bubFetch(p), BUB_REFRESH);
}

function bubHead(p, b) {
  const name = svcName(p);
  b.el.querySelector('.bub-head').innerHTML =
    `<span class="port">${p}</span>${name ? ` <span class="svc">${esc(name)}</span>` : ''}`;
}

// The live numbers, from the same samples as the list (redrawn with it).
function bubLive() {
  for (const [p, b] of bubOpenNow) {
    const e = ports.get(p);
    let html;
    if (!e || (e.rate <= 0 && e.own <= 0)) {
      html = '<span class="faint">No traffic right now</span>';
    } else {
      const span = windowSpan(), r = x => fmtRate(x * 8 / span);
      let tcp = 0, udp = 0, loc = 0, rem = 0;
      for (const s of e.hist) {
        for (const v of s.o && !state.hideOwn ? [s.v, s.o] : [s.v]) {
          tcp += v[0] + v[1]; udp += v[2] + v[3]; loc += v[0] + v[2]; rem += v[1] + v[3];
        }
      }
      html = `<b>${fmtRate(e.rate)}</b>` +
        `<span class="faint">TCP ${r(tcp)} · UDP ${r(udp)} · as local port ${r(loc)} · as remote ${r(rem)}</span>`;
    }
    b.el.querySelector('.bub-live').innerHTML = html;
  }
}

function bubBytes(n) {
  if (n == null) return '';
  for (const [u, d] of [['GB', 1e9], ['MB', 1e6], ['KB', 1e3]]) {
    if (n >= d) return `${(n / d).toFixed(n >= 10 * d ? 0 : 1)} ${u}`;
  }
  return `${n} B`;
}

function bubWhen(sec) {
  if (!sec) return '';
  const d = new Date(sec * 1000), today = new Date().toDateString() === d.toDateString();
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  return today ? time : `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`;
}

function bubAgo(s) {
  if (s == null) return '';
  return s < 1.5 ? 'just now' : s < 90 ? `${Math.round(s)} s ago` : `${Math.round(s / 60)} min ago`;
}

function bubProcs(p, b) {
  const d = b.data, out = [];
  if (d.listening.length) {
    const by = {};
    for (const l of d.listening) (by[`${l.proto} · PID ${l.pid}`] ||= []).push(l.addr === '::' ? '[::]' : l.addr);
    out.push(`<p class="bub-listen">Listening: ${Object.entries(by).map(([k, a]) => `${esc(a.join(', '))} (${esc(k)})`).join('; ')}</p>`);
  }
  if (!d.procs.length) out.push('<p class="faint">No process has this port open right now.</p>');
  for (const pr of d.procs) {
    const title = pr.desc && pr.desc.toLowerCase() !== (pr.name || '').toLowerCase() ? pr.desc : '';
    const meta = [`PID ${pr.pid}`, pr.started ? `started ${bubWhen(pr.started)}` : '', pr.mem != null ? `${bubBytes(pr.mem)} private memory` : '',
      pr.threads ? `${pr.threads} threads` : ''].filter(Boolean).join(' · ');
    const who = [pr.user ? `User ${esc(pr.user)}` : '', pr.parent ? `started by ${esc(pr.parent.name)} (${pr.parent.pid})` : '',
      pr.company ? esc(pr.company) : ''].filter(Boolean).join(' · ');
    const conns = pr.conns.map(c => `<li><span class="mono">${esc(c.proto)} ${esc(c.local)}${c.remote ? ` → ${esc(c.remote)}` : ''}</span>` +
      `${c.state ? ` <span class="faint">${esc(c.state)}</span>` : ''}</li>`).join('');
    out.push(`<div class="bub-proc">` +
      `<div class="bp-name">${esc(pr.name || `PID ${pr.pid}`)}${title ? ` <span class="faint">${esc(title)}</span>` : ''}</div>` +
      `<div class="bp-meta">${meta}</div>` +
      (pr.path ? `<div class="bp-path mono" title="${esc(pr.path)}">${esc(pr.path)}</div>` : '') +
      (who ? `<div class="bp-meta">${who}</div>` : '') +
      (pr.services.length ? `<div class="bp-meta">Services: ${esc(pr.services.join(', '))}</div>` : '') +
      `<div class="bp-seen">${pr.seen != null ? `Sent or received on this port ${bubAgo(pr.seen)}` : 'Has this port open'}` +
      `${pr.nconns ? ` · ${pr.nconns} connection${pr.nconns === 1 ? '' : 's'} on it` : ''}</div>` +
      (conns ? `<ul class="bp-conns">${conns}</ul>` : '') +
      (pr.nconns > pr.conns.length ? `<div class="faint">+ ${pr.nconns - pr.conns.length} more</div>` : '') +
      `</div>`);
  }
  if (d.more) out.push(`<p class="faint">+ ${d.more} more process${d.more === 1 ? '' : 'es'} using this port</p>`);
  b.el.querySelector('.bub-procs').innerHTML = out.join('');
}

// ---------------------------------------------------------------------------
// Port scan alerts
// ---------------------------------------------------------------------------
// Each scan has its own alert: one that stops and starts again is a new scan.
// Each machine's Ports page shows that machine's alerts. While a scan of the
// machine shown runs, the Port scan button and the Ports tab throb red. A new
// scan zooms the view out so both grids fit beside its alert, and, if its
// machine's Ports page isn't showing, makes the tabs that lead there throb until
// it's opened: the Ports tab, the Local tab for this PC, the Remote tab for a
// remote one. A remote machine's card says it was scanned, and when, until that
// note is clicked (it's kept across restarts).
const scanBox = $('alerts'), scanLight = $('ctl-scan'), scanTab = $('tabbtn-net'), scanTop = $('topbtn-local'),
  scanRemoteTop = $('topbtn-remote');
const scanKnown = new Map();     // machine|scan id -> latest description from the server, with its machine as .m
// key -> how many packets it had when dismissed; it comes back if more arrive.
const scanDismissed = new Map(Object.entries(loadPref('dismissedScans') || {}));
let scanSkew = 0;                // server clock minus this one, seconds
const scanUnseen = new Set();    // machines a scan began on while their Ports page wasn't showing
let scanFitDue = false;          // fit the view around the alerts when the page is next shown
// A remote machine's card's note: machine id -> { key, at (this clock's seconds), from, ports } of its
// latest scan, until the note is clicked; and the scans whose notes were clicked away.
const scanNotes = new Map(Object.entries(loadPref('scanNotes') || {}).filter(([, n]) => n && n.key));
const scanNoteGone = new Set(Array.isArray(loadPref('scanNotesGone')) ? loadPref('scanNotesGone') : []);
function scanNotesSave() {
  savePref('scanNotes', Object.fromEntries(scanNotes));
  savePref('scanNotesGone', [...scanNoteGone].slice(-100));
}

const scanKey = s => `${s.m}|${s.id}`;
const scanShowing = id => activeTab === 'net' && portsOf === id;

function scanTake(m) {
  if (m.now) scanSkew = m.now - Date.now() / 1000;
  let fresh = false;
  let noted = false;
  for (const s of m.scans || []) {
    s.m = s.m || m.m || 'local';
    if (!scanKnown.has(scanKey(s)) && !s.ended) {
      if (scanShowing(s.m)) fresh = true;
      else scanUnseen.add(s.m);
    }
    scanKnown.set(scanKey(s), s);
    const note = scanNotes.get(s.m);
    if (s.m !== 'local' && !scanNoteGone.has(scanKey(s)) && (!note || note.key === scanKey(s) || note.at < s.first - scanSkew)) {
      scanNotes.set(s.m, { key: scanKey(s), at: s.first - scanSkew, from: s.machines === 1 ? s.remotes[0][0] : `${s.machines} machines`, ports: s.total });
      noted = true;
    }
  }
  if (noted) scanNotesSave();
  scanRender();
  if (fresh) {
    scanFitDue = true;
    requestAnimationFrame(scanFit);
  }
}
scanListeners.push(scanTake);
portsListeners.push(id => {   // another machine's page: its own bubbles and alerts
  for (const p of [...bubOpenNow.keys()]) bubClose(p);
  scanUnseen.delete(id);
  scanFitDue = [...scanKnown.values()].some(s => s.m === id && !s.ended);
  scanRender();
});

// Scans from before this page opened (or before a reconnect).
async function scanLoad() {
  try {
    scanTake(await (await fetch('/api/scans', { cache: 'no-store' })).json());
  } catch { /* the stream brings new ones anyway */ }
}

const scanLive = id => [...scanKnown.values()].some(s => s.m === id && !s.ended);

// Which tabs throb, and which machines' cards say they were scanned.
function scanMarkTab() {
  const shown = typeof viewMachine === 'function' ? viewMachine() : 'local';
  const top = typeof nav !== 'undefined' ? nav.top : 'local';
  scanTab.classList.toggle('alarm', scanLive(shown) || scanUnseen.has(shown));
  scanTop.classList.toggle('alarm', top !== 'local' && scanUnseen.has('local'));
  const remote = [...scanUnseen].filter(id => id !== 'local' && !(top === 'remote' && id === shown));
  scanRemoteTop.classList.toggle('alarm', remote.length > 0 && !(top === 'remote' && !nav.machine));
  if (typeof rCards !== 'undefined') {
    for (const [id, c] of rCards) {
      const note = scanNotes.get(id), live = note && scanLive(id) && !scanKnown.get(note.key)?.ended;
      const el = c.el.querySelector('.rscan');
      c.el.classList.toggle('scanned', !!note);
      c.el.classList.toggle('scan-live', !!live);
      el.hidden = !note;
      if (!note) continue;
      const text = live ? 'Port scan under way' :`Port scanned ${bubWhen(note.at).replace(/\s/g, ' ')}`;   // the time on one line
      if (el.textContent !== text) el.textContent = text;
      el.dataset.short = live ? 'Being scanned' : `Scanned ${bubWhen(note.at)}`;
      el.title = `${live ? 'Being port scanned' : `Port scanned ${bubWhen(note.at)}`}, from ${note.from}: ${note.ports.toLocaleString()} ports.` +
        '\nIts Ports page has the details. Click this note to dismiss it.';
    }
  }
}

// A card's note, clicked: gone until there's another scan of that machine.
function scanNoteDismiss(id) {
  const note = scanNotes.get(id);
  if (!note) return;
  scanNoteGone.add(note.key);
  scanNotes.delete(id);
  scanUnseen.delete(id);
  scanNotesSave();
  scanMarkTab();
}

// Notes of machines no longer watched go too.
if (typeof perfMetaListeners !== 'undefined') {
  perfMetaListeners.push(meta => {
    const ids = new Set(meta.machines.map(m => m.id));
    let gone = false;
    for (const id of [...scanNotes.keys()]) if (!ids.has(id)) { scanNotes.delete(id); gone = true; }
    if (gone) scanNotesSave();
  });
}
if (typeof perfMetaListeners !== 'undefined') perfMetaListeners.push(() => requestAnimationFrame(scanMarkTab));   // new cards

tabListeners.push(name => {
  if (name === 'net') {
    scanUnseen.delete(portsOf);
    if (scanFitDue) requestAnimationFrame(scanFit);
  }
  scanMarkTab();
});

// Zoom out so both grids fit beside the alerts.
function scanFit() {
  if (activeTab !== 'net' || !scanBox.firstElementChild) return;
  const s = stage.getBoundingClientRect(), a = scanBox.getBoundingClientRect();
  if (!a.width) return;
  scanFitDue = false;
  const clear = r => ({ x: r.left - s.left - 10, y: r.top - s.top - 10, w: r.width + 20, h: r.height + 20 });
  fitAround([clear(a), clear(stage.querySelector('.legend').getBoundingClientRect())]);
}

// The button brings back the newest alert, if it was dismissed, and makes room for it.
scanLight.addEventListener('click', () => {
  const newest = [...scanKnown.values()].filter(s => s.m === portsOf).sort((a, b) => b.first - a.first)[0];
  if (!newest) return;
  scanDismissed.delete(scanKey(newest));
  savePref('dismissedScans', Object.fromEntries(scanDismissed));
  scanRender();
  requestAnimationFrame(scanFit);
});

// "1–1024, 3306, 8080–8081" from sorted port numbers.
function scanRanges(list) {
  const out = [];
  for (let i = 0; i < list.length;) {
    let j = i;
    while (j + 1 < list.length && list[j + 1] === list[j] + 1) j++;
    out.push(j - i >= 2 ? `${list[i]}–${list[j]}` : j === i ? `${list[i]}` : `${list[i]}, ${list[j]}`);
    i = j + 1;
  }
  return out.join(', ');
}

function scanTime(sec) {
  return new Date((sec - scanSkew) * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' });
}

function scanDuration(s) {
  return s < 90 ? `${Math.max(1, Math.round(s))} s` : `${Math.round(s / 60)} min`;
}

const SCAN_ORDER = { up: 'in order, lowest first', down: 'in order, highest first', mixed: 'in random order' };

function scanFrom(s) {
  const one = r => `<span class="mono">${esc(r[0])}</span>${r[1] ? ` <span class="faint">(${esc(r[1])})</span>` : ''}`;
  if (s.machines === 1) return `From ${one(s.remotes[0])}`;
  const some = s.remotes.slice(0, 6), more = s.machines - some.length;
  return `From ${s.machines.toLocaleString()} machines together: ${some.map(one).join(', ')}` +
    (more ? ` and ${more.toLocaleString()} more` : '');
}

function scanCard(s) {
  const tcp = s.ports.filter(x => x[1] === 'TCP').map(x => x[0]);
  const udp = s.ports.filter(x => x[1] === 'UDP').map(x => x[0]);
  const open = s.ports.filter(x => x[2] === 'open').map(x => `${x[1]} ${x[0]}`);
  const n = s.total, more = n - s.ports.length;
  const phase = s.ended ? `ended ${scanTime(s.last)}` : 'in progress';
  return `<div class="alert${s.ended ? ' ended' : ''}" data-id="${esc(scanKey(s))}" role="alert">` +
    `<div class="al-head"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.5 14.5 13.5h-13z"/><path d="M8 6v3.6M8 11.4v.1"/></svg>` +
    `<b>Port scan detected</b><span class="al-state">${phase}</span>` +
    `<button type="button" class="al-x" aria-label="Dismiss" title="Dismiss">✕</button></div>` +
    `<div class="al-from">${scanFrom(s)}</div>` +
    `<div class="al-stats">${n.toLocaleString()} port${n === 1 ? '' : 's'} probed ${SCAN_ORDER[s.order]} · ${s.probes.toLocaleString()} packets · ` +
    `over ${scanDuration(s.last - s.first)} · since ${scanTime(s.first)}</div>` +
    (open.length ? `<div class="al-open">Answered (open here): ${esc(open.join(', '))}</div>` : '') +
    `<div class="al-ports">` +
    (tcp.length ? `<div><span class="al-k">TCP</span> ${scanRanges(tcp)}</div>` : '') +
    (udp.length ? `<div><span class="al-k">UDP</span> ${scanRanges(udp)}</div>` : '') +
    (more > 0 ? `<div>and ${more.toLocaleString()} more</div>` : '') +
    `</div></div>`;
}

function scanRender() {
  const show = [...scanKnown.values()]
    .filter(s => s.m === portsOf && (!scanDismissed.has(scanKey(s)) || s.probes > scanDismissed.get(scanKey(s))))
    .sort((a, b) => b.first - a.first);
  scanBox.innerHTML = show.map(scanCard).join('');
  stage.classList.toggle('has-alert', show.length > 0);
  const live = scanLive(portsOf);
  scanLight.classList.toggle('on', live);
  scanLight.setAttribute('aria-pressed', String(live));
  scanLight.title = live ? 'A port scan is under way. Click to show its alert.'
    : 'Lights up while a port scan is under way. Click to show the latest alert.';
  scanMarkTab();
}

scanBox.addEventListener('click', ev => {
  const x = ev.target.closest('.al-x');
  if (!x) return;
  const id = x.closest('.alert').dataset.id, s = scanKnown.get(id);
  scanDismissed.set(id, s ? s.probes : 0);
  // Keep the newest 50 dismissals, enough to cover what the server still remembers.
  const keep = [...scanDismissed].slice(-50);
  savePref('dismissedScans', Object.fromEntries(keep));
  scanRender();
});

scanLoad();
