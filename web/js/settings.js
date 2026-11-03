// settings.js: Settings tab: config form (only changed fields are sent, as a partial
// PUT /api/config), detection-zone editor, the wide camera and failover cards and the
// live system panel.
import { api, bus, state } from './api.js';
import { CATS, CAT_KEYS, CAM_NAME, h, icon, toast, fmtUptime, fmtNum, fmtTime, fmtDate, tsDay, today, capitalize, setText, emptyState, errorState } from './ui.js';
import { createZoneEditor } from './zone.js';

let els = {};
let loaded = false;
let loading = false;
let saving = false;
let active = false;
let zone = null;
let models = null;
let base = null; // the config the form was filled from: the diff base for "unsaved changes"

/* ------------------------------------------------------------------ path helpers */

function getPath(obj, path) {
  return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
}

function setPath(obj, path, value) {
  const keys = path.split('.');
  let o = obj;
  for (const k of keys.slice(0, -1)) o = o[k] ??= {};
  o[keys[keys.length - 1]] = value;
}

function same(a, b, path) {
  if (typeof a === 'number' && typeof b === 'number') return Math.abs(a - b) < 1e-9;
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b)) return false;
    if (path === 'ptz.autotrack_categories') return [...a].sort().join() === [...b].sort().join(); // order-insensitive
    return JSON.stringify(a) === JSON.stringify(b);
  }
  return a === b;
}

/* ------------------------------------------------------------------ fields */

const fields = () => [...els.form.querySelectorAll('[data-path]')];

const round = (v, d) => Math.round(v * 10 ** d) / 10 ** d;

/** Current value of a form control in config units, or undefined when invalid. */
function readField(el) {
  const type = el.dataset.type;
  if (type === 'bool') return el.checked;
  if (type === 'string') return el.value.trim();
  if (type === 'sens') return round((100 - Number(el.value)) / 100, 2); // sensitivity -> min_score
  const n = Number(el.value);
  if (el.value === '' || !Number.isFinite(n)) return undefined;
  return type === 'int' ? Math.round(n) : round(n, 3);
}

function writeField(el, v) {
  const type = el.dataset.type;
  if (type === 'bool') el.checked = !!v;
  else if (type === 'sens') el.value = String(Math.round(100 - Number(v) * 100));
  else el.value = v == null ? '' : String(v);
  syncOutput(el);
}

function readFollowCats() {
  return CAT_KEYS.filter((k) => els.followCats.querySelector(`input[value="${k}"]`)?.checked);
}

function fmtOut(el) {
  const v = readField(el);
  if (v === undefined) return '–';
  switch (el.dataset.format || el.dataset.type) {
    case 'sec': return `${v} s`;
    case 'pct': return `${Math.round(v * 100)}%`;
    case 'sens': {
      const sens = Number(el.value);
      const word = sens >= 60 ? 'High' : sens >= 40 ? 'Medium' : 'Low';
      return `${word} · ≥${Math.round(v * 100)}%`;
    }
    default:
      if (el.dataset.path === 'detect.min_move') return `${fmtNum(v, 2)}×`;
      if (el.dataset.path === 'ptz.max_zoom_pos') return `${v.toLocaleString()} (${Math.round((v / 16384) * 100)}%)`;
      return String(v);
  }
}

function syncOutput(el) {
  if (el.type === 'range') {
    const min = Number(el.min) || 0;
    const max = Number(el.max) || 100;
    el.style.setProperty('--pct', `${((Number(el.value) - min) / (max - min)) * 100}%`);
  }
  const out = els.form.querySelector(`output[data-out="${el.dataset.path}"]`);
  if (out) out.textContent = fmtOut(el);
  if (el.dataset.path?.endsWith('.enabled')) el.closest('.cat-row')?.classList.toggle('is-off', !el.checked);
}

/* ------------------------------------------------------------------ dirty tracking, save */

/** A setting this server's config does not have (an older backend): never shown, checked or sent. */
const absent = (el) => base != null && getPath(base, el.dataset.path) === undefined;

function collectPatch() {
  const cfg = base || {};
  const patch = {};
  let n = 0;
  for (const el of fields()) {
    const v = readField(el);
    if (v === undefined || absent(el)) continue;
    if (!same(v, getPath(cfg, el.dataset.path), el.dataset.path)) {
      setPath(patch, el.dataset.path, v);
      n += 1;
    }
  }
  const cats = readFollowCats();
  if (!same(cats, getPath(cfg, 'ptz.autotrack_categories') || [], 'ptz.autotrack_categories')) {
    setPath(patch, 'ptz.autotrack_categories', cats);
    n += 1;
  }
  return { patch, n };
}

function validate() {
  for (const el of fields()) {
    if (absent(el)) continue;
    const v = readField(el);
    const label = els.form.querySelector(`label[for="${el.id}"]`)?.textContent || el.getAttribute('aria-label') || el.dataset.path;
    if (el.type === 'number') {
      if (v === undefined) return { el, msg: `${label}: enter a number` };
      if ((el.min !== '' && v < Number(el.min)) || (el.max !== '' && v > Number(el.max))) {
        return { el, msg: `${label}: use a value from ${el.min} to ${el.max}` };
      }
    }
    if (el.dataset.path === 'camera.name' && !v) return { el, msg: 'Give the camera a name' };
    if (el.dataset.path === 'camera.rtsp_url' && !/^rtsps?:\/\//i.test(v)) return { el, msg: 'The stream URL should start with rtsp://' };
    if (el.dataset.path === 'camera.visca_host' && !v) return { el, msg: 'Enter the VISCA host' };
    if (el.dataset.path === 'eye.name' && !v) return { el, msg: 'Give the wide camera a name' };
    if (el.dataset.path === 'eye.device' && !v && els.eyeEnabled.checked) return { el, msg: "Enter the wide camera's USB name, e.g. Avaya HC020" };
  }
  const clip = readField(els.form.querySelector('[data-path="events.clip_seconds"]'));
  const pre = readField(els.form.querySelector('[data-path="events.preroll_seconds"]'));
  if (pre >= clip) return { el: els.form.querySelector('[data-path="events.preroll_seconds"]'), msg: 'Pre-roll must be shorter than the clip' };
  return null;
}

function updateDirty() {
  if (!loaded) return;
  const { n } = collectPatch();
  els.savebar.hidden = n === 0;
  document.body.classList.toggle('has-savebar', n > 0 && active);
  els.savebarText.textContent = n === 1 ? '1 unsaved change' : `${n} unsaved changes`;
}

const isDirty = () => loaded && collectPatch().n > 0;

async function save() {
  if (saving) return;
  const { patch, n } = collectPatch();
  if (!n) return;
  const problem = validate();
  if (problem) {
    toast(problem.msg, { type: 'error' });
    problem.el?.closest('details')?.setAttribute('open', '');
    problem.el?.focus();
    return;
  }
  saving = true;
  els.saveBtn.disabled = true;
  els.saveBtn.textContent = 'Saving…';
  try {
    const res = await api.put('/api/config', patch);
    if (res?.config) {
      state.config = res.config;
      fill(res.config);
      applyZones(res.config); // turning the wide camera on or off changes whose zone can be drawn
      renderLive(state.status);
      bus.emit('config', res.config);
    }
    toast(res?.restarted ? 'Saved. Detection is restarting…' : 'Settings saved', { type: 'success' });
  } catch (err) {
    toast(`Couldn't save: ${err.message}`, { type: 'error', timeout: 9000 });
  } finally {
    saving = false;
    els.saveBtn.disabled = false;
    els.saveBtn.textContent = 'Save';
    updateDirty();
  }
}

/** Save one camera's zone: the PTZ Home view's (detect.zone) or the wide camera's (eye.zone). */
async function saveZone(points, cam = 'ptz') {
  const section = cam === 'eye' ? 'eye' : 'detect';
  try {
    const res = await api.put('/api/config', { [section]: { zone: points } });
    if (res?.config) {
      state.config = res.config;
      bus.emit('config', res.config);
    }
    if (cam === 'eye') toast(points.length ? 'Wide camera zone saved' : 'Wide camera zone cleared: it is automatic again', { type: 'success' });
    else toast(points.length ? 'Detection zone saved' : 'Zone cleared: the whole frame is used', { type: 'success' });
    return res?.config?.[section]?.zone ?? points;
  } catch (err) {
    toast(`Couldn't save the zone: ${err.message}`, { type: 'error', timeout: 9000 });
    return null;
  }
}

/** Both zones and whether the wide camera's can be edited, from a config. */
function applyZones(cfg) {
  zone.setEyeEnabled(!!getPath(cfg, 'eye.enabled'));
  zone.setSaved(getPath(cfg, 'detect.zone') || [], false, 'ptz');
  zone.setSaved(getPath(cfg, 'eye.zone') || [], false, 'eye');
}

/* ------------------------------------------------------------------ fill */

function fill(cfg) {
  if (!cfg) return;
  base = cfg;
  for (const el of fields()) {
    const v = getPath(cfg, el.dataset.path);
    writeField(el, v);
    const holder = el.closest('.field, .switch-row, .range-field');
    if (holder) holder.hidden = v === undefined;
  }
  // A server without the wide camera (an older backend) shows Settings as before.
  els.eyeCard.hidden = cfg.eye === undefined;
  els.failoverCard.hidden = cfg.failover === undefined;
  els.usbHint.hidden = getPath(cfg, 'camera.usb_device') === undefined; // with its fields (an older backend)
  const cats = getPath(cfg, 'ptz.autotrack_categories') || [];
  for (const input of els.followCats.querySelectorAll('input')) input.checked = cats.includes(input.value);
  fillModels(cfg);
  updateDirty();
}

/** A config change made elsewhere: take it for every field the user has not touched. */
function mergeExternal(cfg) {
  if (!base) {
    fill(cfg);
    return;
  }
  for (const el of fields()) {
    const path = el.dataset.path;
    const v = readField(el);
    if (v !== undefined && same(v, getPath(base, path), path)) writeField(el, getPath(cfg, path));
  }
  const catsPath = 'ptz.autotrack_categories';
  if (same(readFollowCats(), getPath(base, catsPath) || [], catsPath)) {
    const cats = getPath(cfg, catsPath) || [];
    for (const input of els.followCats.querySelectorAll('input')) input.checked = cats.includes(input.value);
  }
  base = cfg;
  updateDirty();
}

const MODEL_NAMES = { zero_dce: 'Zero-DCE (recommended)', zero_dce_pp: 'Zero-DCE++ (slower on this HAT)' };

function fillModels(cfg) {
  const label = (p) => {
    const stem = p.split('/').pop().replace(/\.hef$/i, '');
    return MODEL_NAMES[stem] || stem;
  };
  const fillSelect = (select, current, list, extra = []) => {
    const all = [...new Set([...(list || []), current].filter(Boolean))];
    select.replaceChildren(...extra, ...all.map((p) => h('option', { value: p, text: label(p) })));
    select.value = current;
  };
  fillSelect(els.model, getPath(cfg, 'detect.model') || models?.current || '', models?.models);
  fillSelect(els.llModel, getPath(cfg, 'lowlight.model') ?? '', models?.lowlight_models,
    [h('option', { value: '', text: 'None (night vision off)' })]);
}

/** The Night vision card's live line: is it on right now, and how dark is the scene. */
function renderNight(s) {
  const ll = s?.lowlight;
  if (!ll) {
    els.nightPill.textContent = '–';
    els.nightStatus.textContent = '';
    return;
  }
  els.nightPill.textContent = ll.active ? 'On now' : 'Off now';
  els.nightPill.classList.toggle('is-accent', !!ll.active);
  const luma = Number(ll.luma);
  const parts = [];
  if (Number.isFinite(luma)) parts.push(`Scene brightness ${Math.round(luma)} of 255`);
  const onBelow = getPath(base, 'lowlight.night_luma');
  const offAbove = getPath(base, 'lowlight.day_luma');
  if (ll.mode === 'auto' && Number.isFinite(luma) && onBelow != null) {
    parts.push(ll.active ? `turns off above ${Math.round(offAbove)}` : `turns on below ${Math.round(onBelow)}`);
  }
  if (ll.active && Number.isFinite(Number(ll.enhance_ms))) parts.push(`${Math.round(ll.enhance_ms)} ms per frame on the AI HAT`);
  if (!ll.model && ll.mode !== 'off') parts.push('model not loaded');
  if (s.main?.camera === 'eye' && s.cameras?.eye) parts.push('on the wide camera, main right now');
  els.nightStatus.textContent = parts.join(' · ');
}

function renderBright(s) {
  const br = s?.brightness;
  if (!br) {
    els.brightPill.textContent = '–';
    els.brightStatus.textContent = '';
    return;
  }
  const on = !!br.auto && Number(br.gain) > 1.05;
  els.brightPill.textContent = br.blinded ? 'Blinded' : on ? 'On now' : 'Off now';
  els.brightPill.classList.toggle('is-accent', on);
  els.brightPill.classList.toggle('is-warn', !!br.blinded);
  const parts = [];
  if (Number.isFinite(Number(br.white))) parts.push(`${Math.round(br.white)} % of the picture is pure white`);
  if (br.camera === 'eye') parts.push('on the wide camera, main right now'); // these numbers follow the main camera
  if (br.blinded) parts.push('the camera is blinded by direct light: shade it');
  else if (on) parts.push(`stretched \u00d7${Number(br.gain).toFixed(1)}`);
  els.brightStatus.textContent = parts.join(' · ');
}

const setPill = (el, text, cls = '') => {
  el.textContent = text;
  el.className = `pill ${cls}`.trim();
};

const ago = (sec) => (sec < 120 ? `${Math.round(sec)} s` : `${Math.round(sec / 60)} min`);

/** "Nov 1, 2026 at 3:12 PM" (or "today at ..."). */
function whenText(ts) {
  const day = tsDay(ts);
  const date = day === today() ? 'today' : fmtDate(day, { month: 'short', day: 'numeric', year: 'numeric' });
  return `${date} at ${fmtTime(ts)}`;
}

/** The Wide camera card's live line: online, fps, how white its picture is, whether it counts, which camera is main. */
function renderEye(s) {
  const eye = s?.cameras?.eye;
  const parts = [];
  if (eye && eye.online !== false) {
    const isMain = s.main?.camera === 'eye';
    setPill(els.eyePill, isMain ? 'Main camera' : 'Online', isMain ? 'is-accent' : 'is-good');
    parts.push(`Online · ${fmtNum(eye.fps, 0)} fps`);
    if (Number.isFinite(Number(eye.white))) parts.push(`${Math.round(eye.white)} % of the picture white`);
    if (eye.blinded) parts.push('blinded by direct light');
    // Why not, when it does not: off, offline or blinded, no sync or zone yet, or the PTZ's Home not read yet.
    parts.push(s.counting === 'eye' ? 'counting passers-by'
      : s.counting_note ? `the PTZ camera counts: ${s.counting_note}` : 'not counting (the PTZ camera counts)');
    parts.push(isMain ? 'main camera now' : 'the PTZ camera is main');
  } else if (eye) {
    setPill(els.eyePill, 'Offline', 'is-bad');
    const age = Number(eye.last_frame_age);
    parts.push(Number.isFinite(age) && age > 2 ? `No picture for ${ago(age)}` : 'No picture yet');
    parts.push('check the USB cable and the camera name');
  } else if (getPath(base, 'eye.enabled')) {
    setPill(els.eyePill, 'Starting');
    parts.push('Waiting for the wide camera…');
  } else {
    setPill(els.eyePill, 'Off', 'is-muted');
    parts.push('Off');
  }
  setText(els.eyeStatus, parts.join(' · '));
}

/** Camera sync: measured or not, when, and how well it aims. */
function renderSync(s) {
  const sync = s?.sync;
  if (!sync) {
    setPill(els.syncPill, '–');
    setText(els.syncStatus, '');
    return;
  }
  const auto = scheduledText(sync.scheduled);
  if (!sync.calibrated) {
    const waiting = sync.scheduled?.state === 'waiting' || sync.scheduled?.state === 'syncing';
    setPill(els.syncPill, waiting ? 'Scheduled' : 'Not calibrated',
      waiting ? 'is-accent' : getPath(base, 'eye.enabled') ? 'is-warn' : 'is-muted');
    setText(els.syncStatus, `Not measured yet. Until it is, the wide camera counts only in its own zone and can't steer the PTZ.${auto ? ` ${auto}` : ''}`);
    return;
  }
  setPill(els.syncPill, 'Calibrated', 'is-good');
  const parts = [];
  if (auto) parts.push(auto.replace(/\.$/, ''));
  if (Number(sync.created) > 0) parts.push(`Last calibrated ${whenText(sync.created)}`);
  if (Number(sync.positions) > 0) parts.push(`${sync.positions} positions`);
  const r = sync.residuals || {};
  if (Number.isFinite(Number(r.eye_median))) {
    const worst = Number.isFinite(Number(r.eye_max)) ? ` (worst ${fmtNum(r.eye_max * 100, 1)} %)` : '';
    parts.push(`aims within about ${fmtNum(r.eye_median * 100, 1)} % of the picture${worst}`);
  }
  setText(els.syncStatus, parts.join(' · '));
}

/** tools/sync_when_clear.py's progress (status.sync.scheduled), as a sentence; '' when it is not running. */
function scheduledText(p) {
  if (!p) return '';
  if (p.state === 'waiting') {
    const need = Math.round((Number(p.need) || 600) / 60);
    const clear = Math.floor((Number(p.clear_for) || 0) / 60);
    const text = clear > 0
      ? `An automatic sync is waiting: both pictures clear for ${clear} of the ${need} minutes it needs.`
      : `An automatic sync is waiting for both pictures to be clear for ${need} minutes in daylight.`;
    return p.error ? `${text} (Its last try did not finish: ${p.error}; it tries again.)` : text;
  }
  if (p.state === 'syncing') return 'Syncing the cameras now.';
  if (p.state === 'failed') return `The automatic sync did not finish: ${p.error || 'see its log'}.`;
  return '';
}

/** Failover: which camera is main now, since when and why, and how white the PTZ picture is. */
function renderFailover(s) {
  const m = s?.main;
  if (!s?.cameras?.eye || !m) {
    setPill(els.failoverPill, 'Off', 'is-muted');
    setText(els.failoverStatus, 'Needs the wide camera: turn it on in the Wide camera card.');
    return;
  }
  const onEye = m.camera === 'eye';
  // Amber only when failover put the wide camera there, not when it is set to always be main.
  setPill(els.failoverPill, onEye ? 'Wide is main' : 'PTZ is main', !onEye ? 'is-good' : m.mode === 'eye' ? 'is-accent' : 'is-warn');
  const since = Number(m.since) > 0 ? ` since ${fmtTime(m.since)}` : '';
  const why = m.reason && m.reason !== 'PTZ camera' ? ` (${m.reason})` : '';
  const parts = [`${CAM_NAME[onEye ? 'eye' : 'ptz']} is main${since}${why}`];
  const ptz = s.cameras.ptz;
  if (ptz?.online === false) parts.push('PTZ camera offline');
  else if (ptz?.blinded) parts.push('PTZ picture blinded');
  else if (Number.isFinite(Number(ptz?.white))) parts.push(`PTZ picture ${Math.round(ptz.white)} % white`);
  setText(els.failoverStatus, capitalize(parts.join(' · ')));
}

/** Every status-driven line in the cards. */
function renderLive(s) {
  renderNight(s);
  renderBright(s);
  renderEye(s);
  renderSync(s);
  renderFailover(s);
  zone.setCounting(s?.counting);
}

/** Copy the commands in `code`. Plain http on the LAN is not a secure context, so there is a fallback. */
async function copyCommands(code) {
  const text = code.textContent.trim();
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch { /* fall back below */ }
  if (!ok) {
    const range = document.createRange();
    range.selectNodeContents(code);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range); // stays selected, so it can be copied by hand if the browser refuses
    try { ok = document.execCommand('copy'); } catch { ok = false; }
  }
  if (ok) toast('Copied. Paste the commands into a terminal on the Pi.', { type: 'success' });
  else toast('Selected: copy the commands with your keyboard or the context menu');
}

async function load({ quiet = false } = {}) {
  if (loading) return;
  loading = true;
  if (!loaded) {
    els.state.replaceChildren(h('div', { class: 'state-loading' }, h('span', { class: 'spinner' }), 'Loading settings…'));
    els.state.hidden = false;
  }
  try {
    const [cfg, mdl] = await Promise.all([
      api.get('/api/config'),
      api.get('/api/models').catch(() => null),
    ]);
    if (mdl) models = mdl;
    state.config = cfg;
    loaded = true;
    fill(cfg);
    applyZones(cfg);
    renderLive(state.status); // some lines depend on the config (is the wide camera on?)
    els.state.hidden = true;
    els.form.hidden = false;
    if (active) zone.activate();
    bus.emit('config', cfg);
  } catch (err) {
    if (!loaded) {
      els.state.replaceChildren(errorState(err, () => load(), "Couldn't load settings"));
    } else if (!quiet) {
      toast(`Couldn't refresh settings: ${err.message}`, { type: 'error' });
    }
  } finally {
    loading = false;
  }
}

/* ------------------------------------------------------------------ system panel */

function renderSystem(s) {
  if (!s) {
    els.sys.replaceChildren(h('div', { class: 'sys-empty', text: 'Waiting for status…' }));
    return;
  }
  const cam = s.camera || {};
  const det = s.detector || {};
  const rec = s.recorder || {};
  const sys = s.system || {};
  const ptz = s.ptz || {};
  const temp = Number(sys.cpu_temp);
  const tempCls = temp >= 80 ? 'is-bad' : temp >= 70 ? 'is-warn' : '';
  const flag = (ok, yes = 'Online', no = 'Offline') => h('span', { class: `state-dot ${ok ? 'is-good' : 'is-bad'}`, text: ok ? yes : no });
  const item = (iconName, label, value, extra, cls = '') => [
    h('dt', null, icon(iconName), label),
    h('dd', { class: cls }, value, extra || null),
  ];
  const num = (v, d = 1, unit = '') => (Number.isFinite(Number(v)) ? `${fmtNum(v, d)}${unit}` : '–');
  // `camera` follows the main camera; with a wide camera each one gets its own rows.
  const eye = s.cameras?.eye;
  const ptzCam = eye ? { ...cam, ...(s.cameras.ptz || {}) } : cam;
  const eyeRec = eye?.recorder || {};
  els.version.textContent = s.version ? `v${s.version}` : '';
  els.sys.replaceChildren(...[
    item('cpu', 'Model', `${det.model || '–'}${det.device ? ` · ${det.device}` : ''}`, null),
    item('stats', 'Inference', det.online === false ? 'Offline' : `${num(det.fps)} fps · ${num(det.infer_ms)} ms`, det.online === false ? null : null, det.online === false ? 'is-bad' : ''),
    item('live', eye ? 'PTZ camera' : 'Camera', ptzCam.online === false ? '' : `${cam.width || '–'}×${cam.height || '–'} · ${num(ptzCam.fps)} fps `, flag(ptzCam.online !== false)),
    eye ? item('wide', 'Wide camera', eye.online === false ? '' : `${num(eye.fps)} fps `, flag(eye.online !== false)) : null,
    item('video', eye ? 'PTZ recorder' : 'Recorder', rec.online === false ? '' : `${num(rec.buffer_seconds, 0)} s buffered `, flag(rec.online !== false)),
    eye ? item('video', 'Wide recorder', eyeRec.online === false ? '' : `${num(eyeRec.buffer_seconds, 0)} s buffered `, flag(eyeRec.online !== false)) : null,
    item('target', 'PTZ', ptz.enabled === false ? 'Disabled' : '', ptz.enabled === false ? null : flag(!!ptz.connected, 'Connected', 'Not connected')),
    item('thermo', 'CPU temperature', num(temp, 1, ' °C'), null, tempCls),
    item('cpu', 'CPU load', `${num(sys.cpu_percent, 0)}%${Array.isArray(sys.load) ? ` · load ${sys.load.map((x) => fmtNum(x, 2)).join(' ')}` : ''}`, null),
    item('disk', 'Disk free', num(sys.disk_free_gb, 1, ' GB'), null, Number(sys.disk_free_gb) < 5 ? 'is-warn' : ''),
    item('image', 'Media stored', num(sys.media_gb, 2, ' GB'), null),
    item('clock', 'Uptime', fmtUptime(s.uptime), null),
  ].filter(Boolean).flat());
}

/* ------------------------------------------------------------------ build */

function buildCategoryRows() {
  els.cats.replaceChildren(...CATS.map((c) => {
    const en = h('input', {
      type: 'checkbox', role: 'switch', class: 'switch', id: `f-en-${c.key}`,
      'data-path': `detect.categories.${c.key}.enabled`, 'data-type': 'bool', 'aria-label': `Count ${c.plural.toLowerCase()}`,
    });
    const sens = h('input', {
      type: 'range', id: `f-sens-${c.key}`, min: 10, max: 80, step: 5,
      'data-path': `detect.categories.${c.key}.min_score`, 'data-type': 'sens',
      'aria-describedby': 'sens-hint',
    });
    return h('div', { class: 'cat-row', 'data-cat': c.key },
      h('label', { class: 'cat-row-head' },
        h('span', { class: 'cat-ico' }, icon(c.key)),
        h('span', { class: 'cat-row-name', text: c.plural }),
        en),
      h('div', { class: 'range-field compact' },
        h('div', { class: 'field-top' },
          h('label', { for: `f-sens-${c.key}`, text: 'Sensitivity' }),
          h('output', { 'data-out': `detect.categories.${c.key}.min_score` })),
        sens));
  }), h('p', { class: 'hint', id: 'sens-hint', text: 'Higher sensitivity accepts less certain detections (a lower minimum confidence) and may add false alarms.' }));
}

function buildFollowCats() {
  els.followCats.replaceChildren(...CATS.map((c) => h('label', { class: 'check-chip', 'data-cat': c.key },
    h('input', { type: 'checkbox', value: c.key }),
    icon(c.key),
    h('span', { text: c.plural }))));
}

export function init() {
  els = {
    state: document.getElementById('settings-state'),
    form: document.getElementById('settings-form'),
    cats: document.getElementById('cat-settings'),
    followCats: document.getElementById('follow-cats'),
    model: document.getElementById('f-model'),
    savebar: document.getElementById('savebar'),
    savebarText: document.getElementById('savebar-text'),
    saveBtn: document.getElementById('settings-save'),
    discardBtn: document.getElementById('settings-discard'),
    sys: document.getElementById('sys-grid'),
    llModel: document.getElementById('f-ll-model'),
    nightPill: document.getElementById('night-pill'),
    nightStatus: document.getElementById('night-status'),
    brightPill: document.getElementById('bright-pill'),
    brightStatus: document.getElementById('bright-status'),
    eyeCard: document.querySelector('.s-eye'),
    failoverCard: document.querySelector('.s-failover'),
    eyeEnabled: document.getElementById('f-eye-enabled'),
    eyePill: document.getElementById('eye-pill'),
    eyeStatus: document.getElementById('eye-status'),
    syncPill: document.getElementById('sync-pill'),
    syncStatus: document.getElementById('sync-status'),
    syncCmd: document.getElementById('sync-cmd'),
    syncCopy: document.getElementById('sync-copy'),
    syncAutoCmd: document.getElementById('sync-auto-cmd'),
    syncAutoCopy: document.getElementById('sync-auto-copy'),
    usbHint: document.getElementById('usb-hint'),
    failoverPill: document.getElementById('failover-pill'),
    failoverStatus: document.getElementById('failover-status'),
    version: document.getElementById('sys-version'),
  };
  buildCategoryRows();
  buildFollowCats();
  zone = createZoneEditor({ onSave: saveZone });
  els.syncCopy.addEventListener('click', () => copyCommands(els.syncCmd));
  els.syncAutoCopy.addEventListener('click', () => copyCommands(els.syncAutoCmd));

  const onEdit = (e) => {
    const el = e.target;
    if (el.dataset?.path) syncOutput(el);
    updateDirty();
  };
  els.form.addEventListener('input', onEdit);
  els.form.addEventListener('change', (e) => {
    const el = e.target;
    // Clamp typed numbers into range when the field is left.
    if (el.type === 'number' && el.value !== '') {
      const v = Number(el.value);
      if (Number.isFinite(v)) {
        const lo = el.min === '' ? -Infinity : Number(el.min);
        const hi = el.max === '' ? Infinity : Number(el.max);
        const c = Math.min(hi, Math.max(lo, el.dataset.type === 'int' ? Math.round(v) : v));
        if (c !== v) el.value = String(c);
      }
    }
    onEdit(e);
  });
  els.form.addEventListener('submit', (e) => {
    e.preventDefault();
    save();
  });
  els.saveBtn.addEventListener('click', save);
  els.discardBtn.addEventListener('click', () => {
    fill(base || state.config);
    toast('Changes discarded');
  });

  bus.on('status', (s) => {
    if (active || !loaded) renderSystem(s);
    renderLive(s);
  });
  bus.on('main', () => renderLive(state.status));
  bus.on('config', (cfg) => {
    if (!loaded || saving || cfg === base) return;
    // Another control (e.g. the Live auto-zoom switch) changed the config.
    mergeExternal(cfg);
    applyZones(cfg);
  });
  window.addEventListener('beforeunload', (e) => {
    if (isDirty() || zone.isDirty()) {
      e.preventDefault();
      e.returnValue = '';
    }
  });
  renderSystem(state.status);
  renderLive(state.status);
}

export function show() {
  active = true;
  renderSystem(state.status);
  renderLive(state.status);
  if (!loaded) load();
  else {
    if (!isDirty()) load({ quiet: true });
    zone.activate();
  }
  updateDirty();
}

export function hide() {
  active = false;
  document.body.classList.remove('has-savebar');
}

export function refresh() {
  load();
}
