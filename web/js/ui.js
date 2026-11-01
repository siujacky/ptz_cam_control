// ui.js: shared UI building blocks: category metadata, inline SVG icons, DOM helpers,
// formatters (always the browser locale), the event card, toasts and a confirm dialog.
import { mediaSrc } from './api.js';

/* ------------------------------------------------------------------ categories */

export const CATS = [
  { key: 'person', label: 'Person', plural: 'People', color: '#4F9DFF' },
  { key: 'vehicle', label: 'Vehicle', plural: 'Vehicles', color: '#FFB020' },
  { key: 'bicycle', label: 'Bicycle', plural: 'Bicycles', color: '#2ED47A' },
  { key: 'motorcycle', label: 'Motorcycle', plural: 'Motorcycles', color: '#FF5C7A' },
  { key: 'animal', label: 'Animal', plural: 'Animals', color: '#B57BFF' },
];
export const CAT_KEYS = CATS.map((c) => c.key);
export const CAT = Object.fromEntries(CATS.map((c) => [c.key, c]));
// top_category priority from the API contract (spec 9.2)
export const PRIORITY = ['person', 'animal', 'motorcycle', 'bicycle', 'vehicle'];

export const DIRS = {
  ltr: { label: 'Left to right', short: 'Left → right', rot: 90 },
  rtl: { label: 'Right to left', short: 'Right → left', rot: 270 },
  approach: { label: 'Approaching', short: 'Approaching', rot: 180 },
  leave: { label: 'Leaving', short: 'Leaving', rot: 0 },
  unknown: { label: 'Direction unknown', short: 'Unknown', rot: null },
};
export const DIR_KEYS = Object.keys(DIRS);

/** "Person", "2 People" */
export function catName(key, n = 1) {
  const c = CAT[key];
  if (!c) return String(key);
  return n > 1 ? `${n} ${c.plural}` : c.label;
}

/** Category keys present in a {cat: n} map, in priority order. */
export function sortCats(counts) {
  return PRIORITY.filter((k) => counts && counts[k] > 0);
}

/** "Person + Bicycle", "2 Vehicles" */
export function eventTitle(ev) {
  const keys = sortCats(ev && ev.categories);
  if (!keys.length) return ev && ev.top_category ? catName(ev.top_category) : 'Event';
  return keys.map((k) => catName(k, ev.categories[k])).join(' + ');
}

export function capitalize(s) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : '';
}

/* ------------------------------------------------------------------ icons */
// Hand-drawn 24x24 stroke icons. Elements that should be filled say so explicitly.
const F = 'fill="currentColor" stroke="none"';
const ICONS = {
  person: '<circle cx="12" cy="7.3" r="3.7"/><path d="M4.6 20.5c.6-4 3.6-6.6 7.4-6.6s6.8 2.6 7.4 6.6"/>',
  vehicle: '<path d="M5.3 11.3 7 6.8a2.2 2.2 0 0 1 2-1.4h6a2.2 2.2 0 0 1 2 1.4l1.7 4.5"/>'
    + '<path d="M3.8 17.3v-4c0-1.1.9-2 2-2h12.4c1.1 0 2 .9 2 2v4z"/>'
    + '<path d="M5.9 17.3v1.6c0 .5.4.9.9.9h1.3c.5 0 .9-.4.9-.9v-1.6M15 17.3v1.6c0 .5.4.9.9.9h1.3c.5 0 .9-.4.9-.9v-1.6"/>'
    + `<circle cx="7.5" cy="14.3" r="1.15" ${F}/><circle cx="16.5" cy="14.3" r="1.15" ${F}/>`,
  bicycle: '<circle cx="5.6" cy="15.8" r="3.6"/><circle cx="18.4" cy="15.8" r="3.6"/>'
    + '<path d="M5.6 15.8 9.4 8.7h6M12 15.8 8.8 6.8M7.4 6.8h2.8M12 15.8H5.6M15.4 8.7 12 15.8M15.4 8.7l3 7.1M15.4 8.7l-.9-2.3h2.4"/>',
  motorcycle: '<circle cx="5.3" cy="16" r="3.3"/><circle cx="18.7" cy="16" r="3.3"/>'
    + '<path d="M5.3 16h5.4l3.6-5.2h-6.6"/><path d="M18.7 16 15.7 7.6h-2.3"/>'
    + '<path d="M10.7 16l1.4-2.2M14.3 10.8h2.8"/>',
  animal: '<circle cx="5.7" cy="10.5" r="1.9"/><circle cx="9.6" cy="6.3" r="1.9"/><circle cx="14.4" cy="6.3" r="1.9"/><circle cx="18.3" cy="10.5" r="1.9"/>'
    + '<path d="M12 11.7c-2.7 0-5.6 3.4-5.6 5.9 0 1.5 1.1 2.4 2.4 2.4 1.2 0 2.1-.7 3.2-.7s2 .7 3.2.7c1.3 0 2.4-.9 2.4-2.4 0-2.5-2.9-5.9-5.6-5.9z"/>',

  lens: '<circle cx="12" cy="12" r="8.6"/><circle cx="12" cy="12" r="4.2"/>' + `<circle cx="13.3" cy="10.7" r="1.2" ${F}/>`,
  live: '<rect x="2.8" y="6.4" width="13.2" height="11.2" rx="2.6"/><path d="m16 10.3 4.4-2.6c.4-.2.8.1.8.5v7.6c0 .4-.4.7-.8.5L16 13.7"/>',
  video: '<rect x="2.8" y="6.4" width="13.2" height="11.2" rx="2.6"/><path d="m16 10.3 4.4-2.6c.4-.2.8.1.8.5v7.6c0 .4-.4.7-.8.5L16 13.7"/>',
  history: '<path d="M3.6 12a8.4 8.4 0 1 0 2.5-6"/><path d="M3.6 4.4v4.3h4.3"/><path d="M12 7.6V12l3 2"/>',
  stats: '<path d="M4 20h16"/><rect x="5.2" y="11" width="3.4" height="6.4" rx="1"/><rect x="10.3" y="5.5" width="3.4" height="11.9" rx="1"/><rect x="15.4" y="13.4" width="3.4" height="4" rx="1"/>',
  settings: '<path d="M4 7.5h9M17.5 7.5H20M4 16.5h2.5M11 16.5h9"/><circle cx="15.2" cy="7.5" r="2.3"/><circle cx="8.8" cy="16.5" r="2.3"/>',
  'chevron-left': '<path d="m14.5 6-6 6 6 6"/>',
  'chevron-right': '<path d="m9.5 6 6 6-6 6"/>',
  'chevron-down': '<path d="m6 9.5 6 6 6-6"/>',
  arrow: '<path d="M12 19V5.5M6.5 11 12 5.5l5.5 5.5"/>',
  home: '<path d="M4 11 12 4.5l8 6.5"/><path d="M6.3 9.2v10.3h4.2v-5.2h3v5.2h4.2V9.2"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  zoom: '<circle cx="10.5" cy="10.5" r="6.3"/><path d="m15.3 15.3 4.9 4.9"/>',
  expand: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  collapse: '<path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"/>',
  boxes: '<path d="M3.5 8V5.6c0-1.2.9-2.1 2.1-2.1H8M16 3.5h2.4c1.2 0 2.1.9 2.1 2.1V8M20.5 16v2.4c0 1.2-.9 2.1-2.1 2.1H16M8 20.5H5.6c-1.2 0-2.1-.9-2.1-2.1V16"/><rect x="8" y="8" width="8" height="8" rx="1.6"/>',
  close: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
  download: '<path d="M12 4v11M7 10.5l5 5 5-5M5 19.5h14"/>',
  trash: '<path d="M4.5 7h15M9.5 7V5h5v2M6.5 7l.9 12.1c.1 1 .9 1.9 2 1.9h5.2c1.1 0 1.9-.8 2-1.9L17.5 7M10 11v6M14 11v6"/>',
  play: `<path d="M8.5 5.6v12.8c0 .6.7 1 1.2.7l10-6.4c.5-.3.5-1.1 0-1.4l-10-6.4c-.5-.3-1.2.1-1.2.7z" ${F}/>`,
  image: '<rect x="3.5" y="5" width="17" height="14" rx="2.6"/><circle cx="9" cy="10" r="1.6"/><path d="m4 17.5 5-4.5 3.5 3 3-2.5 4.5 4"/>',
  target: '<circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2.4"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3"/>',
  refresh: '<path d="M19.8 11.2A8 8 0 0 0 6.2 6.4L4 8.6M4 4v4.6h4.6M4.2 12.8a8 8 0 0 0 13.6 4.8l2.2-2.2M20 20v-4.6h-4.6"/>',
  warning: '<path d="M10.3 4.6 3 17.6c-.8 1.4.2 3 1.7 3h14.6c1.5 0 2.5-1.6 1.7-3L13.7 4.6c-.8-1.4-2.6-1.4-3.4 0z"/><path d="M12 9.6v4.6"/>' + `<circle cx="12" cy="17.2" r="1.1" ${F}/>`,
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  info: '<circle cx="12" cy="12" r="8.6"/><path d="M12 11v5.6"/>' + `<circle cx="12" cy="7.8" r="1.1" ${F}/>`,
  'camera-off': '<path d="M3.5 3.5l17 17"/><path d="M9.6 6.4h3.8c1.4 0 2.6 1.2 2.6 2.6v3.8m0 3.8c-.4.6-1.1 1-1.9 1H5.4c-1.4 0-2.6-1.2-2.6-2.6V9c0-1 .6-1.9 1.4-2.3"/><path d="m16 10.3 4.4-2.6c.4-.2.8.1.8.5v7.3"/>',
  offline: '<path d="M3.5 3.5l17 17"/><path d="M8.9 8.3A11 11 0 0 1 21 9.6M2.8 9.6a11 11 0 0 1 2.8-1.9M6 13a6.8 6.8 0 0 1 3.4-1.9M14.5 11.2A6.8 6.8 0 0 1 18 13M9.2 16.3a4 4 0 0 1 5.6 0"/>' + `<circle cx="12" cy="19.3" r="1.1" ${F}/>`,
  undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
  clear: '<circle cx="12" cy="12" r="8.6"/><path d="m9 9 6 6M15 9l-6 6"/>',
  pin: '<path d="M7 4h10M8.5 4v5.2L6 13.5h12l-2.5-4.3V4M12 13.5V20"/>',
  dot: `<circle cx="12" cy="12" r="3" ${F}/>`,
  cpu: '<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10 3.5V7M14 3.5V7M10 17v3.5M14 17v3.5M3.5 10H7M3.5 14H7M17 10h3.5M17 14h3.5"/>',
  thermo: '<path d="M14 14.6V5.2a2 2 0 0 0-4 0v9.4a4 4 0 1 0 4 0z"/><path d="M12 10v7"/>',
  disk: '<ellipse cx="12" cy="6" rx="7.5" ry="2.8"/><path d="M4.5 6v12c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8V6M4.5 12c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8"/>',
  clock: '<circle cx="12" cy="12" r="8.6"/><path d="M12 7.5V12l3 2"/>',
  'trend-up': '<path d="M5 15.5 10 10.5l3 3 6-6M14.5 7.5H19V12"/>',
  'trend-down': '<path d="M5 8.5l5 5 3-3 6 6M14.5 16.5H19V12"/>',
  sigma: '<path d="M17.5 5.5h-11l6 6.5-6 6.5h11"/>',
  calendar: '<rect x="3.5" y="5" width="17" height="15.5" rx="2.6"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
  moon: '<path d="M19.6 14.7A8.2 8.2 0 0 1 9.3 4.4a8.2 8.2 0 1 0 10.3 10.3z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
};

const SVG_NS = 'http://www.w3.org/2000/svg';

/** An inline SVG icon element (decorative: aria-hidden). */
export function icon(name, cls = '') {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', `icon icon-${name}${cls ? ` ${cls}` : ''}`);
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.innerHTML = ICONS[name] || ICONS.dot; // static markup from the table above, never user data
  return svg;
}

/** Replace every <i data-icon="name"> placeholder in the static HTML. */
export function hydrateIcons(root = document) {
  root.querySelectorAll('i[data-icon]').forEach((el) => {
    el.replaceWith(icon(el.dataset.icon, el.className));
  });
}

/* ------------------------------------------------------------------ DOM helpers */

/** Tiny element builder. Text goes through textContent, never innerHTML. */
export function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'style') Object.assign(el.style, v);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : String(v));
    }
  }
  appendKids(el, kids);
  return el;
}

/** SVG element builder (attributes only). */
export function svgEl(tag, attrs, ...kids) {
  const el = document.createElementNS(SVG_NS, tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, String(v));
  appendKids(el, kids);
  return el;
}

function appendKids(el, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
}

export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/**
 * Run a re-render of `container` without losing keyboard focus: if focus was inside it,
 * put it back on the same element, or on the new element with the same data-key/data-id.
 */
export function keepFocus(container, render) {
  const active = document.activeElement;
  const had = active && active !== document.body && container.contains(active);
  const key = had ? active.dataset?.id ?? active.dataset?.key : undefined;
  render();
  if (!had || document.activeElement === active) return;
  let target = active.isConnected && container.contains(active) ? active : null;
  if (!target && key !== undefined) {
    target = [...container.querySelectorAll('[data-id], [data-key]')]
      .find((el) => (el.dataset.id ?? el.dataset.key) === key) || null;
  }
  if (target) target.focus({ preventScroll: true });
}

/** Category chip: colored icon + text (the text never wears the category color). */
export function catChip(key, n = 1, { short = false } = {}) {
  return h('span', { class: 'cat-chip', 'data-cat': key },
    icon(key),
    h('span', { text: short ? (n > 1 ? `${n}` : CAT[key]?.label || key) : catName(key, n) }));
}

/** Empty / error states shared by every tab. */
export function emptyState(iconName, title, text, action) {
  return h('div', { class: 'empty' },
    h('span', { class: 'empty-icon' }, icon(iconName)),
    h('strong', { text: title }),
    text ? h('span', { text }) : null,
    action || null);
}

export function errorState(err, onRetry, title = "Couldn't load this") {
  return emptyState('warning', title, err && err.message ? err.message : String(err || ''),
    onRetry ? h('button', { type: 'button', class: 'btn btn-sm', onclick: onRetry }, icon('refresh'), 'Try again') : null);
}

/* ------------------------------------------------------------------ event card */

/** Ring-style event card used by Live (recent) and History. */
export function eventCard(ev, { onOpen, showDay = false } = {}) {
  const keys = sortCats(ev.categories);
  const day = showDay ? tsDay(ev.start_ts) : null;
  const when = fmtTime(ev.start_ts) + (day && day !== today() ? ` · ${dayLabel(day)}` : '');
  const status = ev.clip_status === 'pending' ? 'clip processing' : ev.clip_status === 'failed' ? 'clip unavailable' : '';
  const card = h('button', {
    type: 'button',
    class: `ev-card is-${ev.clip_status || 'ready'}`,
    'data-id': ev.id,
    'data-cat': ev.top_category,
    'aria-label': `${eventTitle(ev)} at ${when}${status ? `, ${status}` : ''}`,
  });
  const img = h('img', { src: mediaSrc(ev.thumb_url, ev.id), alt: '', loading: 'lazy', decoding: 'async', width: 384, height: 216 });
  const thumb = h('span', { class: 'ev-thumb' }, h('span', { class: 'ev-ph' }, icon(ev.top_category || 'image')), img);
  img.addEventListener('error', () => thumb.classList.add('no-img'), { once: true });
  img.addEventListener('load', () => thumb.classList.add('has-img'), { once: true });
  if (ev.clip_status === 'pending') {
    thumb.append(h('span', { class: 'ev-badge pending' }, h('span', { class: 'spinner xs' }), 'Processing clip…'));
  } else if (ev.clip_status === 'failed') {
    thumb.append(h('span', { class: 'ev-badge failed' }, icon('warning'), 'No clip'));
  } else {
    thumb.append(h('span', { class: 'ev-badge' }, icon('play'), fmtDuration(ev.duration)));
  }
  if (ev.night) thumb.append(h('span', { class: 'ev-night', title: 'Night vision' }, icon('moon')));
  card.append(thumb, h('span', { class: 'ev-body' },
    h('span', { class: 'ev-time', text: when }),
    h('span', { class: 'ev-chips' }, keys.map((k) => catChip(k, ev.categories[k])))));
  if (onOpen) card.addEventListener('click', () => onOpen(ev));
  return card;
}

/** Signature of everything an event card shows; a card is rebuilt when it changes. */
export function cardSig(ev, rev = 0) {
  return `${ev.clip_status}|${ev.thumb_url}|${rev}|${JSON.stringify(ev.categories)}|${Math.round(ev.duration || 0)}|${ev.night ? 1 : 0}`;
}

/* ------------------------------------------------------------------ dates and numbers */

const pad = (n) => String(n).padStart(2, '0');

/** Local YYYY-MM-DD (the API's day format). */
export function dayStr(d = new Date()) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}
export const today = () => dayStr();
export function parseDay(s) {
  const [y, m, d] = String(s).split('-').map(Number);
  return new Date(y, (m || 1) - 1, d || 1);
}
export function addDays(s, n) {
  const d = parseDay(s);
  d.setDate(d.getDate() + n);
  return dayStr(d);
}
export const dayStartTs = (s) => parseDay(s).getTime() / 1000;
export const tsDay = (ts) => dayStr(new Date(ts * 1000));

/** The day an event belongs to: explicit field, else its media path, else its start time. */
export function evDay(ev) {
  if (ev.day) return ev.day;
  const m = /\/media\/(\d{4}-\d{2}-\d{2})\//.exec(ev.snapshot_url || ev.thumb_url || '');
  return m ? m[1] : tsDay(ev.start_ts);
}

export function fmtTime(ts, { seconds = false } = {}) {
  const opts = seconds ? { hour: 'numeric', minute: '2-digit', second: '2-digit' } : { hour: 'numeric', minute: '2-digit' };
  return new Date(ts * 1000).toLocaleTimeString([], opts);
}
export function fmtDate(s, opts = { weekday: 'short', month: 'short', day: 'numeric' }) {
  return parseDay(s).toLocaleDateString([], opts);
}
export function dayLabel(s) {
  const t = today();
  if (s === t) return 'Today';
  if (s === addDays(t, -1)) return 'Yesterday';
  return fmtDate(s);
}
/** Hour of day in the locale's style: "5 PM" or "17". */
export function fmtHour(hour) {
  return new Date(2000, 0, 1, hour).toLocaleTimeString([], { hour: 'numeric' });
}
/** "0:30", "1:05" */
export function fmtDuration(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  return `${Math.floor(s / 60)}:${pad(s % 60)}`;
}
export function fmtUptime(sec) {
  let s = Math.max(0, Math.floor(Number(sec) || 0));
  const d = Math.floor(s / 86400); s -= d * 86400;
  const hr = Math.floor(s / 3600); s -= hr * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  if (d) return `${d}d ${hr}h`;
  if (hr) return `${hr}h ${m}m`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}
export const fmtInt = (n) => (Math.round(Number(n) || 0)).toLocaleString();
export function fmtNum(n, digits = 1) {
  return Number.isFinite(Number(n)) ? Number(n).toLocaleString([], { maximumFractionDigits: digits, minimumFractionDigits: digits }) : '–';
}

/* ------------------------------------------------------------------ storage */

/** localStorage that never throws (private mode, blocked storage). */
export const store = {
  get(key, fallback) {
    try {
      const v = localStorage.getItem(`ptzcam.${key}`);
      return v == null ? fallback : JSON.parse(v);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try { localStorage.setItem(`ptzcam.${key}`, JSON.stringify(value)); } catch { /* ignore */ }
  },
};

/* ------------------------------------------------------------------ toasts */

const TOAST_ICON = { info: 'info', success: 'check', error: 'warning' };

export function toast(message, { type = 'info', timeout } = {}) {
  const host = document.getElementById('toasts');
  if (!host) return;
  const el = h('div', { class: `toast toast-${type}`, role: type === 'error' ? 'alert' : 'status' },
    icon(TOAST_ICON[type] || 'info', 'toast-icon'),
    h('span', { class: 'toast-msg', text: message }));
  const close = () => {
    clearTimeout(timer);
    el.classList.add('is-leaving');
    setTimeout(() => el.remove(), 200);
  };
  el.append(h('button', { type: 'button', class: 'toast-close', 'aria-label': 'Dismiss', onclick: close }, icon('close')));
  host.append(el);
  while (host.children.length > 3) host.firstElementChild.remove();
  const timer = setTimeout(close, timeout || (type === 'error' ? 6500 : 3200));
}

/* ------------------------------------------------------------------ confirm dialog */

export function confirmDialog({ title, message, ok = 'OK', danger = false }) {
  const dlg = document.getElementById('confirm');
  document.getElementById('confirm-title').textContent = title;
  document.getElementById('confirm-msg').textContent = message || '';
  const okBtn = document.getElementById('confirm-ok');
  okBtn.textContent = ok;
  okBtn.className = `btn ${danger ? 'btn-danger-solid' : 'btn-primary'}`;
  dlg.returnValue = '';
  dlg.showModal();
  document.getElementById('confirm-cancel').focus();
  return new Promise((resolve) => {
    dlg.addEventListener('close', () => resolve(dlg.returnValue === 'ok'), { once: true });
  });
}
