// live.js: Live tab: MJPEG live view (released whenever it is not visible), overlay
// toggle, fullscreen, click-to-follow on tracked boxes, today counters and recent activity.
import { api, bus, state, mediaRev } from './api.js';
import { CATS, PRIORITY, h, icon, catChip, eventCard, cardSig, emptyState, errorState, store, fmtInt, fmtNum, evDay, today, keepFocus } from './ui.js';
import { initPtz, followTrack } from './ptz.js';
import { openViewer } from './viewer.js';

const RETRY_MS = 2000;
const RECENT_N = 6;

let els = {};
let active = false;        // Live tab is shown
let viewerOpen = false;
let overlay = true;
let streaming = false;     // the <img> currently has a stream URL
let gotFrame = false;
let retryTimer = 0;
let cameraOnline = true;
let statusKnown = false;   // the first stream request waits for the first status
let hoverId = null;
let recent = [];
let recentLoaded = false;
let counts = null;
const recentCards = new Map();

/* ------------------------------------------------------------------ MJPEG lifecycle */
// Browsers allow ~6 connections per host and MJPEG holds one for as long as it runs, so
// the stream only runs while the Live tab is on screen and the page is visible.

// While the status says the camera is offline there is nothing to stream; the
// offline -> online transition restarts it (and keeps the console free of 503s).
const wanted = () => active && !document.hidden && !viewerOpen && cameraOnline && statusKnown;

function syncStream() {
  if (wanted()) startStream();
  else stopStream();
}

function streamUrl(bust) {
  return `/api/live.mjpg?overlay=${overlay ? 1 : 0}${bust ? `&_=${Date.now()}` : ''}`;
}

function startStream(bust = false) {
  clearTimeout(retryTimer);
  if (streaming && !bust) return;
  streaming = true;
  setFrame(false);
  els.stage.classList.add('is-loading');
  els.img.src = streamUrl(bust);
}

/** Whether the <img> shows real frames (it stays invisible until then: no broken icon). */
function setFrame(on) {
  gotFrame = on;
  els.stage.classList.toggle('has-frame', on);
  renderBadge();
}

function renderBadge() {
  const s = state.status?.camera;
  let text = 'Live';
  let cls = '';
  if (!cameraOnline) { text = 'Offline'; cls = 'is-offline'; }
  else if (!gotFrame) { text = 'Connecting'; cls = 'is-connecting'; }
  els.badgeText.textContent = text;
  els.badge.className = `live-badge ${cls}`.trim();
  els.fps.textContent = !cls && Number.isFinite(s?.fps) ? `${fmtNum(s.fps, 0)} fps` : '';
}

function stopStream() {
  clearTimeout(retryTimer);
  if (!streaming) return;
  streaming = false;
  freezeFrame();
  // Removing the attribute aborts the multipart request and frees the connection; unlike
  // src = '' it does not fire an error event that a quick restart could mistake for a failure.
  els.img.removeAttribute('src');
  els.stage.classList.remove('is-loading');
}

function restartStream() {
  if (!wanted()) return;
  freezeFrame();
  streaming = false;
  startStream(true);
}

/** Keep the last frame on a canvas so stopping or switching streams never flashes black. */
function freezeFrame() {
  const { img, poster } = els;
  if (!gotFrame || !img.naturalWidth) return;
  try {
    poster.width = img.naturalWidth;
    poster.height = img.naturalHeight;
    poster.getContext('2d').drawImage(img, 0, 0);
    poster.hidden = false;
  } catch {
    /* some browsers refuse to draw a multipart image; the spinner covers the gap */
  }
}

function onFrame() {
  if (!streaming) return;
  if (!gotFrame) {
    setFrame(true);
    els.poster.hidden = true;
    els.stage.classList.remove('is-loading', 'is-retrying');
    draw();
  }
}

function onStreamError() {
  if (!streaming || !els.img.getAttribute('src')) return; // not a stream we are waiting for
  setFrame(false);
  els.stage.classList.add('is-retrying');
  els.stage.classList.remove('is-loading');
  clearTimeout(retryTimer);
  retryTimer = setTimeout(() => {
    if (!wanted()) return;
    streaming = false;
    startStream(true);
  }, RETRY_MS);
}

/* ------------------------------------------------------------------ overlay, fullscreen */

function setOverlay(on) {
  overlay = on;
  store.set('overlay', on);
  els.overlayBtn.setAttribute('aria-pressed', String(on));
  els.overlayBtn.title = on ? 'Hide detection boxes' : 'Show detection boxes';
  if (streaming) restartStream();
}

const fsElement = () => document.fullscreenElement || document.webkitFullscreenElement;

function toggleFullscreen() {
  const stage = els.stage;
  if (stage.classList.contains('is-pseudo-fs')) {
    setPseudoFs(false);
  } else if (fsElement()) {
    (document.exitFullscreen || document.webkitExitFullscreen).call(document);
  } else if (stage.requestFullscreen) {
    stage.requestFullscreen().catch(() => setPseudoFs(true));
  } else if (stage.webkitRequestFullscreen) {
    stage.webkitRequestFullscreen();
  } else {
    setPseudoFs(true); // iPhone Safari: no element fullscreen, fill the viewport instead
  }
}

function setPseudoFs(on) {
  els.stage.classList.toggle('is-pseudo-fs', on);
  document.body.classList.toggle('no-scroll', on);
  syncFsButton();
}

function syncFsButton() {
  const on = !!fsElement() || els.stage.classList.contains('is-pseudo-fs');
  els.fsBtn.replaceChildren(icon(on ? 'collapse' : 'expand'), h('span', { class: 'sr-only', text: on ? 'Exit fullscreen' : 'Enter fullscreen' }));
  els.fsBtn.title = on ? 'Exit fullscreen' : 'Fullscreen';
  requestAnimationFrame(draw);
}

/* ------------------------------------------------------------------ click-to-follow */

/** Where the image content actually sits inside the element (object-fit: contain). */
function contentRect() {
  const r = els.img.getBoundingClientRect();
  const nw = els.img.naturalWidth || 16;
  const nh = els.img.naturalHeight || 9;
  const scale = Math.min(r.width / nw, r.height / nh);
  const w = nw * scale;
  const hgt = nh * scale;
  return { left: r.left + (r.width - w) / 2, top: r.top + (r.height - hgt) / 2, width: w, height: hgt, box: r };
}

function normPoint(e) {
  const c = contentRect();
  return { x: (e.clientX - c.left) / c.width, y: (e.clientY - c.top) / c.height };
}

/** Smallest tracked box containing the point (small objects win over big ones behind them). */
function pickTrack(x, y) {
  const list = state.tracks?.tracks;
  if (!list?.length || x < 0 || x > 1 || y < 0 || y > 1) return null;
  const slack = 0.01; // a little forgiveness for fingers
  let best = null;
  let bestArea = Infinity;
  for (const t of list) {
    const [x1, y1, x2, y2] = t.box;
    if (x >= x1 - slack && x <= x2 + slack && y >= y1 - slack && y <= y2 + slack) {
      const area = (x2 - x1) * (y2 - y1);
      if (area < bestArea) {
        best = t;
        bestArea = area;
      }
    }
  }
  return best;
}

function onStageClick(e) {
  if (e.target.closest('button') || !gotFrame) return;
  const p = normPoint(e);
  const t = pickTrack(p.x, p.y);
  if (t) followTrack(t);
  else if (state.tracks?.tracks?.length) showStageToast('Tap directly on a box to follow it');
}

function onStageMove(e) {
  if (e.pointerType !== 'mouse') return;
  const p = normPoint(e);
  const t = gotFrame ? pickTrack(p.x, p.y) : null;
  const id = t ? t.id : null;
  if (id !== hoverId) {
    hoverId = id;
    els.stage.classList.toggle('is-pickable', id != null);
    draw();
  }
}

let stageToastTimer = 0;
function showStageToast(text) {
  els.stageToast.textContent = text;
  els.stageToast.hidden = false;
  clearTimeout(stageToastTimer);
  stageToastTimer = setTimeout(() => { els.stageToast.hidden = true; }, 1800);
}

/** Outline the hovered box and the followed target on a transparent canvas. */
function draw() {
  const cv = els.canvas;
  const ctx = cv.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const box = els.stage.getBoundingClientRect();
  const W = Math.round(box.width * dpr);
  const H = Math.round(box.height * dpr);
  if (cv.width !== W || cv.height !== H) {
    cv.width = W;
    cv.height = H;
  }
  ctx.clearRect(0, 0, W, H);
  const list = state.tracks?.tracks;
  if (!list?.length || !gotFrame || !cameraOnline) return;
  const p = state.status?.ptz;
  const targetId = p?.autotrack ? p.target_id : null;
  const c = contentRect();
  const ox = (c.left - box.left) * dpr;
  const oy = (c.top - box.top) * dpr;
  const cw = c.width * dpr;
  const ch = c.height * dpr;
  for (const t of list) {
    const isTarget = t.target || (targetId != null && t.id === targetId);
    const isHover = t.id === hoverId;
    if (!isTarget && !isHover) continue;
    const [x1, y1, x2, y2] = t.box;
    const x = ox + x1 * cw;
    const y = oy + y1 * ch;
    const w = (x2 - x1) * cw;
    const hh = (y2 - y1) * ch;
    ctx.save();
    ctx.lineWidth = (isTarget ? 2.5 : 2) * dpr;
    ctx.strokeStyle = '#ffffff';
    ctx.shadowColor = 'rgba(0,0,0,0.65)';
    ctx.shadowBlur = 6 * dpr;
    ctx.setLineDash(isTarget ? [] : [6 * dpr, 4 * dpr]);
    roundRect(ctx, x - 3 * dpr, y - 3 * dpr, w + 6 * dpr, hh + 6 * dpr, 6 * dpr);
    ctx.stroke();
    ctx.restore();
    // Below the box: the server draws its own label above it in the stream.
    label(ctx, isTarget ? 'Following' : 'Click to follow', x - 3 * dpr, y + hh + 3 * dpr, dpr, isTarget, H);
  }
}

function roundRect(ctx, x, y, w, hgt, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + hgt, r);
  ctx.arcTo(x + w, y + hgt, x, y + hgt, r);
  ctx.arcTo(x, y + hgt, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function label(ctx, text, x, yBelow, dpr, strong, canvasH) {
  ctx.save();
  ctx.font = `600 ${12 * dpr}px system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`;
  const tw = ctx.measureText(text).width;
  const ph = 20 * dpr;
  const ly = yBelow + 4 * dpr + ph > canvasH ? yBelow - ph - 6 * dpr : yBelow + 4 * dpr; // flip inside at the bottom edge
  ctx.fillStyle = strong ? 'rgba(22,115,196,0.95)' : 'rgba(10,14,22,0.82)';
  roundRect(ctx, x, ly, tw + 16 * dpr, ph, 10 * dpr);
  ctx.fill();
  ctx.fillStyle = '#ffffff';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, x + 8 * dpr, ly + ph / 2 + 0.5 * dpr);
  ctx.restore();
}

/* ------------------------------------------------------------------ in view now */

let inViewSig = null;

/** Live summary of what the detector sees right now (SSE `tracks`, ~5 Hz). */
function renderInView() {
  const list = (cameraOnline && state.tracks?.tracks) || [];
  const counts = {};
  for (const t of list) counts[t.category] = (counts[t.category] || 0) + 1;
  const keys = PRIORITY.filter((k) => counts[k]);
  const sig = `${cameraOnline}|${keys.map((k) => `${k}${counts[k]}`).join(',')}`;
  if (sig === inViewSig) return; // only touch the DOM when the summary changes
  inViewSig = sig;
  els.inview.classList.toggle('is-active', keys.length > 0);
  els.inviewChips.replaceChildren(...(keys.length
    ? keys.map((k) => catChip(k, counts[k]))
    : [h('span', { class: 'inview-none', text: cameraOnline ? 'Nothing detected' : 'Camera offline' })]));
}

/* ------------------------------------------------------------------ status */

function renderStatus(s) {
  if (!s) return;
  if (!statusKnown) {
    statusKnown = true;
    cameraOnline = s.camera?.online !== false;
    syncStream();
  }
  const cam = s.camera || {};
  const wasOnline = cameraOnline;
  cameraOnline = cam.online !== false;
  els.stage.classList.toggle('is-offline', !cameraOnline);
  els.msg.hidden = cameraOnline;
  if (!cameraOnline) {
    els.msgTitle.textContent = 'Camera offline';
    const age = Number(cam.last_frame_age);
    els.msgText.textContent = Number.isFinite(age) && age > 2
      ? `No picture for ${age < 120 ? `${Math.round(age)} s` : `${Math.round(age / 60)} min`}. Trying to reconnect…`
      : 'Trying to reconnect…';
  }
  renderBadge();
  els.night.hidden = !(cameraOnline && s.lowlight?.active);
  const br = s.brightness;
  const blinded = !!br?.blinded;
  const boosted = !!br?.auto && Number(br.gain) > 1.05;
  els.bright.hidden = !(cameraOnline && (blinded || boosted));
  els.bright.classList.toggle('is-blinded', blinded);
  els.brightText.textContent = blinded ? 'Too bright: camera blinded' : `Brightness \u00d7${Number(br?.gain).toFixed(1)}`;
  els.bright.title = blinded
    ? 'The picture is white: direct light is overloading the camera, and nothing is left to recover. Shade it from the sun.'
    : 'Auto brightness: the washed-out picture is stretched back to full contrast';
  if (cameraOnline !== wasOnline) {
    if (cameraOnline) restartStream(); // camera came back: start a fresh stream
    else syncStream();                 // gone: drop the stream until it returns
  }

  const warn = [];
  if (s.detector && s.detector.online === false && cameraOnline) warn.push(['Detection paused', 'Detector offline']);
  if (s.recorder && s.recorder.online === false && cameraOnline) warn.push(['Recorder offline', 'New events will have no clip']);
  els.warn.replaceChildren(...warn.map(([t, title]) => h('span', { class: 'warn-chip', title }, icon('warning'), t)));

  if (s.today) setCounts(s.today);
  draw();
  renderInView();
}

/* ------------------------------------------------------------------ today counters */

function buildToday() {
  const tile = (key, label, iconName) => h('a', { class: 'stat-tile', href: key ? `#history?cat=${key}` : '#history', 'data-cat': key || 'total' },
    h('span', { class: 'stat-top' },
      h('span', { class: 'stat-icon' }, icon(iconName)),
      h('span', { class: 'stat-num', 'data-num': '', text: '–' })),
    h('span', { class: 'stat-label', text: label }));
  els.today.replaceChildren(...CATS.map((c) => tile(c.key, c.plural, c.key)), tile('', 'Total', 'sigma'));
}

function setCounts(next) {
  if (!next) return;
  const prev = counts;
  counts = { ...next };
  let total = 0;
  let prevTotal = 0;
  for (const c of CATS) {
    const n = Number(next[c.key]) || 0;
    total += n;
    prevTotal += prev ? Number(prev[c.key]) || 0 : 0;
    updateTile(c.key, n, prev ? Number(prev[c.key]) || 0 : null);
  }
  updateTile('total', total, prev ? prevTotal : null);
}

function updateTile(key, n, before) {
  const tile = els.today.querySelector(`[data-cat="${key}"]`);
  if (!tile) return;
  tile.querySelector('[data-num]').textContent = fmtInt(n);
  const name = key === 'total' ? 'total passers-by' : CATS.find((c) => c.key === key).plural.toLowerCase();
  tile.setAttribute('aria-label', `${fmtInt(n)} ${name} today. Show in history.`);
  if (before != null && n > before) {
    tile.classList.remove('pulse');
    void tile.offsetWidth; // restart the animation
    tile.classList.add('pulse');
  }
}

/* ------------------------------------------------------------------ recent activity */

async function loadRecent() {
  if (!recentLoaded) els.recent.replaceChildren(...Array.from({ length: 3 }, () => h('div', { class: 'ev-skel' })));
  try {
    const res = await api.get(`/api/events?limit=${RECENT_N}`);
    recent = (res?.events || []).slice(0, RECENT_N);
    recentLoaded = true;
    renderRecent();
  } catch (err) {
    if (!recentLoaded) els.recent.replaceChildren(errorState(err, loadRecent, "Couldn't load recent activity"));
  }
}

function renderRecent() {
  if (!recent.length) {
    recentCards.clear();
    els.recent.replaceChildren(emptyState('history', 'No activity yet', 'Events appear here as people, vehicles and animals pass by.'));
    return;
  }
  const seen = new Set();
  const nodes = recent.map((ev) => {
    seen.add(ev.id);
    const sig = cardSig(ev, mediaRev(ev.id)) + (evDay(ev) === today() ? '' : '|d');
    const hit = recentCards.get(ev.id);
    if (hit && hit.sig === sig) return hit.el;
    const el = eventCard(ev, { showDay: true, onOpen: openRecent });
    recentCards.set(ev.id, { sig, el });
    return el;
  });
  for (const id of recentCards.keys()) if (!seen.has(id)) recentCards.delete(id);
  keepFocus(els.recent, () => els.recent.replaceChildren(...nodes));
}

function openRecent(ev) {
  const index = recent.findIndex((e) => e.id === ev.id);
  openViewer({ list: recent, index: Math.max(0, index) });
}

function onEventNew(ev) {
  if (!ev || recent.some((e) => e.id === ev.id)) return;
  recent = [ev, ...recent].sort((a, b) => b.start_ts - a.start_ts).slice(0, RECENT_N);
  recentLoaded = true;
  renderRecent();
}

function onEventUpdate(ev) {
  const i = recent.findIndex((e) => e.id === ev.id);
  if (i < 0) return;
  recent[i] = ev;
  renderRecent();
}

function onEventDeleted({ id }) {
  if (!recent.some((e) => e.id === id)) return;
  recent = recent.filter((e) => e.id !== id);
  renderRecent();
  loadRecent(); // backfill to six
}

/* ------------------------------------------------------------------ module API */

export function init() {
  els = {
    stage: document.getElementById('stage'),
    img: document.getElementById('live-img'),
    poster: document.getElementById('live-poster'),
    canvas: document.getElementById('live-canvas'),
    msg: document.getElementById('stage-msg'),
    msgTitle: document.getElementById('stage-msg-title'),
    msgText: document.getElementById('stage-msg-text'),
    badge: document.getElementById('live-badge'),
    badgeText: document.getElementById('live-badge-text'),
    night: document.getElementById('night-badge'),
    bright: document.getElementById('bright-badge'),
    brightText: document.getElementById('bright-badge-text'),
    fps: document.getElementById('live-fps'),
    overlayBtn: document.getElementById('btn-overlay'),
    fsBtn: document.getElementById('btn-fullscreen'),
    stageToast: document.getElementById('stage-toast'),
    warn: document.getElementById('warn-chips'),
    inview: document.getElementById('inview'),
    inviewChips: document.getElementById('inview-chips'),
    today: document.getElementById('today-grid'),
    recent: document.getElementById('recent-grid'),
  };
  els.poster.hidden = true;

  els.img.addEventListener('load', onFrame);
  els.img.addEventListener('error', onStreamError);
  els.stage.addEventListener('click', onStageClick);
  els.stage.addEventListener('pointermove', onStageMove);
  els.stage.addEventListener('pointerleave', () => {
    if (hoverId != null) {
      hoverId = null;
      els.stage.classList.remove('is-pickable');
      draw();
    }
  });

  overlay = store.get('overlay', true) !== false;
  els.overlayBtn.setAttribute('aria-pressed', String(overlay));
  els.overlayBtn.addEventListener('click', () => setOverlay(!overlay));
  els.fsBtn.addEventListener('click', toggleFullscreen);
  document.addEventListener('fullscreenchange', syncFsButton);
  document.addEventListener('webkitfullscreenchange', syncFsButton);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && els.stage.classList.contains('is-pseudo-fs')) setPseudoFs(false);
  });

  document.addEventListener('visibilitychange', syncStream);
  window.addEventListener('pagehide', stopStream);
  window.addEventListener('pageshow', syncStream);
  new ResizeObserver(() => draw()).observe(els.stage);

  buildToday();
  initPtz();

  bus.on('status', renderStatus);
  bus.on('counts', setCounts);
  bus.on('tracks', () => {
    if (!active) return;
    draw();
    renderInView();
  });
  bus.on('event_new', onEventNew);
  bus.on('event_update', onEventUpdate);
  bus.on('event_deleted', onEventDeleted);
  bus.on('viewer', ({ open }) => {
    viewerOpen = open;
    syncStream();
  });
  bus.on('connection', ({ connected, reconnected }) => {
    if (connected && reconnected) {
      restartStream(); // the server may have restarted: the old stream is dead
      loadRecent();
    }
  });

  els.stage.classList.add('is-loading');
  renderBadge();
  renderStatus(state.status);
  loadRecent();
  // If no status arrives (status endpoint broken), try the stream anyway.
  setTimeout(() => {
    if (!statusKnown) {
      statusKnown = true;
      syncStream();
    }
  }, 2500);
}

export function show() {
  active = true;
  syncStream();
  requestAnimationFrame(draw);
}

export function hide() {
  active = false;
  if (els.stage.classList.contains('is-pseudo-fs')) setPseudoFs(false);
  syncStream();
}

export function refresh() {
  restartStream();
  loadRecent();
}
