'use strict';

// ---------------------------------------------------------------------------
// Pictures of memory modules for the Memory page, with what the firmware
// says about each one made part of the picture: engraved in a heat spreader,
// printed on a bare circuit board, or etched on soldered chips. The firmware
// doesn't describe a module's looks, so the product line in its part number
// picks them. Pictures are kept, so drawing one again costs nothing.
// ---------------------------------------------------------------------------
const STICK_H = 124, STICK_MARGIN = 8;             // height and the room around it (glow, shadow), px
const STICK_FONT = '"Bahnschrift SemiCondensed", Bahnschrift, "DIN Alternate", "Segoe UI", sans-serif';
const STICK_MONO = 'Consolas, "Cascadia Mono", monospace';

// Metal finishes: the gradient top to bottom, text on it, and the two sides of an engraved edge
const FINISHES = {
  black: { metal: ['#4a4e55', '#2c2f34', '#191b1e'], ink: '#f4f6f8', dim: '#bcc2ca', hi: 'rgba(255,255,255,0.16)', lo: 'rgba(0,0,0,0.75)' },
  graphite: { metal: ['#777d86', '#4a4f57', '#2d3036'], ink: '#ffffff', dim: '#d3d8de', hi: 'rgba(255,255,255,0.22)', lo: 'rgba(0,0,0,0.7)' },
  silver: { metal: ['#f1f3f5', '#cdd2d8', '#a5abb4'], ink: '#14171b', dim: '#333941', hi: 'rgba(255,255,255,0.8)', lo: 'rgba(255,255,255,0.75)' },
  red: { metal: ['#b8383f', '#8a1c24', '#560e14'], ink: '#fff6f2', dim: '#f7cdc6', hi: 'rgba(255,255,255,0.2)', lo: 'rgba(40,0,0,0.75)' },
  blue: { metal: ['#3f69b0', '#234784', '#132a54'], ink: '#f4f9ff', dim: '#c4d7f5', hi: 'rgba(255,255,255,0.2)', lo: 'rgba(0,8,40,0.75)' },
};
// Circuit boards: gradient, printed text, and the lines of the traces
const BOARDS = {
  green: { board: ['#1f5f43', '#134030'], ink: '#f4f3df', dim: '#c3dcc0', trace: 'rgba(140,220,170,0.16)' },
  black: { board: ['#2b2e30', '#17191a'], ink: '#f3f1e2', dim: '#bfc3bd', trace: 'rgba(220,220,200,0.12)' },
};
// Part number -> the finish of that product line's heat spreader; the first match wins.
// Anything else is drawn as a bare module.
const SPREADERS = [
  [/^CP\d/, 'black'],                                      // Crucial Pro
  [/^BL/, 'graphite'],                                     // Crucial Ballistix
  [/^CM[KWTHGRDNP].*C\d\dW/, 'silver'], [/^CM[KWTHGRDNP].*C\d\dR$/, 'red'], [/^CM[KWTHGRDNP].*C\d\dB$/, 'blue'],
  [/^CM[KWTHGRDNP]/, 'black'],                             // Corsair Vengeance, Dominator
  [/^KF\d+C\d+([A-Z]W|RS)/, 'silver'], [/^(KF|KHX)/, 'black'],   // Kingston FURY, HyperX
  [/^F\d-.*GVR/, 'red'], [/^F\d-.*GVB/, 'blue'], [/^F\d-.*GVS/, 'silver'], [/^F\d-.*GVK/, 'black'],   // G.Skill Ripjaws V
  [/^F\d-/, 'graphite'],                                   // G.Skill
  [/^(TF|FF|TLZ|TDZ)/, 'graphite'],                        // TeamGroup T-Force
  [/^(PV|AX\d)/, 'black'],                                 // Patriot Viper, ADATA XPG
];

function stickLook(m) {
  const part = (m.part || '').toUpperCase();
  const line = SPREADERS.find(([re]) => re.test(part));
  if (/^(Row of chips|Chip|Die)$/.test(m.form) || /^LPDDR/.test(m.type)) return { kind: 'soldered', ...BOARDS.black };
  if (line && m.form !== 'SODIMM') return { kind: 'spreader', ...FINISHES[line[1]], board: BOARDS.black.board, silk: BOARDS.black.dim };
  return { kind: 'bare', ...(line ? BOARDS.black : BOARDS.green) };
}

// What's written on a module, most important first.
function stickFacts(m, slot, w) {
  const rate = m.configured || m.speed;
  return {
    brand: (m.brand || m.maker || 'Memory').toUpperCase(),
    size: fmtModule(m.size),
    spec: [m.type, rate ? `${rate} MT/s` : ''].filter(Boolean).join(' · '),
    short: [m.type, rate].filter(Boolean).join('-'),
    more: [RANKS[m.rank] || '', m.volts ? `${(m.volts / 1000).toFixed(2)} V` : '', m.ecc ? 'ECC' : ''].filter(Boolean).join(' · '),
    part: m.part || '',
    serial: m.serial ? `S/N ${m.serial}` : '',
    slot: (w < 190 && m.locator || slot).toUpperCase(),   // the channel is named above it anyway
  };
}

// Text that shrinks a little to fit, then gives way with an ellipsis. o.cut
// draws the edge that makes it look engraved or printed.
function stickWrite(g, str, x, y, maxW, size, color, o = {}) {
  if (!str || maxW < 12) return 0;
  const set = s => { g.font = `${o.weight || 400} ${s}px ${o.mono ? STICK_MONO : STICK_FONT}`; };
  g.letterSpacing = `${o.spacing || 0}px`;
  g.textBaseline = 'middle';
  g.textAlign = o.align || 'left';
  set(size);
  let w = g.measureText(str).width;
  if (w > maxW) {
    size = Math.max(size * 0.8, size * maxW / w);
    set(size);
    while (str.length > 1 && g.measureText(str).width > maxW) str = str.replace(/…$/, '').slice(0, -1) + '…';
    w = g.measureText(str).width;
  }
  if (o.cut) {
    g.fillStyle = o.cut;
    g.fillText(str, x, y + (o.cutDy || 0.9));
  }
  g.fillStyle = color;
  g.fillText(str, x, y);
  g.letterSpacing = '0px';
  return w;
}

// One line with text at each end; the right one is left off if both don't fit.
function stickRow(g, y, x0, x1, left, right) {
  let rw = 0;
  if (right && right.str) {
    g.font = `${right.o && right.o.weight || 400} ${right.size}px ${right.o && right.o.mono ? STICK_MONO : STICK_FONT}`;
    rw = g.measureText(right.str).width;
    g.font = `${left.o && left.o.weight || 400} ${left.size}px ${left.o && left.o.mono ? STICK_MONO : STICK_FONT}`;
    g.letterSpacing = `${left.o && left.o.spacing || 0}px`;
    const lw = g.measureText(left.str).width;
    g.letterSpacing = '0px';
    const fits = right.keep || lw + 10 + rw <= x1 - x0;
    if (!fits) rw = 0;
  }
  stickWrite(g, left.str, x0, y, x1 - x0 - (rw ? rw + 10 : 0), left.size, left.color, left.o);
  if (rw) stickWrite(g, right.str, x1, y, rw + 1, right.size, right.color, { ...right.o, align: 'right' });
}

function stickGradient(g, y0, y1, stops) {
  const gr = g.createLinearGradient(0, y0, 0, y1);
  stops.forEach((c, i) => gr.addColorStop(i / (stops.length - 1), c));
  return gr;
}

// The gold contacts along the bottom edge, the key notch, and the side notches.
function stickEdge(g, w, h, m, top) {
  const gold = stickGradient(g, top, h, ['#f6e2a0', '#d3a548', '#9d7326']);
  g.fillStyle = gold;
  for (let x = 6; x < w - 7; x += 3.1) g.fillRect(x, top + 1, 1.9, h - top - 2);
  const key = { DDR5: 0.5, DDR4: 0.53, DDR3: 0.41, DDR2: 0.47 }[m.type] || 0.47;
  g.globalCompositeOperation = 'destination-out';
  g.beginPath();
  g.roundRect(w * key - 2.5, h - 10, 5, 14, [2.5, 2.5, 0, 0]);
  for (const x of [0, w]) {   // where the slot's latches grip
    g.moveTo(x + 3.2, h * 0.66);
    g.arc(x, h * 0.66, 3.2, 0, Math.PI * 2);
  }
  g.fill();
  g.globalCompositeOperation = 'source-over';
}

function stickShadow(g, path) {
  g.save();
  g.shadowColor = 'rgba(0,0,0,0.4)';
  g.shadowBlur = 5;
  g.shadowOffsetY = 2;
  g.fillStyle = '#000';
  g.fill(path);
  g.restore();
}

// A module with a heat spreader: everything is engraved in the metal, and the
// light bar along the top has its channel's color.
function drawSpreader(g, w, h, m, f, look, color) {
  const contacts = h - 12, foot = h - 25;
  const pcb = new Path2D();
  pcb.roundRect(1, 22, w - 2, h - 22, 2);
  stickShadow(g, pcb);
  g.fillStyle = stickGradient(g, 22, h, look.board);
  g.fill(pcb);
  stickEdge(g, w, h, m, contacts);

  const l = 1, r = w - 1, crown = [w * 0.27, w * 0.31, w * 0.69, w * 0.73];   // raised in the middle
  const metal = new Path2D();
  metal.moveTo(l, 15);
  metal.lineTo(l + 8, 8);
  metal.lineTo(crown[0], 8);
  metal.lineTo(crown[1], 3);
  metal.lineTo(crown[2], 3);
  metal.lineTo(crown[3], 8);
  metal.lineTo(r - 8, 8);
  metal.lineTo(r, 15);
  metal.lineTo(r, foot - 5);
  metal.lineTo(r - 6, foot);
  metal.lineTo(l + 6, foot);
  metal.lineTo(l, foot - 5);
  metal.closePath();
  stickShadow(g, metal);
  g.fillStyle = stickGradient(g, 3, foot, look.metal);
  g.fill(metal);
  g.save();
  g.clip(metal);
  for (let y = 4; y < foot; y += 1.5) {   // brushed, the same every time
    const n = Math.sin(y * 12.9898) * 43758.5453 % 1;
    g.fillStyle = n > 0 ? `rgba(255,255,255,${(0.035 * n).toFixed(3)})` : `rgba(0,0,0,${(-0.07 * n).toFixed(3)})`;
    g.fillRect(0, y, w, 0.8);
  }
  for (let k = 0; k < 4; k++) {   // fins cut in both ends
    for (const x of [4.5 + k * 2.6, w - 5.5 - k * 2.6]) {
      g.fillStyle = look.lo;
      g.globalAlpha = 0.45;
      g.fillRect(x, 20, 1, foot - 30);
      g.globalAlpha = 1;
      g.fillStyle = look.hi;
      g.fillRect(x + 1, 20, 0.6, foot - 30);
    }
  }
  g.fillStyle = look.hi;   // light catching the fold above the foot
  g.fillRect(0, foot - 5, w, 0.8);
  g.restore();
  // The light bar
  g.save();
  g.lineJoin = 'round';
  g.lineCap = 'round';
  g.beginPath();
  g.moveTo(l + 9, 9.5);
  g.lineTo(crown[0], 9.5);
  g.lineTo(crown[1], 4.5);
  g.lineTo(crown[2], 4.5);
  g.lineTo(crown[3], 9.5);
  g.lineTo(r - 9, 9.5);
  g.shadowColor = rgb(color);
  g.shadowBlur = 8;
  g.strokeStyle = rgb(color);
  g.lineWidth = 3;
  g.stroke();
  g.shadowBlur = 0;
  g.strokeStyle = 'rgba(255,255,255,0.55)';
  g.lineWidth = 0.9;
  g.stroke();
  g.restore();

  const x0 = 19, x1 = w - 19, cut = look.lo, small = w < 190;
  const ink = look.ink, dim = look.dim;
  stickRow(g, 26, x0, x1, { str: f.brand, size: small ? 14 : 17, color: ink, o: { weight: 700, spacing: 0.7, cut } },
    { str: small ? '' : f.size, size: 17, color: ink, o: { weight: 600, cut }, keep: true });
  const rule = g.createLinearGradient(x0, 0, x1, 0);
  rule.addColorStop(0, rgb(color));
  rule.addColorStop(1, rgba(color, 0.15));
  g.fillStyle = rule;
  g.fillRect(x0, 38, x1 - x0, 1.6);
  stickRow(g, 50, x0, x1, { str: small ? `${f.size} ${f.short}` : f.spec, size: small ? 12.5 : 13.5, color: ink, o: { weight: 600, spacing: 0.2, cut } },
    { str: f.more, size: 11.5, color: dim, o: { cut } });
  stickRow(g, 67, x0, x1, { str: f.part || 'No part number', size: 12, color: ink, o: { mono: true, cut } });
  stickRow(g, 84, x0, x1, { str: f.serial || 'No serial number', size: 11.5, color: dim, o: { mono: true, cut } });
  stickRow(g, foot + 7, 8, w - 8, { str: f.slot, size: 9.5, color: look.silk, o: { weight: 600, spacing: 0.5 } });
}

function drawChip(g, x, y, w, h, mark) {
  g.fillStyle = stickGradient(g, y, y + h, ['#2a2c2e', '#111213']);
  g.beginPath();
  g.roundRect(x, y, w, h, 1.2);
  g.fill();
  g.strokeStyle = 'rgba(255,255,255,0.10)';
  g.lineWidth = 0.6;
  g.stroke();
  g.fillStyle = 'rgba(255,255,255,0.28)';   // the mark at pin 1
  g.beginPath();
  g.arc(x + 3, y + 3, 0.9, 0, Math.PI * 2);
  g.fill();
  if (mark && w > 14) stickWrite(g, mark, x + w / 2, y + h / 2, w - 4, 6.5, 'rgba(255,255,255,0.42)', { align: 'center' });
}

// A bare module: chips on a circuit board, everything printed on the board.
function drawBare(g, w, h, m, f, look, color) {
  const contacts = h - 12;
  const pcb = new Path2D();
  pcb.roundRect(1, 2, w - 2, h - 2, [4, 4, 2, 2]);
  stickShadow(g, pcb);
  g.fillStyle = stickGradient(g, 2, h, look.board);
  g.fill(pcb);
  g.save();
  g.clip(pcb);
  g.strokeStyle = look.trace;
  g.lineWidth = 0.7;
  g.beginPath();
  for (let x = 9; x < w - 8; x += 4.4) {   // traces from the contacts up to the chips
    const n = Math.abs(Math.sin(x * 7.13) * 91.7 % 1), bend = 78 + n * 20;
    g.moveTo(x, contacts);
    g.lineTo(x, bend);
    g.lineTo(x + (n - 0.5) * 9, bend - 5);
    g.lineTo(x + (n - 0.5) * 9, 74);
  }
  g.stroke();
  g.fillStyle = rgb(color);   // its channel's color
  g.fillRect(0, 0, w, 6);
  g.fillStyle = 'rgba(255,255,255,0.3)';
  g.fillRect(0, 2, w, 1);
  g.restore();
  const n = m.form === 'SODIMM' ? 4 : w >= 190 ? 8 : 6, hub = 12, x0 = 12, x1 = w - 12;
  const cw = (x1 - x0 - hub - 4) / n;
  for (let k = 0; k < n; k++) {
    const x = x0 + k * cw + (k >= n / 2 ? hub + 4 : 0);
    drawChip(g, x + 1, 44, cw - 2.5, 31, m.type);
  }
  drawChip(g, w / 2 - hub / 2, 52, hub, 14, '');   // the module's own data, read by the firmware
  stickEdge(g, w, h, m, contacts);
  const small = w < 190, ink = look.ink, dim = look.dim, cut = 'rgba(0,0,0,0.35)';
  stickRow(g, 18, x0, x1, { str: f.brand, size: small ? 13.5 : 16, color: ink, o: { weight: 700, spacing: 0.7, cut } },
    { str: small ? '' : f.size, size: 16, color: ink, o: { weight: 600, cut }, keep: true });
  stickRow(g, 34, x0, x1, { str: small ? `${f.size} ${f.short}` : f.spec, size: 12.5, color: ink, o: { weight: 600, cut } },
    { str: f.more, size: 11, color: dim, o: { cut } });
  stickRow(g, 86, x0, x1, { str: f.part || 'No part number', size: 12, color: ink, o: { mono: true, cut } });
  stickRow(g, 101, x0, x1, { str: f.serial || f.slot, size: 11, color: dim, o: { mono: true, cut } },
    { str: f.serial ? f.slot : '', size: 9.5, color: dim, o: { weight: 600, cut } });
}

// Memory soldered to the main board: a chip package with everything etched on it.
function drawSoldered(g, w, h, m, f, look, color) {
  const pcb = new Path2D();
  pcb.roundRect(1, 2, w - 2, h - 4, 4);
  stickShadow(g, pcb);
  g.fillStyle = stickGradient(g, 2, h, look.board);
  g.fill(pcb);
  g.save();
  g.clip(pcb);
  g.strokeStyle = look.trace;
  g.lineWidth = 0.8;
  g.beginPath();
  for (let y = 10; y < h - 8; y += 5) {
    g.moveTo(0, y);
    g.lineTo(w, y);
  }
  g.stroke();
  g.restore();
  const x = 10, y = 9, pw = w - 20, ph = h - 20;
  g.fillStyle = stickGradient(g, y, y + ph, ['#2e3033', '#1a1b1d']);
  g.beginPath();
  g.roundRect(x, y, pw, ph, 3);
  g.fill();
  g.strokeStyle = 'rgba(255,255,255,0.14)';
  g.lineWidth = 1;
  g.stroke();
  g.fillStyle = rgb(color);
  g.beginPath();
  g.arc(x + 9, y + 9, 3, 0, Math.PI * 2);
  g.fill();
  const x0 = x + 18, x1 = x + pw - 12, ink = '#e4e6e8', dim = '#b1b5ba', small = w < 190;
  stickRow(g, y + 15, x0, x1, { str: f.brand, size: small ? 13 : 16, color: ink, o: { weight: 700, spacing: 0.7 } },
    { str: small ? '' : f.size, size: 16, color: ink, o: { weight: 600 }, keep: true });
  stickRow(g, y + 35, x0, x1, { str: small ? `${f.size} ${f.short}` : f.spec, size: 12.5, color: ink, o: { weight: 600 } },
    { str: f.more, size: 11, color: dim });
  stickRow(g, y + 53, x0, x1, { str: f.part || 'No part number', size: 12, color: ink, o: { mono: true } });
  stickRow(g, y + 70, x0, x1, { str: f.serial || f.slot, size: 11, color: dim, o: { mono: true } });
  if (f.serial) stickRow(g, y + 86, x0, x1, { str: f.slot, size: 9.5, color: dim, o: { weight: 600 } });
}

// A slot with nothing in it: the socket, its latches open, and where a module would go.
function drawSocket(g, w, h, m, slot) {
  g.setLineDash([4, 3]);
  g.strokeStyle = T.muted;
  g.lineWidth = 1;
  g.beginPath();
  g.roundRect(7, 8, w - 14, h - 30, 5);
  g.stroke();
  g.setLineDash([]);
  g.fillStyle = stickGradient(g, h - 18, h, ['#3b3e43', '#202226']);
  g.beginPath();
  g.roundRect(4, h - 17, w - 8, 17, 2);
  g.fill();
  g.fillStyle = '#0c0d0e';
  g.fillRect(12, h - 12, w - 24, 5);
  g.fillStyle = '#b98f3d';
  for (let x = 14; x < w - 15; x += 3.1) g.fillRect(x, h - 11, 1.4, 3);
  for (const side of [0, 1]) {   // latches
    g.save();
    if (side) { g.translate(w, 0); g.scale(-1, 1); }
    g.fillStyle = stickGradient(g, h - 34, h, ['#dfdfda', '#a9a9a3']);
    g.beginPath();
    g.moveTo(1, h - 4);
    g.lineTo(-3, h - 32);
    g.lineTo(3, h - 34);
    g.lineTo(9, h - 4);
    g.closePath();
    g.fill();
    g.restore();
  }
  const text = m.size === 0 ? 'Empty slot' : 'Size not reported';
  stickWrite(g, text, w / 2, h / 2 - 20, w - 24, 13, T.ink2, { weight: 600, align: 'center' });
  stickWrite(g, slot, w / 2, h / 2 - 3, w - 24, 11.5, T.muted, { align: 'center' });
}

// The picture of a module (or an empty slot) in a w px wide box, at ratio
// device px per px, with STICK_MARGIN around it.
const stickPictures = new Map();
function stickPicture(m, slot, w, color, ratio) {
  const key = JSON.stringify([m, slot, w, color, ratio, darkQuery.matches]);
  let cv = stickPictures.get(key);
  if (cv) return cv;
  if (stickPictures.size > 96) stickPictures.clear();   // after many window sizes
  cv = document.createElement('canvas');
  cv.width = Math.round((w + 2 * STICK_MARGIN) * ratio);
  cv.height = Math.round((STICK_H + 2 * STICK_MARGIN) * ratio);
  const g = cv.getContext('2d');
  g.scale(ratio, ratio);
  g.translate(STICK_MARGIN, STICK_MARGIN);
  if (!m.size) drawSocket(g, w, STICK_H, m, slot);
  else {
    const look = stickLook(m), draw = { spreader: drawSpreader, bare: drawBare, soldered: drawSoldered }[look.kind];
    draw(g, w, STICK_H, m, stickFacts(m, slot, w), look, color);
  }
  stickPictures.set(key, cv);
  return cv;
}
