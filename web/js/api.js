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

/** App-wide bus. SSE types: status, tracks, counts, event_new, event_update, event_deleted.
 *  App types: connection, config, viewer. */
export const bus = new Emitter();

/** Latest known server state, shared by all tabs. */
export const state = {
  status: null,     // last status object (GET /api/status or SSE)
  tracks: null,     // last SSE tracks payload
  config: null,     // last config (GET/PUT /api/config)
  connected: false, // SSE connected
};

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

const SSE_TYPES = ['status', 'tracks', 'counts', 'event_new', 'event_update', 'event_deleted'];

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
    if (type === 'status') state.status = data;
    else if (type === 'tracks') state.tracks = data;
    else if (type === 'event_update' && data && data.id != null) revisions.set(data.id, mediaRev(data.id) + 1);
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
