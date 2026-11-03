// zone.js: detection-zone polygon editor over a live frame. Click / tap adds a corner,
// corners can be dragged (or moved with the arrow keys and deleted with Delete).
// Points are normalized [x, y] in 0..1; an empty list means "the whole frame".
// With a wide camera there are two zones: the PTZ Home view's (detect.zone) and the wide
// camera's (eye.zone, where empty means automatic). Each camera keeps its own draft.
import { h, clamp, CAM_NAME, setText } from './ui.js';

const round4 = (v) => Math.round(v * 10000) / 10000;
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const CAMS = ['ptz', 'eye'];

export function createZoneEditor({ onSave }) {
  const els = {
    stage: document.getElementById('zone-stage'),
    img: document.getElementById('zone-img'),
    dim: document.getElementById('zone-dim'),
    poly: document.getElementById('zone-poly'),
    line: document.getElementById('zone-line'),
    handles: document.getElementById('zone-handles'),
    msg: document.getElementById('zone-msg'),
    pill: document.getElementById('zone-pill'),
    undo: document.getElementById('zone-undo'),
    clear: document.getElementById('zone-clear'),
    refresh: document.getElementById('zone-refresh'),
    save: document.getElementById('zone-save'),
    cams: document.getElementById('zone-cams'),
    emptyHint: document.getElementById('zone-empty-hint'),
    counting: document.getElementById('zone-counting'),
  };
  const zones = { ptz: { pts: [], saved: [] }, eye: { pts: [], saved: [] } };
  let cam = 'ptz';           // whose zone is on screen
  let eyeOn = false;         // a wide camera is configured
  let pts = zones.ptz.pts;   // the points being edited (zones[cam].pts)
  let drag = null;
  let suppressClick = false;
  let frameState = 'idle'; // idle | loading | ok | error
  let frameCam = null;     // camera of the frame on screen
  let saving = false;

  const norm = (list) => (Array.isArray(list) ? list : []).filter((p) => Array.isArray(p) && p.length === 2).map(([x, y]) => [round4(Number(x)), round4(Number(y))]);
  const dirtyOf = (c) => !same(norm(zones[c].pts), norm(zones[c].saved));
  const dirty = () => dirtyOf(cam);
  const valid = () => pts.length === 0 || pts.length >= 3;

  /** Pointer position -> normalized image coordinates (handles object-fit letterboxing). */
  function toNorm(e) {
    const r = els.img.getBoundingClientRect();
    const nw = els.img.naturalWidth || 16;
    const nh = els.img.naturalHeight || 9;
    const s = Math.min(r.width / nw, r.height / nh);
    const w = nw * s;
    const hh = nh * s;
    const x = (e.clientX - (r.left + (r.width - w) / 2)) / w;
    const y = (e.clientY - (r.top + (r.height - hh) / 2)) / hh;
    return [round4(clamp(x, 0, 1)), round4(clamp(y, 0, 1))];
  }

  function render() {
    const str = pts.map(([x, y]) => `${x},${y}`).join(' ');
    const closed = pts.length >= 3;
    els.poly.setAttribute('points', closed ? str : '');
    els.line.setAttribute('points', !closed && pts.length === 2 ? str : '');
    els.dim.setAttribute('d', closed ? `M0 0H1V1H0Z M${pts.map(([x, y]) => `${x} ${y}`).join(' L')} Z` : '');

    // Reuse handle elements so a drag in progress keeps its pointer capture.
    while (els.handles.children.length > pts.length) els.handles.lastElementChild.remove();
    while (els.handles.children.length < pts.length) {
      els.handles.append(h('button', { type: 'button', class: 'zone-handle' }, h('span', { class: 'zone-dot' })));
    }
    [...els.handles.children].forEach((btn, i) => {
      btn.dataset.i = String(i);
      btn.style.left = `${pts[i][0] * 100}%`;
      btn.style.top = `${pts[i][1] * 100}%`;
      btn.setAttribute('aria-label', `Corner ${i + 1} of ${pts.length}. Arrow keys move it, Delete removes it.`);
      btn.classList.toggle('is-first', i === 0);
    });

    const isDirty = dirty();
    let pill = cam === 'eye' ? 'Automatic' : 'Whole frame';
    let cls = '';
    if (pts.length && !valid()) { pill = `Add ${3 - pts.length} more corner${pts.length === 2 ? '' : 's'}`; cls = 'is-warn'; }
    else if (isDirty) { pill = 'Unsaved'; cls = 'is-accent'; }
    else if (pts.length) { pill = `${pts.length} corners`; cls = 'is-good'; }
    els.pill.textContent = pill;
    els.pill.className = `pill ${cls}`;
    els.undo.disabled = !pts.length;
    els.clear.disabled = !pts.length;
    els.save.disabled = saving || !isDirty || !valid();
    renderCams();
  }

  function renderCams() {
    els.cams.hidden = !eyeOn;
    for (const b of els.cams.querySelectorAll('button[data-cam]')) {
      const c = b.dataset.cam;
      b.setAttribute('aria-pressed', String(c === cam));
      b.title = dirtyOf(c) && c !== cam ? 'Unsaved changes' : '';
    }
    els.emptyHint.textContent = cam === 'eye'
      ? "No zone on the wide camera means automatic: it counts in the PTZ Home view's area (and its zone) once the cameras are synced."
      : 'No zone means the whole frame.';
    els.img.alt = cam === 'eye' ? 'Current wide camera frame for drawing its detection zone' : 'Current camera frame for drawing the detection zone';
  }

  function loadFrame() {
    frameState = 'loading';
    frameCam = cam;
    els.stage.classList.add('is-loading');
    els.msg.hidden = true;
    // Always name the camera: the default (the main camera) can be the wide one during a failover.
    els.img.src = `/api/frame.jpg?camera=${cam}&overlay=0&width=1280&_=${Date.now()}`;
  }

  els.img.addEventListener('load', () => {
    frameState = 'ok';
    els.stage.classList.remove('is-loading', 'is-error');
    if (els.img.naturalWidth && els.img.naturalHeight) {
      els.stage.style.aspectRatio = `${els.img.naturalWidth} / ${els.img.naturalHeight}`;
    }
  });
  els.img.addEventListener('error', () => {
    if (!els.img.getAttribute('src')) return;
    frameState = 'error';
    els.stage.classList.remove('is-loading');
    els.stage.classList.add('is-error');
    els.msg.textContent = "Couldn't get a camera frame. You can still draw, or try New frame.";
    els.msg.hidden = false;
  });

  els.stage.addEventListener('click', (e) => {
    if (suppressClick) { suppressClick = false; return; }
    if (e.target.closest('.zone-handle')) return;
    if (pts.length >= 64) return;
    pts.push(toNorm(e));
    render();
  });

  els.handles.addEventListener('pointerdown', (e) => {
    const btn = e.target.closest('.zone-handle');
    if (!btn || (e.pointerType === 'mouse' && e.button !== 0)) return;
    e.preventDefault();
    btn.setPointerCapture(e.pointerId);
    btn.focus({ preventScroll: true });
    drag = { i: Number(btn.dataset.i), id: e.pointerId, moved: false };
    btn.classList.add('is-dragging');
  });
  els.handles.addEventListener('pointermove', (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    pts[drag.i] = toNorm(e);
    drag.moved = true;
    render();
  });
  const endDrag = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    els.handles.querySelector('.is-dragging')?.classList.remove('is-dragging');
    if (drag.moved) suppressClick = true; // the click that ends a drag must not add a corner
    setTimeout(() => { suppressClick = false; }, 0);
    drag = null;
  };
  els.handles.addEventListener('pointerup', endDrag);
  els.handles.addEventListener('pointercancel', endDrag);
  els.handles.addEventListener('keydown', (e) => {
    const btn = e.target.closest('.zone-handle');
    if (!btn) return;
    const i = Number(btn.dataset.i);
    const step = e.shiftKey ? 0.02 : 0.005;
    const move = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
    if (move) {
      e.preventDefault();
      pts[i] = [round4(clamp(pts[i][0] + move[0], 0, 1)), round4(clamp(pts[i][1] + move[1], 0, 1))];
      render();
    } else if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault();
      pts.splice(i, 1);
      render();
      const next = els.handles.children[Math.min(i, pts.length - 1)];
      (next || els.stage).focus?.();
    }
  });

  els.undo.addEventListener('click', () => { pts.pop(); render(); });
  els.clear.addEventListener('click', () => { pts.length = 0; render(); });
  els.refresh.addEventListener('click', loadFrame);
  els.save.addEventListener('click', async () => {
    if (!valid() || !dirty()) return;
    const which = cam;
    saving = true;
    render();
    try {
      const result = await onSave(norm(zones[which].pts), which);
      if (result) setSaved(result, true, which);
    } finally {
      saving = false;
      render();
    }
  });
  els.cams.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-cam]');
    if (b) setCamera(b.dataset.cam);
  });

  /** New saved zone from the server for one camera. Keeps unsaved edits unless `force`. */
  function setSaved(zone, force = false, which = 'ptz') {
    const z = zones[which];
    const wasDirty = dirtyOf(which);
    z.saved = norm(zone);
    if (force || !wasDirty) {
      z.pts = z.saved.map((p) => p.slice());
      if (which === cam) pts = z.pts;
    }
    render();
  }

  /** Show and edit another camera's zone; its draft and frame come back with it. */
  function setCamera(next) {
    const c = CAMS.includes(next) && (next === 'ptz' || eyeOn) ? next : 'ptz';
    if (c === cam) return;
    drag = null;
    cam = c;
    pts = zones[c].pts;
    els.handles.replaceChildren(); // corners of the other camera's zone
    render();
    if (frameState !== 'idle') loadFrame();
  }

  render();
  return {
    setSaved,
    setCamera,
    /** Whether a wide camera is configured: shows the camera choice (back to the PTZ zone when not). */
    setEyeEnabled(on) {
      eyeOn = !!on;
      if (!eyeOn) setCamera('ptz');
      render();
    },
    /** Which camera counts right now ("ptz" | "eye"), for the line under the choice. */
    setCounting(which) {
      const show = eyeOn && CAMS.includes(which);
      els.counting.hidden = !show;
      if (show) {
        setText(els.counting, which === 'eye' // aria-live, and this runs on every status
          ? 'The wide camera counts passers-by right now.'
          : `The ${CAM_NAME.ptz} counts passers-by right now, on its Home view.`);
      }
    },
    isDirty: () => CAMS.some(dirtyOf),
    activate() { if (frameState === 'idle' || frameState === 'error' || frameCam !== cam) loadFrame(); },
    reloadFrame: loadFrame,
  };
}
