// api.js: HTTP helpers, a small event bus, shared app state and the single SSE connection.

export class ApiError extends Error {
  constructor(message, status = 0, data = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.data = data;
  }
  get aborted() { return this.status === -1; }
}

/**
 * fetch + JSON with a timeout. Errors become ApiError with the server's {"error": "..."}
 * message when there is one. Pass {signal} to cancel a stale request.
 */
export async function request(method, url, body, { timeout = 12000, signal } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  const onAbort = () => ctrl.abort();
  if (signal) {
    if (signal.aborted) ctrl.abort();
    else signal.addEventListener('abort', onAbort, { once: true });
  }
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
      cache: 'no-store',
      credentials: 'same-origin',
    });
  } catch (err) {
    if (signal && signal.aborted) throw new ApiError('Cancelled', -1);
    throw new ApiError(ctrl.signal.aborted ? 'The server took too long to answer' : "Can't reach the server", 0);
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener('abort', onAbort);
  }
  let data = null;
  try {
    const text = await res.text();
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    const msg = data && typeof data.error === 'string' ? data.error : `${res.status} ${res.statusText || 'error'}`.trim();
    throw new ApiError(msg, res.status, data);
  }
  return data;
}

export const api = {
  get: (url, opts) => request('GET', url, undefined, opts),
  post: (url, body = {}, opts) => request('POST', url, body, opts),
  put: (url, body, opts) => request('PUT', url, body, opts),
  del: (url, opts) => request('DELETE', url, undefined, opts),
};

/* ------------------------------------------------------------------ event bus */

export class Emitter {
  constructor() { this.handlers = new Map(); }
  on(type, fn) {
    if (!this.handlers.has(type)) this.handlers.set(type, new Set());
    this.handlers.get(type).add(fn);
    return () => this.handlers.get(type).delete(fn);
  }
  emit(type, data) {
    for (const fn of [...(this.handlers.get(type) || [])]) {
      try { fn(data); } catch (err) { console.error(`[bus:${type}]`, err); }
    }
  }
}

/** App-wide bus. SSE types: status, tracks, counts, main, event_new, event_update, event_deleted.
 *  App types: connection, config, viewer. */
export const bus = new Emitter();

/** Latest known server state, shared by all tabs. */
let lastMain = null; // the newest main-camera switch seen on the event stream

/**
 * Take a new status. One built just before a main-camera switch can arrive after the switch's event: until the
 * server's status has the switch (its `since` is at least the switch's), keep the switch.
 */
export function takeStatus(s) {
  const m = s?.main;
  if (m && lastMain?.server && !(Number(m.since) >= lastMain.since)) {
    s.main = { ...m, camera: lastMain.camera, reason: lastMain.reason, since: lastMain.since };
  }
  state.status = s;
  return s;
}

export const state = {
  status: null,     // last status object (GET /api/status or SSE)
  tracks: null,     // last SSE tracks payload of the main camera
  tracksBy: {},     // camera ("ptz" | "eye") -> its last SSE tracks payload (both publish, ~5 Hz each)
  config: null,     // last config (GET/PUT /api/config)
  connected: false, // SSE connected
};

/* ------------------------------------------------------------------ cameras */
// The PTZ camera and, optionally, the "eye": a fixed USB wide camera beside it. The main camera is the one
// the live view, photos and default clips come from; failover makes the eye main while the PTZ can't see.

export const EYE_ID_BASE = 2 ** 40; // track ids of the wide camera start here (ptz.EYE_ID_BASE)
const TRACKS_STALE_MS = 3000;       // a camera that stopped publishing has nothing in view
const tracksAt = {};

/** Whether a wide camera is running (status.cameras.eye only exists while it is enabled). */
export const hasEye = () => !!state.status?.cameras?.eye;

/** The camera that is main right now. */
export const mainCamera = () => (state.status?.main?.camera === 'eye' && hasEye() ? 'eye' : 'ptz');

/** Which camera a track id belongs to. */
export const trackCamera = (id) => (Number(id) >= EYE_ID_BASE ? 'eye' : 'ptz');

/** One camera's status block ({online, fps, white, blinded, night, ...}); `camera` for a single-camera server. */
export function cameraStatus(camera, s = state.status) {
  if (!s) return null;
  return s.cameras?.[camera] || (camera === 'ptz' ? s.camera : null) || null;
}

/** The latest tracks of one camera ([] when it has not published for a while). */
export function tracksFor(camera) {
  const p = state.tracksBy[camera];
  if (!p || Date.now() - (tracksAt[camera] || 0) > TRACKS_STALE_MS) return [];
  return p.tracks || [];
}

/* ------------------------------------------------------------------ media revisions */
// Snapshots and thumbs are rewritten in place when an event improves; the URL stays the
// same, so a revision counter (bumped by event_update) is appended to bust the cache.
const revisions = new Map();
export const mediaRev = (id) => revisions.get(id) || 0;
export function mediaSrc(url, id) {
  if (!url) return '';
  const rev = mediaRev(id);
  return rev ? `${url}${url.includes('?') ? '&' : '?'}v=${rev}` : url;
}

/* ------------------------------------------------------------------ SSE */

const SSE_TYPES = ['status', 'tracks', 'counts', 'main', 'event_new', 'event_update', 'event_deleted'];

/**
 * One EventSource for the whole app. Reconnects with exponential backoff, treats a silent
 * stream (no message for 10 s; status alone arrives every 2 s) as dead, and closes itself
 * after the page has been hidden for a minute (it reconnects as soon as it is visible).
 */
export class LiveStream {
  constructor(url) {
    this.url = url;
    this.es = null;
    this.backoff = 1000;
    this.retryTimer = 0;
    this.hiddenTimer = 0;
    this.lastMessage = 0;
    this.everConnected = false;
    this.paused = false;
    this.watchdog = setInterval(() => this.checkStale(), 3000);
    document.addEventListener('visibilitychange', () => this.onVisibility());
  }

  start() { this.connect(); }

  reconnectNow() {
    this.backoff = 1000;
    this.connect();
  }

  connect() {
    clearTimeout(this.retryTimer);
    this.close();
    this.paused = false;
    let es;
    try {
      es = new EventSource(this.url);
    } catch (err) {
      this.scheduleRetry();
      return;
    }
    this.es = es;
    es.onopen = () => {
      if (this.es !== es) return;
      this.lastMessage = Date.now();
      this.backoff = 1000;
      this.setConnected(true);
    };
    es.onerror = () => {
      if (this.es !== es) return;
      // Take over from the browser's own retry so the backoff is ours and predictable.
      this.close();
      this.setConnected(false);
      this.scheduleRetry();
    };
    for (const type of SSE_TYPES) {
      es.addEventListener(type, (e) => this.dispatch(es, type, e));
    }
  }

  dispatch(es, type, e) {
    if (this.es !== es) return;
    this.lastMessage = Date.now();
    if (!state.connected) this.setConnected(true);
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    if (type === 'status') takeStatus(data);
    else if (type === 'tracks' && data) {
      const cam = data.camera === 'eye' ? 'eye' : 'ptz'; // a single-camera server sends no camera
      state.tracksBy[cam] = data;
      tracksAt[cam] = Date.now();
      if (data.main !== false) state.tracks = data;
    } else if (type === 'main' && data && state.status) {
      // A main-camera switch: take it now rather than at the next status tick (up to 2 s later).
      const server = Number(data.since) > 0; // the server's switch time (an older server sends none)
      lastMain = { camera: data.camera, reason: data.reason, since: server ? Number(data.since) : Date.now() / 1000, server };
      state.status.main = { ...(state.status.main || {}), camera: lastMain.camera, reason: lastMain.reason, since: lastMain.since };
    } else if (type === 'event_update' && data && data.id != null) revisions.set(data.id, mediaRev(data.id) + 1);
    bus.emit(type, data);
  }

  close() {
    if (this.es) {
      this.es.onopen = this.es.onerror = null;
      this.es.close();
      this.es = null;
    }
  }

  scheduleRetry() {
    clearTimeout(this.retryTimer);
    if (this.paused) return;
    const delay = Math.min(15000, this.backoff) * (0.8 + Math.random() * 0.4);
    this.backoff = Math.min(15000, this.backoff * 2);
    this.retryTimer = setTimeout(() => this.connect(), delay);
  }

  checkStale() {
    if (this.es && state.connected && Date.now() - this.lastMessage > 10000) {
      this.close();
      this.setConnected(false);
      this.scheduleRetry();
    }
  }

  onVisibility() {
    clearTimeout(this.hiddenTimer);
    if (document.hidden) {
      this.hiddenTimer = setTimeout(() => {
        this.paused = true;
        clearTimeout(this.retryTimer);
        this.close();
        this.setConnected(false, { paused: true });
      }, 60000);
    } else if (this.paused || !this.es) {
      this.reconnectNow();
    }
  }

  setConnected(connected, extra = {}) {
    if (state.connected === connected && !extra.paused) return;
    state.connected = connected;
    const reconnected = connected && this.everConnected;
    if (connected) this.everConnected = true;
    bus.emit('connection', { connected, reconnected, paused: !!extra.paused });
  }
}
