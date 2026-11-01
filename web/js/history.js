// history.js: History tab: day navigator, category filter chips, a zoomable 24 h timeline
// and event cards grouped by hour, with "Load more" paging and live SSE updates.
import { api, bus, mediaRev } from './api.js';
import {
  CATS, CAT, h, icon, eventCard, cardSig, catChip, eventTitle, sortCats, emptyState, errorState,
  fmtTime, fmtHour, fmtDate, fmtInt, dayLabel, today, addDays, dayStartTs, evDay, clamp, toast, keepFocus,
} from './ui.js';
import { openViewer } from './viewer.js';

const PAGE = 50;          // cards per page
const TL_PAGE = 200;      // timeline page size (the backend caps limit at 200); paged with `before`
const TL_MAX_PAGES = 10;  // up to 2000 events a day on the timeline
const ZOOMS = [1, 2, 4, 8];

let els = {};
let loadedOnce = false;
let active = false;
let day = today();
let category = '';        // '' = all categories
let days = [];            // GET /api/days
let events = [];          // loaded card pages, newest first
let hasMore = false;
let tlEvents = [];        // every event of the day (timeline), newest first
let tlZoom = 0;           // index into ZOOMS
let seq = 0;              // invalidates responses for a previous day/filter
let loadingMore = false;
let daysTimer = 0;
let lastToday = today();
const cards = new Map();  // event id -> { sig, el }

const matches = (ev) => evDay(ev) === day && (!category || (ev.categories && ev.categories[category] > 0));
const byNewest = (a, b) => b.start_ts - a.start_ts;
/** Union by id (first wins), newest first. */
function mergeEvents(...lists) {
  const seen = new Map();
  for (const list of lists) for (const e of list) if (!seen.has(e.id)) seen.set(e.id, e);
  return [...seen.values()].sort(byNewest);
}

/* ------------------------------------------------------------------ data */

async function loadDays() {
  try {
    const res = await api.get('/api/days?limit=60');
    days = res?.days || [];
  } catch {
    /* the navigator still works day by day without it */
  }
  renderNav();
  renderChips();
}

function refreshDaysSoon() {
  clearTimeout(daysTimer);
  daysTimer = setTimeout(loadDays, 1500);
}

function query(extra) {
  const q = new URLSearchParams({ day, ...extra });
  if (category) q.set('category', category);
  return `/api/events?${q}`;
}

async function loadDay() {
  const my = ++seq;
  cards.clear();
  events = [];
  tlEvents = [];
  hasMore = false;
  renderNav();
  renderChips();
  renderTimeline();
  els.list.replaceChildren(h('div', { class: 'ev-grid' }, Array.from({ length: 6 }, () => h('div', { class: 'ev-skel' }))));
  els.more.hidden = true;
  try {
    const res = await api.get(query({ limit: PAGE }));
    if (my !== seq) return;
    // `events` was emptied when the load started: whatever is in it now arrived over SSE.
    events = mergeEvents(res?.events || [], events);
    hasMore = !!res?.has_more;
    renderList();
    if (!hasMore) {
      tlEvents = mergeEvents(events, tlEvents);
      renderTimeline(true);
    } else {
      loadTimeline(my);
    }
  } catch (err) {
    if (my !== seq) return;
    els.list.replaceChildren(errorState(err, loadDay, "Couldn't load events"));
    renderTimeline();
  }
}

/** The timeline wants the whole day, not just the first page of cards. */
async function loadTimeline(my) {
  let all = [];
  let before = null;
  try {
    for (let page = 0; page < TL_MAX_PAGES; page++) {
      const res = await api.get(query(before == null ? { limit: TL_PAGE } : { limit: TL_PAGE, before }));
      if (my !== seq) return;
      const got = res?.events || [];
      all = all.concat(got);
      if (!res?.has_more || !got.length) break;
      before = got[got.length - 1].start_ts;
    }
  } catch {
    if (my !== seq) return;
    if (!all.length) all = events.slice(); // fall back to what the cards have
  }
  tlEvents = mergeEvents(all, tlEvents); // keep events that arrived over SSE meanwhile
  renderTimeline(true);
}

async function loadMore() {
  if (loadingMore || !hasMore || !events.length) return [];
  loadingMore = true;
  const my = seq;
  els.more.disabled = true;
  els.more.textContent = 'Loading…';
  try {
    const res = await api.get(query({ limit: PAGE, before: events[events.length - 1].start_ts }));
    if (my !== seq) return [];
    const known = new Set(events.map((e) => e.id));
    const fresh = (res?.events || []).filter((e) => !known.has(e.id));
    events = events.concat(fresh).sort(byNewest);
    hasMore = !!res?.has_more && fresh.length > 0;
    renderList();
    return fresh;
  } catch (err) {
    toast(`Couldn't load more events: ${err.message}`, { type: 'error' });
    throw err;
  } finally {
    loadingMore = false;
    els.more.disabled = false;
    els.more.textContent = 'Load more';
  }
}

/* ------------------------------------------------------------------ day navigator */

function navDays() {
  const set = new Set(days.map((d) => d.day));
  set.add(today());
  return [...set].sort();
}

function renderNav() {
  const t = today();
  const all = navDays();
  const prev = [...all].reverse().find((d) => d < day);
  const next = all.find((d) => d > day && d <= t);
  els.prev.disabled = !prev;
  els.next.disabled = !next && day >= t;
  els.prev.dataset.day = prev || '';
  els.next.dataset.day = next || (day < t ? t : '');
  els.todayBtn.hidden = day === t;
  const label = dayLabel(day);
  els.dayMain.textContent = label;
  els.daySub.textContent = label === 'Today' || label === 'Yesterday'
    ? fmtDate(day, { weekday: 'short', month: 'short', day: 'numeric' })
    : fmtDate(day, { year: 'numeric' });
  els.dayBtn.setAttribute('aria-label', `${fmtDate(day, { weekday: 'long', month: 'long', day: 'numeric' })}. Choose another day`);
  els.dayInput.max = t;
  els.dayInput.min = days.length ? days[days.length - 1].day : addDays(t, -365);
  els.dayInput.value = day;
}

function setDay(next) {
  if (!next || next === day) return;
  day = next > today() ? today() : next;
  loadDay();
}

function setCategory(key) {
  if (key === category) return;
  category = key;
  loadDay();
}

/** Chips are built once and updated in place, so live count updates never swallow a tap. */
function renderChips() {
  if (!els.chips.children.length) {
    const chip = (key, label) => h('button', { type: 'button', class: 'chip', 'data-cat': key || null, 'data-key': key },
      key ? icon(key) : null, h('span', { text: label }), h('span', { class: 'chip-count', text: '0' }));
    els.chips.append(chip('', 'All'), ...CATS.map((c) => chip(c.key, c.plural)));
  }
  const info = days.find((d) => d.day === day);
  const counts = info?.counts || {};
  for (const b of els.chips.children) {
    const key = b.dataset.key;
    const n = key ? counts[key] || 0 : info?.events ?? 0;
    b.setAttribute('aria-pressed', String(category === key));
    b.querySelector('.chip-count').textContent = fmtInt(n);
  }
}

/* ------------------------------------------------------------------ timeline */

function renderTimeline(scrollToContent = false) {
  const start = dayStartTs(day);
  const end = dayStartTs(addDays(day, 1)); // 23 or 25 h on DST days
  const span = end - start;
  const isToday = day === today();
  const marks = [...tlEvents].sort((a, b) => a.start_ts - b.start_ts).map((ev, i, arr) => {
    const m = h('button', {
      type: 'button',
      class: 'tl-mark',
      'data-cat': ev.top_category,
      'data-id': ev.id,
      tabindex: i === arr.length - 1 ? '0' : '-1',
      'aria-label': `${eventTitle(ev)} at ${fmtTime(ev.start_ts)}`,
    });
    m.style.left = `${clamp(((ev.start_ts - start) / span) * 100, 0, 100)}%`;
    return m;
  });
  const ticks = [];
  for (let hr = 1; hr < 24; hr++) {
    const t = h('span', { class: `tl-tick${hr % 6 === 0 ? ' major' : ''}` });
    t.style.left = `${(hr / 24) * 100}%`;
    ticks.push(t);
  }
  const nodes = [...ticks, ...marks];
  if (isToday) {
    const pct = clamp(((Date.now() / 1000 - start) / span) * 100, 0, 100);
    const now = h('span', { class: 'tl-now' }, h('span', { class: 'tl-now-label', text: 'Now' }));
    now.style.left = `${pct}%`;
    nodes.push(now);
  }
  keepFocus(els.tlBar, () => els.tlBar.replaceChildren(...nodes));
  const focused = els.tlBar.querySelector('.tl-mark:focus');
  if (focused) { // the roving tabindex follows the focused marker
    for (const m of els.tlBar.querySelectorAll('.tl-mark[tabindex="0"]')) m.tabIndex = -1;
    focused.tabIndex = 0;
  }
  els.tlBar.setAttribute('aria-label', `Timeline for ${dayLabel(day)}: ${tlEvents.length} events. Use the arrow keys to move between events.`);
  els.tlSub.textContent = tlEvents.length ? `${fmtInt(tlEvents.length)} event${tlEvents.length === 1 ? '' : 's'}` : 'No events';
  renderHours();
  if (scrollToContent) scrollTimelineToContent();
}

function renderHours() {
  const width = els.tlTrack.clientWidth || els.tlScroll.clientWidth || 320;
  const pxPerHour = width / 24;
  const step = pxPerHour >= 52 ? 1 : pxPerHour >= 28 ? 2 : pxPerHour >= 18 ? 3 : 6;
  const labels = [];
  for (let hr = 0; hr < 24; hr += step) {
    const l = h('span', { class: 'tl-hour', text: fmtHour(hr) });
    l.style.left = `${(hr / 24) * 100}%`;
    if (hr === 0) l.classList.add('first');
    labels.push(l);
  }
  els.tlHours.replaceChildren(...labels);
}

function setZoom(i) {
  const next = clamp(i, 0, ZOOMS.length - 1);
  if (next === tlZoom) return;
  const sc = els.tlScroll;
  const centre = (sc.scrollLeft + sc.clientWidth / 2) / (sc.scrollWidth || 1);
  tlZoom = next;
  els.tlTrack.style.setProperty('--tl-zoom', ZOOMS[tlZoom]);
  els.zoomOut.disabled = tlZoom === 0;
  els.zoomIn.disabled = tlZoom === ZOOMS.length - 1;
  renderHours();
  sc.scrollLeft = centre * sc.scrollWidth - sc.clientWidth / 2;
}

/** On a scrollable (phone or zoomed) timeline, bring "now" or the newest event into view. */
function scrollTimelineToContent() {
  const sc = els.tlScroll;
  if (sc.scrollWidth <= sc.clientWidth + 2) return;
  const start = dayStartTs(day);
  const span = dayStartTs(addDays(day, 1)) - start;
  const ts = day === today() ? Date.now() / 1000 : tlEvents[0]?.start_ts;
  if (!ts) return;
  const x = ((ts - start) / span) * sc.scrollWidth;
  sc.scrollLeft = x - sc.clientWidth * 0.75;
}

function showTip(mark) {
  const ev = tlEvents.find((e) => e.id === Number(mark.dataset.id));
  if (!ev) return;
  els.tip.replaceChildren(
    h('span', { class: 'tip-time', text: fmtTime(ev.start_ts) }),
    h('span', { class: 'tip-chips' }, sortCats(ev.categories).map((k) => catChip(k, ev.categories[k]))),
  );
  els.tip.hidden = false;
  const card = els.tip.parentElement.getBoundingClientRect();
  const r = mark.getBoundingClientRect();
  const tw = els.tip.offsetWidth;
  const left = clamp(r.left + r.width / 2 - card.left - tw / 2, 8, card.width - tw - 8);
  els.tip.style.left = `${left}px`;
  els.tip.style.top = `${r.top - card.top - els.tip.offsetHeight - 8}px`;
}

function hideTip() {
  els.tip.hidden = true;
}

function onTimelineKey(e) {
  const m = e.target.closest('.tl-mark');
  if (!m) return;
  const marks = [...els.tlBar.querySelectorAll('.tl-mark')];
  let i = marks.indexOf(m);
  if (e.key === 'ArrowRight') i += 1;
  else if (e.key === 'ArrowLeft') i -= 1;
  else if (e.key === 'Home') i = 0;
  else if (e.key === 'End') i = marks.length - 1;
  else return;
  e.preventDefault();
  i = clamp(i, 0, marks.length - 1);
  m.tabIndex = -1;
  marks[i].tabIndex = 0;
  marks[i].focus();
  marks[i].scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

/* ------------------------------------------------------------------ list */

function cardFor(ev) {
  const sig = cardSig(ev, mediaRev(ev.id));
  const hit = cards.get(ev.id);
  if (hit && hit.sig === sig) return hit.el;
  const el = eventCard(ev, { onOpen: openEvent });
  cards.set(ev.id, { sig, el });
  return el;
}

function renderList() {
  if (!events.length) {
    const what = category ? CAT[category].plural.toLowerCase() : 'activity';
    const msg = day === today() ? `No ${what} yet today.` : `No ${what} on ${fmtDate(day, { weekday: 'long', month: 'long', day: 'numeric' })}.`;
    const prevDay = els.prev.dataset.day;
    els.list.replaceChildren(emptyState('history', 'Nothing here', msg,
      prevDay ? h('button', { type: 'button', class: 'btn btn-sm', onclick: () => setDay(prevDay) }, icon('chevron-left'), 'Previous day with activity') : null));
    els.more.hidden = true;
    return;
  }
  // Group by local hour, newest hour first (Ring style).
  const groups = [];
  let cur = null;
  for (const ev of events) {
    const d = new Date(ev.start_ts * 1000);
    const key = `${d.toDateString()}|${d.getHours()}`;
    if (!cur || cur.key !== key) {
      cur = { key, hour: d.getHours(), items: [] };
      groups.push(cur);
    }
    cur.items.push(ev);
  }
  const keep = new Set(events.map((e) => e.id));
  for (const id of cards.keys()) if (!keep.has(id)) cards.delete(id);
  keepFocus(els.list, () => els.list.replaceChildren(...groups.map((g) => h('section', { class: 'hour-group', 'aria-label': `${fmtHour(g.hour)}, ${g.items.length} events` },
    h('h3', { class: 'hour-head' },
      h('span', { class: 'hour-name', text: fmtHour(g.hour) }),
      h('span', { class: 'hour-count', text: `${g.items.length} event${g.items.length === 1 ? '' : 's'}` })),
    h('div', { class: 'ev-grid' }, g.items.map(cardFor))))));
  els.more.hidden = !hasMore;
}

function openEvent(ev) {
  // Navigate through the whole day when the timeline has it, else the loaded pages.
  const useTl = tlEvents.length >= events.length && tlEvents.some((e) => e.id === ev.id);
  const list = useTl ? tlEvents : events;
  openViewer({
    list,
    index: list.findIndex((e) => e.id === ev.id),
    loadOlder: !useTl && hasMore ? loadMore : null,
  });
}

/* ------------------------------------------------------------------ live updates */

function upsert(arr, ev) {
  const i = arr.findIndex((e) => e.id === ev.id);
  if (i >= 0) arr[i] = ev;
  else arr.push(ev);
  arr.sort(byNewest);
}

function onEventNew(ev) {
  refreshDaysSoon();
  if (!loadedOnce || !ev || !matches(ev)) return;
  upsert(events, ev);
  upsert(tlEvents, ev);
  renderList();
  renderTimeline();
}

function onEventUpdate(ev) {
  if (!loadedOnce || !ev) return;
  let hit = false;
  for (const arr of [events, tlEvents]) {
    const i = arr.findIndex((e) => e.id === ev.id);
    if (i >= 0) { arr[i] = ev; hit = true; }
  }
  if (hit) {
    renderList();
    renderTimeline();
  }
}

function onEventDeleted({ id }) {
  if (!loadedOnce) return;
  const before = events.length + tlEvents.length;
  events = events.filter((e) => e.id !== id);
  tlEvents = tlEvents.filter((e) => e.id !== id);
  if (events.length + tlEvents.length !== before) {
    cards.delete(id);
    renderList();
    renderTimeline();
  }
  refreshDaysSoon();
}

function onCounts(counts) {
  const t = today();
  const info = days.find((d) => d.day === t);
  if (info) info.counts = { ...info.counts, ...counts };
  else days.unshift({ day: t, events: 0, counts });
  if (day === t) renderChips();
}

/* ------------------------------------------------------------------ module API */

export function init() {
  els = {
    prev: document.getElementById('day-prev'),
    next: document.getElementById('day-next'),
    dayBtn: document.getElementById('day-btn'),
    dayMain: document.getElementById('day-main'),
    daySub: document.getElementById('day-sub'),
    dayInput: document.getElementById('day-input'),
    todayBtn: document.getElementById('day-today'),
    chips: document.getElementById('hist-chips'),
    tlScroll: document.getElementById('tl-scroll'),
    tlTrack: document.getElementById('tl-track'),
    tlBar: document.getElementById('tl-bar'),
    tlHours: document.getElementById('tl-hours'),
    tlSub: document.getElementById('tl-sub'),
    tip: document.getElementById('tl-tip'),
    zoomIn: document.getElementById('tl-zoom-in'),
    zoomOut: document.getElementById('tl-zoom-out'),
    list: document.getElementById('event-list'),
    more: document.getElementById('load-more'),
  };
  els.tlBar.setAttribute('role', 'group');
  els.zoomOut.disabled = true;

  els.prev.addEventListener('click', () => setDay(els.prev.dataset.day));
  els.next.addEventListener('click', () => setDay(els.next.dataset.day));
  els.todayBtn.addEventListener('click', () => setDay(today()));
  els.dayBtn.addEventListener('click', () => {
    try {
      if (els.dayInput.showPicker) { els.dayInput.showPicker(); return; }
    } catch { /* fall through */ }
    els.dayInput.focus();
    els.dayInput.click();
  });
  els.dayInput.addEventListener('change', () => { if (els.dayInput.value) setDay(els.dayInput.value); });
  els.chips.addEventListener('click', (e) => {
    const b = e.target.closest('.chip');
    if (b) setCategory(b.dataset.key || '');
  });
  els.more.addEventListener('click', () => loadMore().catch(() => {}));

  els.tlBar.addEventListener('click', (e) => {
    const m = e.target.closest('.tl-mark');
    if (!m) return;
    const id = Number(m.dataset.id);
    openViewer({ list: tlEvents, index: Math.max(0, tlEvents.findIndex((x) => x.id === id)) });
  });
  els.tlBar.addEventListener('keydown', onTimelineKey);
  els.tlBar.addEventListener('pointerover', (e) => {
    const m = e.target.closest('.tl-mark');
    if (m && e.pointerType === 'mouse') showTip(m);
  });
  els.tlBar.addEventListener('pointerout', (e) => { if (e.target.closest('.tl-mark')) hideTip(); });
  els.tlBar.addEventListener('focusin', (e) => { const m = e.target.closest('.tl-mark'); if (m) showTip(m); });
  els.tlBar.addEventListener('focusout', hideTip);
  els.tlScroll.addEventListener('scroll', hideTip, { passive: true });
  els.zoomIn.addEventListener('click', () => setZoom(tlZoom + 1));
  els.zoomOut.addEventListener('click', () => setZoom(tlZoom - 1));
  new ResizeObserver(() => { if (active) renderHours(); }).observe(els.tlScroll);

  bus.on('event_new', onEventNew);
  bus.on('event_update', onEventUpdate);
  bus.on('event_deleted', onEventDeleted);
  bus.on('counts', onCounts);
  bus.on('connection', ({ connected, reconnected }) => {
    if (connected && reconnected && loadedOnce) {
      loadDays();
      if (active) loadDay();
    }
  });

  // Keep "now" moving and notice midnight.
  setInterval(() => {
    const t = today();
    if (t !== lastToday) {
      lastToday = t;
      renderNav();
    }
    if (active && day === t) renderTimeline();
  }, 60000);
}

export function show(params = {}) {
  active = true;
  let changed = false;
  if (params.cat !== undefined) {
    const key = CAT[params.cat] ? params.cat : '';
    if (key !== category) { category = key; changed = true; }
    if (day !== today()) { day = today(); changed = true; }
  }
  if (params.day && /^\d{4}-\d{2}-\d{2}$/.test(params.day) && params.day !== day) {
    day = params.day > today() ? today() : params.day;
    changed = true;
  }
  if (Object.keys(params).length) history.replaceState(null, '', '#history'); // keep the URL tidy
  if (!loadedOnce) {
    loadedOnce = true;
    loadDays();
    loadDay();
  } else if (changed) {
    loadDay();
  } else {
    renderTimeline();
  }
}

export function hide() {
  active = false;
  hideTip();
}

export function refresh() {
  loadDays();
  loadDay();
}
