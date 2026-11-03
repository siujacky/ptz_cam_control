// app.js: bootstrap, tab router, header (camera name, connection dot, clock) and the
// "backend unreachable" banner. Tabs are modules exposing init(), show(params), hide().
import { api, bus, state, takeStatus, LiveStream } from './api.js';
import { hydrateIcons } from './ui.js';
import * as live from './live.js';
import * as history from './history.js';
import * as stats from './stats.js';
import * as settings from './settings.js';
import { initViewer } from './viewer.js';

const TABS = { live, history, stats, settings };
const TITLES = { live: 'Live', history: 'History', stats: 'Stats', settings: 'Settings' };
const BANNER_DELAY = 2500; // tolerate short blips before shouting

let current = null;
let stream = null;
let bannerTimer = 0;
let statusFailed = false;

/** "#history?cat=person" -> { tab: 'history', params: {cat: 'person'} } */
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [name, query = ''] = raw.split('?');
  const tab = TABS[name] ? name : 'live';
  return { tab, params: Object.fromEntries(new URLSearchParams(query)) };
}

function route() {
  const { tab, params } = parseHash();
  if (tab === current) {
    if (Object.keys(params).length) TABS[tab].show?.(params);
    return;
  }
  const prev = current;
  current = tab;
  if (prev) {
    TABS[prev].hide?.();
    document.getElementById(`tab-${prev}`).hidden = true;
  }
  document.getElementById(`tab-${tab}`).hidden = false;
  document.body.dataset.tab = tab;
  for (const a of document.querySelectorAll('.tab-link')) {
    if (a.dataset.tab === tab) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  updateTitle();
  if (prev) window.scrollTo({ top: 0 });
  TABS[tab].show?.(params);
}

/** The site's name: the PTZ camera's (status.camera follows the main camera, which can be the wide one). */
const siteName = (s) => s?.cameras?.ptz?.name || s?.camera?.name;

function updateTitle() {
  const name = siteName(state.status) || state.config?.camera?.name;
  document.title = `${TITLES[current] || 'Live'} · ${name || 'PTZ Camera'}`;
}

/* ------------------------------------------------------------------ header */

function setupHeader() {
  const clock = document.getElementById('clock');
  const camName = document.getElementById('cam-name');
  const conn = document.getElementById('conn');
  const connText = document.getElementById('conn-text');
  let lastClock = '';

  const tick = () => {
    const now = new Date();
    const text = now.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    if (text !== lastClock) {
      lastClock = text;
      clock.textContent = text;
      clock.dateTime = now.toISOString();
      clock.title = now.toLocaleDateString([], { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
    }
  };
  tick();
  setInterval(tick, 1000);

  const applyName = (name) => {
    if (name && camName.textContent !== name) {
      camName.textContent = name;
      updateTitle();
    }
  };
  bus.on('status', (s) => applyName(siteName(s)));
  bus.on('config', (c) => applyName(c?.camera?.name));

  bus.on('connection', ({ connected, paused }) => {
    conn.classList.toggle('is-online', connected);
    conn.classList.toggle('is-offline', !connected && !paused);
    connText.textContent = connected ? 'Connected' : paused ? 'Paused' : 'Reconnecting…';
    clearTimeout(bannerTimer);
    if (connected) {
      statusFailed = false;
      showBanner(false);
    } else if (!paused) {
      bannerTimer = setTimeout(() => showBanner(true), BANNER_DELAY);
    }
  });
}

function showBanner(show, text) {
  const banner = document.getElementById('banner');
  if (text) document.getElementById('banner-text').textContent = text;
  else if (show) document.getElementById('banner-text').textContent = "Can't reach the camera server. Retrying…";
  banner.hidden = !show;
  document.body.classList.toggle('has-banner', show);
}

async function loadStatus() {
  try {
    const s = takeStatus(await api.get('/api/status', { timeout: 8000 }));
    bus.emit('status', s);
  } catch (err) {
    statusFailed = true;
    if (!state.connected) {
      showBanner(true, err.status > 0 ? `The camera server answered with an error (${err.message}). Retrying…` : undefined);
    }
  }
}

function boot() {
  hydrateIcons(document);
  initViewer();
  for (const tab of Object.values(TABS)) tab.init();
  setupHeader();

  document.getElementById('banner-retry').addEventListener('click', () => {
    stream.reconnectNow();
    loadStatus();
    TABS[current]?.refresh?.();
  });

  stream = new LiveStream('/api/stream');
  stream.start();
  loadStatus();
  // The settings, for hints that depend on them (e.g. why a wide-camera box can't be followed). Settings
  // loads its own copy when opened; this one only fills the gap until then.
  api.get('/api/config').then((c) => { if (!state.config) state.config = c; }).catch(() => {});

  window.addEventListener('hashchange', route);
  route();

  // A second status fetch if the first one failed and SSE never came up.
  setInterval(() => { if (statusFailed && !state.connected) loadStatus(); }, 10000);
}

boot();
