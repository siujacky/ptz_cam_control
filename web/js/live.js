// live.js: Live tab: MJPEG live view (released whenever it is not visible), overlay
// toggle, fullscreen, click-to-follow on tracked boxes, today counters and recent activity.
// With a wide camera ("eye"), the big view shows the main camera and a small picture-in-picture
// the other one; tapping it swaps them (remembered per browser, with a way back to Auto).
import { api, bus, state, mediaRev, hasEye, mainCamera, cameraStatus, tracksFor } from './api.js';
import { CATS, PRIORITY, CAM_NAME, h, icon, catChip, eventCard, cardSig, emptyState, errorState, store, toast, fmtInt, fmtNum, evDay, today, keepFocus, capitalize, setText } from './ui.js';
import { initPtz, followTrack } from './ptz.js';
import { openViewer } from './viewer.js';

const RETRY_MS = 2000;
const RECENT_N = 6;
const PIP_MS = 500;        // the picture-in-picture polls frame.jpg about twice a second: no second MJPEG connection
const PIP_RETRY_MS = 3000;
const CHOICES = ['auto', 'ptz', 'eye'];

let els = {};
let active = false;        // Live tab is shown
let viewerOpen = false;
let overlay = true;
let streaming = false;     // the <img> currently has a stream URL
let streamCam = null;      // the camera parameter of that URL: main, ptz or eye
let gotFrame = false;
let retryTimer = 0;
let cameraOnline = true;   // the camera on the big view
let statusKnown = false;   // the first stream request waits for the first status
let hoverId = null;
let recent = [];
let recentLoaded = false;
let counts = null;
let choice = 'auto';       // big view: auto (the main camera), or a camera the user picked
let shownMain = null;      // the main camera the UI last rendered
let pipCam = null;         // camera in the picture-in-picture
let pipTimer = 0;
let pipBusy = false;
const recentCards = new Map();

/* ------------------------------------------------------------------ which camera is big */

/** The user's pick, which only applies while there is a wide camera. */
const pick = () => (hasEye() ? choice : 'auto');

/** The camera on the big view. */
const shown = () => (pick() === 'auto' ? mainCamera() : pick());

/** The stream follows the main camera by itself (the server switches it seamlessly on a failover). */
const wantStreamCam = () => (pick() === 'auto' ? 'main' : pick());

/** The camera in the picture-in-picture: the other one. */
const otherCam = () => (shown() === 'eye' ? 'ptz' : 'eye');

function setChoice(next) {
  choice = CHOICES.includes(next) ? next : 'auto';
  store.set('liveCamera', choice);
  applyCamera({ posterFromPip: true });
}

/** Tap on the picture-in-picture: its camera goes big. Back on the main camera means back to Auto. */
function swapCameras() {
  const next = otherCam();
  setChoice(next === mainCamera() ? 'auto' : next);
}

/**
 * Re-derive everything that depends on which camera is big: the stream URL, the badges, the boxes and
 * the picture-in-picture. Called on a swap, a main-camera switch and every status.
 */
function applyCamera({ posterFromPip = false } = {}) {
  const s = state.status;
  shownMain = mainCamera();
  const wasOnline = cameraOnline;
  cameraOnline = statusKnown && s ? cameraStatus(shown(), s)?.online !== false : cameraOnline;
  els.stage.dataset.camera = shown();
  renderCamBadge();
  if (s) renderPicture(s);
  if (streaming && streamCam !== wantStreamCam()) restartStream({ posterFromPip });
  else if (cameraOnline !== wasOnline) {
    if (cameraOnline) restartStream(); // camera came back: start a fresh stream
    else syncStream();                 // gone: drop the stream until it returns
  }
  syncPip();
  draw();
  renderInView();
}

/* ------------------------------------------------------------------ MJPEG lifecycle */
// Browsers allow ~6 connections per host and MJPEG holds one for as long as it runs, so
// the stream only runs while the Live tab is on screen and the page is visible.

// While the status says the camera is offline there is nothing to stream; the
// offline -> online transition restarts it (and keeps the console free of 503s).
const wanted = () => active && !document.hidden && !viewerOpen && cameraOnline && statusKnown;

function syncStream() {
  if (wanted()) startStream();
  else stopStream();
  syncPip();
}

function streamUrl(cam, bust) {
  return `/api/live.mjpg?camera=${cam}&overlay=${overlay ? 1 : 0}${bust ? `&_=${Date.now()}` : ''}`;
}

function startStream(bust = false) {
  clearTimeout(retryTimer);
  if (streaming && !bust) return;
  streaming = true;
  streamCam = wantStreamCam();
  setFrame(false);
  els.stage.classList.add('is-loading');
  els.img.src = streamUrl(streamCam, bust);
}

/** Whether the <img> shows real frames (it stays invisible until then: no broken icon). */
function setFrame(on) {
  gotFrame = on;
  els.stage.classList.toggle('has-frame', on);
  renderBadge();
}

function renderBadge() {
  const s = cameraStatus(shown());
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

function restartStream({ posterFromPip = false } = {}) {
  if (!wanted()) {
    if (streaming) stopStream();
    return;
  }
  freezeFrame();
  // On a swap the picture-in-picture already shows the camera that goes big: hold that until the stream starts.
  if (posterFromPip && els.pip.classList.contains('has-frame')) freezeFrame(els.pipImg);
  streaming = false;
  startStream(true);
}

/** Keep the last frame on a canvas so stopping or switching streams never flashes black. */
function freezeFrame(src = els.img) {
  const { poster } = els;
  if (!src.naturalWidth || (src === els.img && !gotFrame)) return;
  try {
    poster.width = src.naturalWidth;
    poster.height = src.naturalHeight;
    poster.getContext('2d').drawImage(src, 0, 0);
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
  if (pipCam) pollPip();
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

/* ------------------------------------------------------------------ picture-in-picture */
// The other camera, small, in a corner of the big view. It polls single frames instead of holding a
// second MJPEG connection (browsers allow ~6 per host) and only runs while the big view would.

const pipWanted = () => hasEye() && active && !document.hidden && !viewerOpen && statusKnown;

function syncPip() {
  els.pip.hidden = !hasEye();
  if (!pipWanted()) {
    clearTimeout(pipTimer); // a frame in flight may land; nothing new is asked for
    pipTimer = 0;
    return;
  }
  const cam = otherCam();
  if (cam !== pipCam) {
    pipCam = cam;
    els.pip.classList.remove('has-frame'); // never show the old camera under the new label
    renderPipLabel();
    pollPip();
  } else if (!pipTimer && !pipBusy) {
    pollPip();
  }
  els.pip.classList.toggle('is-offline', cameraStatus(cam)?.online === false);
}

function renderPipLabel() {
  const name = CAM_NAME[pipCam] || CAM_NAME.eye;
  els.pipLabel.textContent = name;
  els.pip.setAttribute('aria-label', `Show the ${name.toLowerCase()} in the big view`);
  els.pip.title = `Swap: show the ${name.toLowerCase()} big`;
}

/** Frame width for the inset: its size on screen, in 64 px steps so the server can reuse its encodes. */
function pipWidth() {
  const w = els.pip.getBoundingClientRect().width * (window.devicePixelRatio || 1);
  return Math.min(640, Math.max(192, Math.ceil(w / 64) * 64));
}

function pollPip() {
  clearTimeout(pipTimer);
  pipTimer = 0;
  if (!pipWanted() || !pipCam) return;
  pipBusy = true;
  // A new src keeps the current picture on screen until the next one has loaded: no flicker.
  els.pipImg.src = `/api/frame.jpg?camera=${pipCam}&width=${pipWidth()}&overlay=${overlay ? 1 : 0}&_=${Date.now()}`;
}

function onPipLoad() {
  pipBusy = false;
  if (!els.pipImg.src.includes(`camera=${pipCam}&`)) return; // a frame of the camera that was here before
  els.pip.classList.add('has-frame');
  clearTimeout(pipTimer);
  const offline = cameraStatus(pipCam)?.online === false; // the server repeats its "Camera offline" picture
  pipTimer = pipWanted() ? setTimeout(pollPip, offline ? PIP_RETRY_MS : PIP_MS) : 0;
}

function onPipError() {
  if (!els.pipImg.getAttribute('src')) return;
  pipBusy = false;
  clearTimeout(pipTimer);
  pipTimer = pipWanted() ? setTimeout(pollPip, PIP_RETRY_MS) : 0;
}

/* ------------------------------------------------------------------ camera badge */

/**
 * Names the camera on the big view when it is not the PTZ ("Wide camera") and, when failover put it there,
 * why. While the user has picked a camera, an Auto button hands the big view back to the main camera.
 */
function renderCamBadge() {
  const eye = hasEye();
  const cam = shown();
  const pinned = eye && choice !== 'auto';
  const m = state.status?.main;
  const text = CAM_NAME[cam];
  let why = '';
  // Failover (or an offline PTZ) made the wide camera main; "always the wide camera" is a setting, not a reason.
  if (eye && cam === 'eye' && !pinned && m?.camera === 'eye' && m.mode !== 'eye' && m.reason) why = capitalize(m.reason);
  els.camBadge.hidden = !(eye && (cam === 'eye' || pinned));
  els.camBadge.classList.toggle('is-failover', !!why);
  setText(els.camBadgeText, why ? `${text} · ${why}` : text);
  // Announced from a region that is always there (one that appears with its text may not be read out); setText
  // only writes a change, so each status tick does not repeat it.
  setText(els.camAnnounce, !eye ? '' : els.camBadge.hidden ? `${CAM_NAME.ptz} on the big view` : els.camBadgeText.textContent);
  els.camBadge.title = why
    ? `The wide camera took over: ${why.toLowerCase()}. The PTZ camera gets the view back once its picture is clear again.`
    : pinned ? `You picked the ${text.toLowerCase()} for the big view` : 'The wide camera is the main camera';
  els.camAuto.hidden = !pinned;
  if (pinned) {
    const main = CAM_NAME[mainCamera()].toLowerCase();
    els.camAuto.title = `Back to automatic: the big view shows the main camera (now the ${main})`;
  }
  els.stageCam.hidden = els.camBadge.hidden && els.camAuto.hidden;
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

/** The boxes of the camera on the big view. */
const shownTracks = () => (cameraOnline ? tracksFor(shown()) : []);

/** Whether a box on the big view can be followed: the wide camera's only while it aims the PTZ. */
const canFollowShown = () => shown() === 'ptz' || state.status?.ptz?.mode === 'eye';

/** Why a box on the wide camera can't be followed right now. */
function eyeFollowHint() {
  if (state.config?.eye?.follow === false) return 'To follow from the wide camera, turn on "Wide camera steers the PTZ" in Settings';
  if (state.status?.sync && !state.status.sync.calibrated) return 'To follow from the wide camera, calibrate the camera sync first (see Settings)';
  return "The wide camera can't steer the PTZ right now. Tap a box in the PTZ camera's view instead.";
}

/** Smallest tracked box containing the point (small objects win over big ones behind them). */
function pickTrack(x, y) {
  const list = shownTracks();
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
  if (t && !canFollowShown()) toast(eyeFollowHint());
  else if (t) followTrack(t);
  else if (shownTracks().length) showStageToast('Tap directly on a box to follow it');
}

function onStageMove(e) {
  if (e.pointerType !== 'mouse') return;
  const p = normPoint(e);
  const t = gotFrame && canFollowShown() && !e.target.closest('.pip') ? pickTrack(p.x, p.y) : null;
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
  const list = shownTracks();
  if (!list?.length || !gotFrame || !cameraOnline) return;
  const p = state.status?.ptz;
  // While the wide camera aims, its view has the target and the PTZ view the track matched to it.
  const eyeAims = p?.mode === 'eye';
  const targetId = !p?.autotrack ? null : shown() === 'eye' ? (eyeAims ? p.target_id : null) : eyeAims ? p.match_id : p.target_id;
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

/** Live summary of what the detector sees right now on the big view's camera (SSE `tracks`, ~5 Hz). */
function renderInView() {
  const list = shownTracks();
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
    cameraOnline = cameraStatus(shown(), s)?.online !== false;
    syncStream();
  }
  applyCamera(); // the camera on the big view decides the stream, the badges and the boxes

  const warn = [];
  if (s.detector && s.detector.online === false && cameraOnline) warn.push(['Detection paused', 'Detector offline']);
  if (s.recorder && s.recorder.online === false && cameraOnline) warn.push(['Recorder offline', 'New events will have no clip']);
  const eye = s.cameras?.eye;
  if (eye && eye.online === false) warn.push(['Wide camera offline', 'Until it is back, the PTZ camera counts on its Home view']);
  else if (eye?.recorder && eye.recorder.online === false) warn.push(['Wide recorder offline', 'New events will have no wide clip']);
  els.warn.replaceChildren(...warn.map(([t, title]) => h('span', { class: 'warn-chip', title }, icon('warning'), t)));

  if (s.today) setCounts(s.today);
}

/** Offline message, LIVE badge, night and brightness badges: all about the camera on the big view. */
function renderPicture(s) {
  const camName = shown();
  const cam = cameraStatus(camName, s) || {};
  els.stage.classList.toggle('is-offline', !cameraOnline);
  els.msg.hidden = cameraOnline;
  if (!cameraOnline) {
    els.msgTitle.textContent = hasEye() ? `${CAM_NAME[camName]} offline` : 'Camera offline';
    const age = Number(cam.last_frame_age);
    els.msgText.textContent = Number.isFinite(age) && age > 2
      ? `No picture for ${age < 120 ? `${Math.round(age)} s` : `${Math.round(age / 60)} min`}. Trying to reconnect…`
      : 'Trying to reconnect…';
  }
  renderBadge();
  // Night vision and auto brightness run on the main camera; the per-camera flags say what this one shows.
  const br = s.brightness;
  const isMain = !br?.camera || br.camera === camName;
  const night = s.cameras ? !!cam.night : !!s.lowlight?.active;
  els.night.hidden = !(cameraOnline && night);
  const blinded = s.cameras ? !!cam.blinded : !!br?.blinded;
  const boosted = isMain && !!br?.auto && Number(br.gain) > 1.05;
  els.bright.hidden = !(cameraOnline && (blinded || boosted));
  els.bright.classList.toggle('is-blinded', blinded);
  els.brightText.textContent = blinded ? 'Too bright: camera blinded' : `Brightness \u00d7${Number(br?.gain).toFixed(1)}`;
  els.bright.title = blinded
    ? 'The picture is white: direct light is overloading the camera, and nothing is left to recover. Shade it from the sun.'
    : 'Auto brightness: the washed-out picture is stretched back to full contrast';
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
    stageCam: document.getElementById('stage-cam'),
    camBadge: document.getElementById('cam-badge'),
    camBadgeText: document.getElementById('cam-badge-text'),
    camAnnounce: document.getElementById('cam-announce'),
    camAuto: document.getElementById('btn-cam-auto'),
    pip: document.getElementById('pip'),
    pipImg: document.getElementById('pip-img'),
    pipLabel: document.getElementById('pip-label'),
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

  choice = store.get('liveCamera', 'auto');
  if (!CHOICES.includes(choice)) choice = 'auto';
  els.pip.addEventListener('click', swapCameras);
  els.pipImg.addEventListener('load', onPipLoad);
  els.pipImg.addEventListener('error', onPipError);
  els.camAuto.addEventListener('click', () => setChoice('auto'));
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
  bus.on('tracks', (data) => {
    if (!active || (data?.camera && data.camera !== shown())) return; // only the big view's camera has boxes here
    draw();
    renderInView();
  });
  bus.on('main', () => {
    // A failover switched cameras: an Auto stream follows on the server by itself, the rest follows here.
    if (statusKnown && mainCamera() !== shownMain) applyCamera();
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
  renderCamBadge();
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
