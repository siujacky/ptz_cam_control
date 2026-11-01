// viewer.js: event viewer modal: clip (snapshot as poster) or snapshot, object list,
// downloads, delete, older/newer navigation (buttons and arrow keys), focus trap.
import { api, bus, mediaSrc } from './api.js';
import { CAT, DIRS, h, icon, catChip, eventTitle, sortCats, fmtTime, fmtDate, evDay, capitalize, toast, confirmDialog } from './ui.js';

let els = {};
let list = [];          // events, newest first
let index = -1;
let loadOlder = null;   // async () => [older events] (History paging), optional
let opener = null;      // element to refocus on close
let mode = 'video';     // 'video' | 'snapshot'
let detailSeq = 0;
let backdropDown = false;
const details = new Map();       // event id -> objects
const unplayable = new Set();    // clip URLs this browser failed to play
let preferNight = true;          // show the night-vision clip when an event has one
let videoEventId = null;         // event whose clip the video element holds
try { preferNight = localStorage.getItem('ptzcam.nightClip') !== '0'; } catch { /* storage blocked */ }

const current = () => list[index];

/* ------------------------------------------------------------------ open / close */

export function openViewer({ list: items, index: i = 0, loadOlder: older = null }) {
  if (!items?.length) return;
  list = items.slice();
  index = Math.min(Math.max(0, i), list.length - 1);
  loadOlder = older;
  mode = 'video';
  if (!els.dlg.open) {
    opener = document.activeElement;
    els.dlg.showModal();
    document.body.classList.add('no-scroll');
    bus.emit('viewer', { open: true });
  }
  render();
}

function close() {
  if (els.dlg.open) els.dlg.close();
}

function onClosed() {
  releaseVideo();
  els.img.removeAttribute('src');
  document.body.classList.remove('no-scroll');
  bus.emit('viewer', { open: false });
  if (opener && opener.isConnected) opener.focus({ preventScroll: true });
  opener = null;
}

/** Stop the clip and drop its connection (Range requests keep one open otherwise). */
function releaseVideo() {
  const v = els.video;
  if (!v.getAttribute('src')) return;
  v.pause();
  videoEventId = null;
  v.removeAttribute('src');
  v.removeAttribute('poster');
  v.load();
}

/* ------------------------------------------------------------------ render */

function render() {
  const ev = current();
  if (!ev) {
    close();
    return;
  }
  const title = eventTitle(ev);
  els.title.textContent = title;
  els.sub.textContent = `${fmtDate(evDay(ev), { weekday: 'long', month: 'short', day: 'numeric' })} · ${fmtTime(ev.start_ts, { seconds: true })}`;
  els.count.textContent = list.length > 1 ? `${index + 1} / ${list.length}${loadOlder ? '+' : ''}` : '';
  els.chips.replaceChildren(...sortCats(ev.categories).map((k) => catChip(k, ev.categories[k])));
  els.media.dataset.cat = ev.top_category || '';

  renderMedia(ev);

  // Downloads: the clip being shown
  const stamp = fileStamp(ev);
  const clip = clipChoice(ev);
  const clipReady = !!clip.url;
  setLink(els.dlMp4, clip.url, `ptzcam-${stamp}${clip.night ? '-night' : ''}.mp4`);
  setLink(els.dlJpg, ev.snapshot_url ? mediaSrc(ev.snapshot_url, ev.id) : null, `ptzcam-${stamp}.jpg`);
  els.dlMp4.title = clipReady ? 'Download the clip' : ev.clip_status === 'pending' ? 'The clip is still being processed' : 'No clip for this event';

  // Navigation: older = further down the newest-first list
  els.prev.disabled = !(index < list.length - 1 || loadOlder);
  els.next.disabled = index <= 0;

  loadDetails(ev);
}

/** Which clip to play: the night-vision one when it exists and is preferred (or is the only one). */
function clipChoice(ev) {
  const night = ev.clip_night_status === 'ready' && !!ev.clip_night_url && !unplayable.has(ev.clip_night_url);
  const orig = ev.clip_status === 'ready' && !!ev.clip_url && !unplayable.has(ev.clip_url);
  const useNight = night && (preferNight || !orig);
  return { url: useNight ? ev.clip_night_url : orig ? ev.clip_url : null, night: useNight, nightReady: night, origReady: orig };
}

function renderNightToggle(ev) {
  const status = ev.clip_night_status;
  const has = !!ev.night && (status === 'ready' || status === 'pending');
  els.vmNight.hidden = !has;
  if (!has) return;
  const ready = status === 'ready' && !unplayable.has(ev.clip_night_url);
  els.vmNight.disabled = !ready;
  els.vmNight.setAttribute('aria-pressed', String(ready && preferNight));
  els.vmNight.title = ready
    ? (preferNight ? "Showing the night-vision clip. Press to see the camera's original." : 'Showing the original. Press for the night-vision clip.')
    : 'The night-vision clip is still being made';
}

function renderMedia(ev) {
  const clip = clipChoice(ev);
  const clipReady = !!clip.url;
  const showVideo = mode === 'video' && clipReady;
  renderNightToggle(ev);
  els.vmVideo.setAttribute('aria-pressed', String(showVideo));
  els.vmSnap.setAttribute('aria-pressed', String(!showVideo));
  els.vmVideo.disabled = !clipReady;
  els.vmVideo.title = clipReady ? 'Play the clip' : ev.clip_status === 'pending' ? 'The clip is still being processed' : 'No clip available';

  if (showVideo) {
    els.img.hidden = true;
    els.video.hidden = false;
    if (els.video.getAttribute('src') !== clip.url) {
      const at = videoEventId === ev.id ? els.video.currentTime : 0; // switching versions of this event's clip
      els.video.poster = mediaSrc(ev.snapshot_url, ev.id);
      els.video.src = clip.url;
      videoEventId = ev.id;
      if (at) els.video.addEventListener('loadedmetadata', () => { els.video.currentTime = at; }, { once: true }); // same moment in the other version
      const p = els.video.play();
      if (p && p.catch) p.catch(() => { /* autoplay refused: the controls are there */ });
    }
    els.overlay.hidden = true;
  } else {
    releaseVideo();
    els.video.hidden = true;
    els.img.hidden = false;
    const src = mediaSrc(ev.snapshot_url, ev.id);
    if (els.img.getAttribute('src') !== src) els.img.src = src;
    els.img.alt = `Snapshot: ${eventTitle(ev)}`;
    let msg = '';
    let spin = false;
    if (mode === 'video' || ev.clip_status !== 'ready') {
      if (ev.clip_status === 'pending') { msg = 'Processing clip…'; spin = true; }
      else if (ev.clip_status === 'failed') msg = 'No clip for this event';
      else if (ev.clip_url && unplayable.has(ev.clip_url)) msg = "This browser can't play the clip. Download it instead.";
    }
    els.overlay.hidden = !msg;
    els.overlayText.textContent = msg;
    els.spinner.hidden = !spin;
    els.overlay.classList.toggle('is-warn', !!msg && !spin);
  }
}

function setLink(a, href, filename) {
  if (href) {
    a.href = href;
    a.setAttribute('download', filename);
    a.removeAttribute('aria-disabled');
    a.removeAttribute('tabindex');
  } else {
    a.removeAttribute('href');
    a.setAttribute('aria-disabled', 'true');
    a.setAttribute('tabindex', '-1');
  }
}

function fileStamp(ev) {
  const d = new Date(ev.start_ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${evDay(ev)}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}-${ev.id}`;
}

/* ------------------------------------------------------------------ objects */

async function loadDetails(ev, force = false) {
  const seq = ++detailSeq;
  if (details.has(ev.id) && !force) {
    renderObjects(details.get(ev.id));
    return;
  }
  if (!force) renderObjects(null);
  try {
    const res = await api.get(`/api/events/${ev.id}`);
    details.set(ev.id, res?.objects || []);
    if (seq === detailSeq) renderObjects(res?.objects || []);
  } catch (err) {
    if (seq !== detailSeq) return;
    els.objects.replaceChildren(h('li', { class: 'obj-empty' }, icon('warning'), `Couldn't load objects: ${err.message}`));
  }
}

function renderObjects(objs) {
  if (objs === null) {
    els.objects.replaceChildren(...[0, 1].map(() => h('li', { class: 'obj skel-row' })));
    return;
  }
  if (!objs.length) {
    els.objects.replaceChildren(h('li', { class: 'obj-empty' }, 'No object details for this event.'));
    return;
  }
  els.objects.replaceChildren(...objs.map((o) => {
    const dir = DIRS[o.direction] || DIRS.unknown;
    const conf = Math.round((Number(o.max_score) || 0) * 100);
    const seen = Number(o.last_ts) - Number(o.first_ts);
    const label = capitalize(o.label || CAT[o.category]?.label || 'Object');
    const dirIcon = dir.rot == null ? icon('dot') : icon('arrow', `rot-${dir.rot}`);
    return h('li', { class: 'obj', 'data-cat': o.category },
      h('span', { class: 'obj-icon' }, icon(CAT[o.category] ? o.category : 'dot')),
      h('span', { class: 'obj-main' },
        h('span', { class: 'obj-label', text: label }),
        h('span', { class: 'obj-sub' },
          h('span', { class: 'obj-dir', title: dir.label }, dirIcon, dir.short),
          Number.isFinite(seen) && seen > 0 ? h('span', { class: 'obj-seen', text: `seen ${Math.round(seen)} s` }) : null)),
      h('span', { class: 'obj-conf', title: 'Best detection confidence' }, `${conf}%`));
  }));
}

/* ------------------------------------------------------------------ navigation */

async function older() {
  if (index < list.length - 1) {
    index += 1;
    mode = 'video';
    render();
    return;
  }
  if (!loadOlder) return;
  els.prev.disabled = true;
  try {
    const more = await loadOlder();
    const known = new Set(list.map((e) => e.id));
    const fresh = (more || []).filter((e) => !known.has(e.id));
    if (!fresh.length) {
      loadOlder = null;
      render();
      return;
    }
    list = list.concat(fresh);
    index += 1;
    mode = 'video';
    render();
  } catch (err) {
    toast(`Couldn't load older events: ${err.message}`, { type: 'error' });
    render();
  }
}

function newer() {
  if (index <= 0) return;
  index -= 1;
  mode = 'video';
  render();
}

async function onDelete() {
  const ev = current();
  if (!ev) return;
  const ok = await confirmDialog({
    title: 'Delete this event?',
    message: 'Its snapshot and clip are removed for good. The passers-by stay counted in Stats.',
    ok: 'Delete',
    danger: true,
  });
  if (!ok) return;
  try {
    await api.del(`/api/events/${ev.id}`);
    toast('Event deleted', { type: 'success' });
    bus.emit('event_deleted', { id: ev.id }); // update every list now; the SSE echo is a no-op
  } catch (err) {
    toast(`Couldn't delete the event: ${err.message}`, { type: 'error' });
  }
}

function removeFromList(id) {
  const i = list.findIndex((e) => e.id === id);
  if (i < 0) return;
  list.splice(i, 1);
  if (!list.length) {
    close();
    return;
  }
  if (i < index) index -= 1;                // something newer vanished: keep showing the same event
  index = Math.min(index, list.length - 1); // the shown one vanished: the next older slides into its slot
  if (els.dlg.open) render();
}

/* ------------------------------------------------------------------ keyboard */

const FOCUSABLE = 'button:not([disabled]), a[href], video[controls], input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';

function trapFocus(e) {
  const items = [...els.dlg.querySelectorAll(FOCUSABLE)].filter((el) => !el.hidden && el.getClientRects().length);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (e.shiftKey && (document.activeElement === first || !els.dlg.contains(document.activeElement))) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

function onKey(e) {
  if (e.key === 'Tab') {
    trapFocus(e);
    return;
  }
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
  if (e.altKey || e.ctrlKey || e.metaKey) return;
  const t = e.target;
  if (t && (t.tagName === 'VIDEO' || t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.isContentEditable)) return; // let the video seek
  e.preventDefault();
  if (e.key === 'ArrowLeft') older();
  else newer();
}

/* ------------------------------------------------------------------ init */

export function initViewer() {
  els = {
    dlg: document.getElementById('viewer'),
    title: document.getElementById('viewer-title'),
    sub: document.getElementById('viewer-sub'),
    count: document.getElementById('viewer-count'),
    media: document.getElementById('viewer-media'),
    img: document.getElementById('viewer-img'),
    video: document.getElementById('viewer-video'),
    overlay: document.getElementById('viewer-overlay'),
    overlayText: document.getElementById('viewer-overlay-text'),
    spinner: document.getElementById('viewer-spinner'),
    prev: document.getElementById('viewer-prev'),
    next: document.getElementById('viewer-next'),
    vmVideo: document.getElementById('vm-video'),
    vmSnap: document.getElementById('vm-snap'),
    vmNight: document.getElementById('vm-night'),
    chips: document.getElementById('viewer-chips'),
    objects: document.getElementById('obj-list'),
    dlMp4: document.getElementById('dl-mp4'),
    dlJpg: document.getElementById('dl-jpg'),
    del: document.getElementById('viewer-delete'),
    closeBtn: document.getElementById('viewer-close'),
  };
  els.closeBtn.addEventListener('click', close);
  els.dlg.addEventListener('close', onClosed);
  // Document level, so the arrows work wherever focus sits while the viewer is open
  // (the confirm dialog on top handles its own keys).
  document.addEventListener('keydown', (e) => {
    if (els.dlg.open && !document.getElementById('confirm').open) onKey(e);
  });
  // Close on a backdrop click (a press that both starts and ends outside the panel).
  els.dlg.addEventListener('pointerdown', (e) => { backdropDown = e.target === els.dlg; });
  els.dlg.addEventListener('click', (e) => {
    if (e.target === els.dlg && backdropDown) close();
    backdropDown = false;
  });
  els.prev.addEventListener('click', older);
  els.next.addEventListener('click', newer);
  els.vmVideo.addEventListener('click', () => { mode = 'video'; renderMedia(current()); });
  els.vmSnap.addEventListener('click', () => { mode = 'snapshot'; renderMedia(current()); });
  els.vmNight.addEventListener('click', () => {
    preferNight = !preferNight;
    try { localStorage.setItem('ptzcam.nightClip', preferNight ? '1' : '0'); } catch { /* storage blocked */ }
    mode = 'video';
    render();
  });
  els.del.addEventListener('click', onDelete);
  els.video.addEventListener('error', () => {
    const ev = current();
    const src = els.video.getAttribute('src');
    if (!ev || !src) return;
    unplayable.add(src);
    renderMedia(ev);
  });
  for (const a of [els.dlMp4, els.dlJpg]) {
    a.addEventListener('click', (e) => { if (a.getAttribute('aria-disabled') === 'true') e.preventDefault(); });
  }

  bus.on('event_update', (ev) => {
    const i = list.findIndex((e) => e.id === ev.id);
    if (i < 0) return;
    list[i] = ev;
    details.delete(ev.id); // an object may have joined
    if (els.dlg.open && i === index) render(); // pending -> ready swaps the video in
  });
  bus.on('event_deleted', ({ id }) => {
    details.delete(id);
    removeFromList(id);
  });
}
