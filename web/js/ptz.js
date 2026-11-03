// ptz.js: PTZ pad: press-and-hold pan/tilt and zoom (repeated every 400 ms while held,
// stopped on release), presets with a Save mode, auto-follow / auto-zoom switches,
// click-to-follow and the human-readable PTZ status line.
import { api, bus, state, hasEye, trackCamera, tracksFor } from './api.js';
import { CAT, h, icon, toast, store, trackNo } from './ui.js';

const REPEAT_MS = 400;
const VISCA_ZOOM_MAX = 16384;
const HOME_SLOT = 200; // camera preset slot the backend stores Home in (ptz.HOME_SLOT)
const ARROWS = {
  ArrowUp: [0, 1], ArrowDown: [0, -1], ArrowLeft: [-1, 0], ArrowRight: [1, 0],
};

let els = {};
let speed = 0.5;
let saveMode = false;
let busySwitch = { follow: false, zoom: false, focus: false };
let lastErrorToast = 0;

/* ------------------------------------------------------------------ command channels */
// Each channel sends commands one at a time and keeps only the newest pending one, so a
// stop can never be overtaken by an older move still in flight.
function channel() {
  let busy = false;
  let next = null;
  async function pump() {
    busy = true;
    while (next) {
      const { path, body } = next;
      next = null;
      try {
        await api.post(path, body, { timeout: 2500 });
      } catch (err) {
        reportError(err);
      }
    }
    busy = false;
  }
  return (path, body) => {
    next = { path, body };
    if (!busy) pump();
  };
}
const sendDrive = channel();
const sendZoom = channel();

function reportError(err) {
  if (Date.now() - lastErrorToast < 4000) return;
  lastErrorToast = Date.now();
  toast(`Camera control: ${err.message}`, { type: 'error' });
}

/* ------------------------------------------------------------------ pan / tilt */

const drive = { pan: 0, tilt: 0, timer: 0 };

function setDrive(pan, tilt) {
  if (pan === drive.pan && tilt === drive.tilt) return;
  drive.pan = pan;
  drive.tilt = tilt;
  clearInterval(drive.timer);
  drive.timer = 0;
  if (!pan && !tilt) {
    sendDrive('/api/ptz/stop', {});
    return;
  }
  const send = () => sendDrive('/api/ptz/move', { pan: drive.pan, tilt: drive.tilt, speed });
  send();
  drive.timer = setInterval(send, REPEAT_MS);
}

/* ------------------------------------------------------------------ zoom */

const zoom = { dir: null, timer: 0 };

function setZoom(dir) {
  if (dir === zoom.dir) return;
  const was = zoom.dir;
  zoom.dir = dir;
  clearInterval(zoom.timer);
  zoom.timer = 0;
  if (!dir) {
    if (was) sendZoom('/api/ptz/zoom', { dir: 'stop', speed });
    return;
  }
  const send = () => sendZoom('/api/ptz/zoom', { dir: zoom.dir, speed });
  send();
  zoom.timer = setInterval(send, REPEAT_MS);
}

/** Release everything (window blur, page hidden, controls disabled). */
function stopAll() {
  els.dpad?.querySelectorAll('.is-pressed').forEach((b) => b.classList.remove('is-pressed'));
  els.zoomBtns?.forEach((b) => b.classList.remove('is-pressed'));
  keys.clear();
  setDrive(0, 0);
  setZoom(null);
}

/**
 * Press-and-hold behaviour for one button: pointer (mouse, touch, pen) and keyboard
 * (Space / Enter held down). Releasing, leaving the button or cancelling ends the hold.
 */
function bindHold(btn, onStart, onEnd) {
  const end = () => {
    if (!btn.classList.contains('is-pressed')) return;
    btn.classList.remove('is-pressed');
    onEnd();
  };
  btn.addEventListener('pointerdown', (e) => {
    if (btn.disabled || (e.pointerType === 'mouse' && e.button !== 0)) return;
    if (e.pointerType !== 'mouse') e.preventDefault(); // no emulated mouse events / callouts
    // Touch pointers are implicitly captured; release so pointerleave fires when the
    // finger slides off the button (spec: stop when the pointer leaves).
    try { if (btn.hasPointerCapture(e.pointerId)) btn.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
    btn.classList.add('is-pressed');
    onStart();
  });
  for (const type of ['pointerup', 'pointerleave', 'pointercancel']) btn.addEventListener(type, end);
  btn.addEventListener('contextmenu', (e) => e.preventDefault());
  btn.addEventListener('keydown', (e) => {
    if ((e.key === ' ' || e.key === 'Enter') && !e.repeat && !btn.disabled) {
      e.preventDefault();
      btn.classList.add('is-pressed');
      onStart();
    }
  });
  btn.addEventListener('keyup', (e) => {
    if (e.key === ' ' || e.key === 'Enter') {
      e.preventDefault();
      end();
    }
  });
  btn.addEventListener('blur', end);
}

/* ------------------------------------------------------------------ keyboard on the pad */

const keys = new Set();

function applyKeys() {
  let pan = 0;
  let tilt = 0;
  for (const k of keys) {
    pan += ARROWS[k][0];
    tilt += ARROWS[k][1];
  }
  pan = Math.sign(pan);
  tilt = Math.sign(tilt);
  for (const b of els.dpadBtns) {
    b.classList.toggle('is-key', keys.size > 0 && +b.dataset.pan === pan && +b.dataset.tilt === tilt);
  }
  setDrive(pan, tilt);
}

function onPadKey(e) {
  if (ARROWS[e.key]) {
    e.preventDefault();
    if (e.type === 'keydown') {
      if (e.repeat || !usable()) return;
      keys.add(e.key);
    } else {
      keys.delete(e.key);
    }
    applyKeys();
  } else if (e.key === '+' || e.key === '=' || e.key === '-' || e.key === '_') {
    e.preventDefault();
    if (e.type === 'keydown') {
      if (!e.repeat && usable()) setZoom(e.key === '-' || e.key === '_' ? 'out' : 'in');
    } else {
      setZoom(null);
    }
  }
}

/* ------------------------------------------------------------------ status */

function usable() {
  const p = state.status?.ptz;
  return !p || (p.enabled && p.connected);
}

/** "zoom 2.4×" from an optional zoom_ratio, else "zoom 37%" of the VISCA range. */
export function zoomText(p) {
  if (!p) return '';
  if (Number.isFinite(p.zoom_ratio) && p.zoom_ratio > 1.05) return `zoom ${p.zoom_ratio.toFixed(1)}×`;
  if (Number.isFinite(p.zoom) && p.zoom > 0) return `zoom ${Math.max(1, Math.round((p.zoom / VISCA_ZOOM_MAX) * 100))}%`;
  return '';
}

/**
 * One-line PTZ state, e.g. "Following person #12 · zoom 37%" or "Watching (wide)". When the wide camera
 * picks the target and aims the PTZ (ptz.mode "eye"), the line says so.
 */
export function ptzLine(p) {
  if (!p) return 'Connecting…';
  if (!p.enabled) return 'PTZ disabled in settings';
  if (!p.connected) return 'PTZ not connected';
  const z = zoomText(p);
  const tail = z ? ` · ${z}` : '';
  const eyeAims = p.mode === 'eye';
  if (p.autotrack) {
    if (p.paused_for > 0) return `Auto-follow paused while you steer · resumes in ${Math.ceil(p.paused_for)} s`;
    if (p.target_id != null) {
      const t = tracksFor(trackCamera(p.target_id)).find((x) => x.id === p.target_id);
      const what = t ? (CAT[t.category]?.label || t.label || 'object').toLowerCase() : 'object';
      return `Following ${what} #${trackNo(p.target_id)}${eyeAims ? ' · aimed by the wide camera' : ''}${tail}`;
    }
    return eyeAims
      ? `Auto-follow on · the wide camera is looking for a moving object${tail}`
      : `Auto-follow on · waiting for a moving object${tail}`;
  }
  if (p.moving) return `Moving${tail}`;
  if (z) return `Watching${tail}`;
  return hasEye() ? 'Watching (zoomed out)' : 'Watching (wide)'; // "wide" would read as the wide camera
}

function pillState(p) {
  if (!p) return ['PTZ', ''];
  if (!p.enabled) return ['Disabled', 'is-muted'];
  if (!p.connected) return ['Offline', 'is-bad'];
  if (p.autotrack && p.paused_for > 0) return ['Paused', 'is-muted'];
  if (p.autotrack) return ['Following', 'is-accent'];
  if (p.moving) return ['Moving', 'is-accent'];
  return ['Ready', 'is-good'];
}

/** Merge a local change into the shared status so the UI reacts before the next tick. */
function patchPtz(patch) {
  if (state.status?.ptz) Object.assign(state.status.ptz, patch);
  renderStatus(state.status);
}

export function renderStatus(status) {
  const p = status?.ptz;
  const line = ptzLine(p);
  els.lineText.textContent = line;
  els.line.classList.toggle('is-following', !!(p && p.autotrack && p.target_id != null));
  const [text, cls] = pillState(p);
  els.pill.textContent = text;
  els.pill.className = `pill ${cls}`;
  const ok = usable();
  els.card.classList.toggle('is-disabled', !ok);
  for (const b of els.controls) b.disabled = !ok;
  if (!ok) stopAll();
  if (p && !busySwitch.follow) els.follow.checked = !!p.autotrack;
  if (p && !busySwitch.zoom) els.autozoom.checked = !!p.autozoom;
  if (p && !busySwitch.focus) els.autofocus.checked = p.autofocus !== false;
  const custom = Number(p?.home_preset) > 0;
  els.homeHint.textContent = custom ? 'Your saved view' : 'Camera default';
  els.resetHome.hidden = !custom;
}

/* ------------------------------------------------------------------ actions */

/** Lock auto-follow onto a track picked in the live view (either camera's: the ids do not overlap). */
export async function followTrack(track) {
  if (!usable()) {
    toast('Camera control is unavailable right now', { type: 'error' });
    return;
  }
  const what = (CAT[track.category]?.label || track.label || 'object').toLowerCase();
  try {
    const res = await api.post('/api/ptz/autotrack', { enabled: true, track_id: track.id });
    patchPtz({ autotrack: !!res?.autotrack, target_id: res?.target_id ?? track.id });
    toast(`Following ${what} #${trackNo(track.id)}`, { type: 'success' });
  } catch (err) {
    toast(`Couldn't follow that ${what}: ${err.message}`, { type: 'error' });
  }
}

async function onFollowSwitch() {
  const enabled = els.follow.checked;
  busySwitch.follow = true;
  try {
    const res = await api.post('/api/ptz/autotrack', { enabled, track_id: null });
    els.follow.checked = !!res?.autotrack;
    patchPtz({ autotrack: !!res?.autotrack, target_id: res?.target_id ?? null, ...(enabled ? {} : { moving: false }) });
  } catch (err) {
    els.follow.checked = !enabled;
    toast(`Auto-follow: ${err.message}`, { type: 'error' });
  } finally {
    busySwitch.follow = false;
  }
}

async function onZoomSwitch() {
  const value = els.autozoom.checked;
  busySwitch.zoom = true;
  try {
    const res = await api.put('/api/config', { ptz: { autozoom: value } });
    if (res?.config) {
      state.config = res.config;
      bus.emit('config', res.config);
    }
    patchPtz({ autozoom: value });
  } catch (err) {
    els.autozoom.checked = !value;
    toast(`Auto-zoom: ${err.message}`, { type: 'error' });
  } finally {
    busySwitch.zoom = false;
  }
}

async function onFocusSwitch() {
  const value = els.autofocus.checked;
  busySwitch.focus = true;
  try {
    const res = await api.put('/api/config', { ptz: { autofocus: value } });
    if (res?.config) {
      state.config = res.config;
      bus.emit('config', res.config);
    }
    patchPtz({ autofocus: value });
  } catch (err) {
    els.autofocus.checked = !value;
    toast(`Auto-focus: ${err.message}`, { type: 'error' });
  } finally {
    busySwitch.focus = false;
  }
}

async function onPreset(n, btn) {
  try {
    if (saveMode) {
      await api.post('/api/ptz/preset', { action: 'set', preset: n });
      toast(`Saved the current position as preset ${n}`, { type: 'success' });
      setSaveMode(false);
    } else {
      await api.post('/api/ptz/preset', { action: 'call', preset: n });
      flash(btn);
    }
  } catch (err) {
    toast(`Preset ${n}: ${err.message}`, { type: 'error' });
  }
}

function setSaveMode(on) {
  saveMode = on;
  els.saveToggle.setAttribute('aria-pressed', String(on));
  els.presetGrid.classList.toggle('is-saving', on);
  els.presetHint.textContent = on ? 'Tap a number to save here' : 'Tap to go';
  for (const b of els.presetBtns) {
    b.setAttribute('aria-label', `${on ? 'Save current position as' : 'Go to'} preset ${b.dataset.preset}`);
  }
}

function flash(btn) {
  btn.classList.remove('is-flash');
  void btn.offsetWidth; // restart the animation
  btn.classList.add('is-flash');
}

/* ------------------------------------------------------------------ init */

export function initPtz() {
  els = {
    card: document.getElementById('ptz-card'),
    pill: document.getElementById('ptz-pill'),
    dpad: document.getElementById('dpad'),
    home: document.getElementById('btn-home'),
    setHome: document.getElementById('btn-set-home'),
    resetHome: document.getElementById('btn-reset-home'),
    homeHint: document.getElementById('home-hint'),
    speed: document.getElementById('ptz-speed'),
    speedOut: document.getElementById('ptz-speed-out'),
    presetGrid: document.getElementById('preset-grid'),
    presetHint: document.getElementById('preset-hint'),
    follow: document.getElementById('sw-follow'),
    autozoom: document.getElementById('sw-zoom'),
    autofocus: document.getElementById('sw-focus'),
    line: document.getElementById('ptz-line'),
    lineText: document.getElementById('ptz-line-text'),
  };
  els.dpadBtns = [...els.dpad.querySelectorAll('.dpad-btn')];
  els.zoomBtns = [...document.querySelectorAll('.zoom-btn')];

  // Presets 1-9 plus the Save mode toggle in the tenth cell.
  els.presetBtns = [];
  for (let n = 1; n <= 9; n++) {
    const b = h('button', { type: 'button', class: 'preset-btn', 'data-preset': n, 'aria-label': `Go to preset ${n}` }, String(n));
    b.addEventListener('click', () => onPreset(n, b));
    els.presetBtns.push(b);
  }
  els.saveToggle = h('button', { type: 'button', class: 'preset-btn preset-save', 'aria-pressed': 'false', title: 'Save mode: store the current position' },
    icon('pin'), h('span', { text: 'Save' }));
  els.saveToggle.addEventListener('click', () => setSaveMode(!saveMode));
  els.presetGrid.append(...els.presetBtns, els.saveToggle);

  for (const b of els.dpadBtns) {
    bindHold(b, () => setDrive(+b.dataset.pan, +b.dataset.tilt), () => setDrive(0, 0));
  }
  for (const b of els.zoomBtns) {
    bindHold(b, () => setZoom(b.dataset.zoom), () => setZoom(null));
  }
  els.dpad.addEventListener('keydown', onPadKey);
  els.dpad.addEventListener('keyup', onPadKey);
  els.dpad.addEventListener('focusout', (e) => {
    if (!els.dpad.contains(e.relatedTarget) && keys.size) {
      keys.clear();
      applyKeys();
    }
  });
  els.setHome.addEventListener('click', async () => {
    els.setHome.disabled = true;
    try {
      await api.post('/api/ptz/home', { action: 'set' });
      patchPtz({ home_preset: HOME_SLOT, paused_for: 0 });
      toast('Home saved: the camera returns to this view', { type: 'success' });
    } catch (err) {
      toast(`Couldn't save Home: ${err.message}`, { type: 'error' });
    } finally {
      els.setHome.disabled = !usable();
    }
  });
  els.resetHome.addEventListener('click', async () => {
    try {
      await api.post('/api/ptz/home', { action: 'reset' });
      patchPtz({ home_preset: 0 });
      toast("Home is the camera's default position again", { type: 'success' });
    } catch (err) {
      toast(`Couldn't reset Home: ${err.message}`, { type: 'error' });
    }
  });

  els.home.addEventListener('click', async () => {
    try {
      await api.post('/api/ptz/home', {});
      flash(els.home);
    } catch (err) {
      toast(`Home: ${err.message}`, { type: 'error' });
    }
  });

  speed = store.get('ptzSpeed', 0.5);
  els.speed.value = String(speed);
  const syncSpeed = () => {
    speed = Math.min(1, Math.max(0.1, parseFloat(els.speed.value) || 0.5));
    els.speedOut.textContent = `${Math.round(speed * 100)}%`;
    els.speed.style.setProperty('--pct', `${((speed - 0.1) / 0.9) * 100}%`);
  };
  syncSpeed();
  els.speed.addEventListener('input', syncSpeed);
  els.speed.addEventListener('change', () => store.set('ptzSpeed', speed));

  els.follow.addEventListener('change', onFollowSwitch);
  els.autozoom.addEventListener('change', onZoomSwitch);
  els.autofocus.addEventListener('change', onFocusSwitch);

  // Everything that can disable control (for the status-driven disabled state).
  els.controls = [...els.dpadBtns, els.home, ...els.zoomBtns, ...els.presetBtns, els.saveToggle, els.follow, els.autozoom, els.autofocus,
    els.setHome, els.resetHome];

  // Never leave the camera moving: release on blur / hidden page.
  window.addEventListener('blur', stopAll);
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopAll(); });
  window.addEventListener('pagehide', stopAll);

  bus.on('status', renderStatus);
  bus.on('tracks', () => {
    // The followed object's category comes from the tracks feed; refresh the line cheaply.
    const p = state.status?.ptz;
    if (p?.autotrack && p.target_id != null) els.lineText.textContent = ptzLine(p);
  });
  renderStatus(state.status);
}
