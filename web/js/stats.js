// stats.js: Stats tab: KPI tiles with trend, a hand-written SVG stacked bar chart (legend
// toggles, tooltips, keyboard, table view), weekday x hour heatmap, direction split and the
// busiest hour.
import { api, bus } from './api.js';
import { CATS, CAT, CAT_KEYS, DIRS, DIR_KEYS, h, svgEl, icon, fmtInt, fmtHour, clamp, emptyState, errorState, keepFocus } from './ui.js';

// Stack order, bottom to top. Chosen with the palette validator so neighbouring segments
// stay distinct under colour-vision deficiency (worst adjacent pair dE 19.6).
const STACK = ['vehicle', 'person', 'bicycle', 'animal', 'motorcycle'];
const PREV = { day: 'the day before', week: 'the previous 7 days', month: 'the previous 30 days' };
const PREV_SHORT = { day: 'yesterday', week: 'prev. 7 days', month: 'prev. 30 days' };
const ACCENT = '#2B9AF3';
const DIR_ALPHA = { ltr: 1, rtl: 0.62, approach: 0.4, leave: 0.26 };

let els = {};
let range = 'day';
let heatCat = '';
let data = null;
let hidden = new Set();
let activeCol = -1;
let seq = 0;
let loadedAt = 0;
let active = false;
let stale = false;
let refreshTimer = 0;
let lastWidth = 0;
let geom = null; // chart geometry for hit testing

/* ------------------------------------------------------------------ data */

async function load({ quiet = false } = {}) {
  const my = ++seq;
  if (!data) {
    restoreBody();
    renderSkeleton();
  } else {
    els.body.classList.add('is-refreshing'); // hold the previous render, dimmed
  }
  const q = new URLSearchParams({ range });
  if (heatCat) q.set('category', heatCat);
  try {
    const res = await api.get(`/api/stats?${q}`);
    if (my !== seq) return;
    data = res;
    loadedAt = Date.now();
    stale = false;
    renderAll();
  } catch (err) {
    if (my !== seq) return;
    if (quiet && data) return; // a background refresh failing is not worth a scare
    data = null;
    els.body.replaceChildren(errorState(err, () => load(), "Couldn't load statistics"));
    els.body.dataset.broken = '1';
  } finally {
    if (my === seq) els.body.classList.remove('is-refreshing');
  }
}

let bodyTemplate = null;
function restoreBody() {
  if (els.body.dataset.broken) {
    els.body.replaceChildren(...bodyTemplate);
    delete els.body.dataset.broken;
  }
}

function scheduleRefresh() {
  if (range !== 'day') return;
  if (!active) { stale = true; return; }
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => load({ quiet: true }), 4000);
}

/* ------------------------------------------------------------------ helpers */

const sum = (o) => CAT_KEYS.reduce((a, k) => a + (Number(o?.[k]) || 0), 0);

function hexRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function niceTicks(max) {
  if (!(max > 0)) return [0, 1, 2, 3, 4];
  const raw = max / 4;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = Math.max(1, Math.round((norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag));
  const top = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = 0; v <= top; v += step) ticks.push(v);
  return ticks;
}

/** Axis and tooltip labels for one bucket, in the browser locale. */
function bucketLabels(b, i) {
  const hourly = (data.bucket || (data.range === 'day' ? 'hour' : 'day')) === 'hour';
  if (hourly) {
    const m = /^(\d{1,2}):/.exec(b.label || '');
    const hr = m ? Number(m[1]) : i;
    return { short: fmtHour(hr), long: `${fmtHour(hr)} – ${fmtHour((hr + 1) % 24)}`, hour: hr };
  }
  if (Number(b.ts) > 0) {
    const d = new Date((Number(b.ts) + 43200) * 1000); // midday of the bucket: robust to timezone skew
    return {
      short: data.range === 'week' ? d.toLocaleDateString([], { weekday: 'short' }) : d.toLocaleDateString([], { month: 'short', day: 'numeric' }),
      long: d.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' }),
    };
  }
  return { short: b.label || String(i + 1), long: b.label || String(i + 1) };
}

/* ------------------------------------------------------------------ KPI tiles */

function trend(now, prev) {
  if (!prev && !now) return { text: 'No activity', cls: 'flat', icon: null };
  if (!prev) return { text: 'New', cls: 'up', icon: 'trend-up' };
  const pct = Math.round(((now - prev) / prev) * 100);
  if (pct === 0) return { text: 'Same', cls: 'flat', icon: null };
  return { text: `${Math.abs(pct)}%`, cls: pct > 0 ? 'up' : 'down', icon: pct > 0 ? 'trend-up' : 'trend-down' };
}

function renderKpis() {
  const t = data.totals || {};
  const p = data.previous_totals || {};
  const tile = (key, label, iconName, now, prev) => {
    const tr = trend(now, prev);
    const dir = tr.cls === 'up' ? 'up' : tr.cls === 'down' ? 'down' : 'unchanged';
    return h('div', {
      class: 'kpi',
      'data-cat': key,
      role: 'group',
      'aria-label': `${label}: ${fmtInt(now)}, ${tr.text === 'New' ? 'new' : `${dir} ${tr.text}`} versus ${fmtInt(prev)} ${PREV[range]}`,
    },
      h('span', { class: 'kpi-top' }, h('span', { class: 'kpi-icon' }, icon(iconName)), h('span', { class: 'kpi-label', text: label })),
      h('span', { class: 'kpi-value', text: fmtInt(now) }),
      h('span', { class: `kpi-trend ${tr.cls}` },
        tr.icon ? icon(tr.icon) : null,
        h('strong', { text: tr.text }),
        h('span', { class: 'kpi-vs', text: `vs ${fmtInt(prev)} ${PREV_SHORT[range]}` })));
  };
  els.kpis.replaceChildren(
    tile('total', 'All passers-by', 'sigma', sum(t), sum(p)),
    ...CATS.map((c) => tile(c.key, c.plural, c.key, Number(t[c.key]) || 0, Number(p[c.key]) || 0)),
  );
}

/* ------------------------------------------------------------------ stacked bar chart */

function renderLegend() {
  keepFocus(els.legend, () => els.legend.replaceChildren(...CATS.map((c) => h('button', {
    type: 'button',
    class: 'legend-item',
    'data-cat': c.key,
    'data-key': c.key,
    'aria-pressed': String(!hidden.has(c.key)),
    title: hidden.has(c.key) ? `Show ${c.plural.toLowerCase()}` : `Hide ${c.plural.toLowerCase()}`,
  }, h('span', { class: 'legend-swatch' }), h('span', { text: c.plural })))));
}

function renderChart() {
  const host = els.chartSvg;
  const W = Math.max(260, Math.floor(els.chart.clientWidth));
  lastWidth = W;
  const H = W < 560 ? 210 : 270;
  const M = { t: 12, r: 8, b: 26, l: 36 };
  const series = data.series || [];
  const n = Math.max(1, series.length);
  const vis = STACK.filter((k) => !hidden.has(k));
  const totals = series.map((b) => vis.reduce((a, k) => a + (Number(b.counts?.[k]) || 0), 0));
  const max = Math.max(0, ...totals);
  const ticks = niceTicks(max);
  const top = ticks[ticks.length - 1];
  const iw = W - M.l - M.r;
  const ih = H - M.t - M.b;
  const band = iw / n;
  const bw = Math.max(3, Math.min(24, band * 0.64)); // bars never fill the band
  const y = (v) => M.t + ih - (v / top) * ih;
  geom = { M, band, n, iw, ih, W, H, y, totals };

  const svg = svgEl('svg', { class: 'chart-svg', width: W, height: H, viewBox: `0 0 ${W} ${H}`, 'aria-hidden': 'true' });
  for (const t of ticks) {
    svg.append(svgEl('line', { class: t === 0 ? 'chart-axis' : 'chart-grid', x1: M.l, x2: W - M.r, y1: Math.round(y(t)) + 0.5, y2: Math.round(y(t)) + 0.5 }));
    svg.append(svgEl('text', { class: 'chart-tick', x: M.l - 8, y: y(t), dy: '0.32em', 'text-anchor': 'end' }, fmtInt(t)));
  }
  const hl = svgEl('rect', { class: 'chart-hl', x: 0, y: M.t - 4, width: band, height: ih + 4, rx: 6, visibility: 'hidden' });
  svg.append(hl);

  series.forEach((b, i) => {
    const x = M.l + band * i + (band - bw) / 2;
    const segs = vis.map((k) => [k, Number(b.counts?.[k]) || 0]).filter(([, v]) => v > 0);
    let acc = 0;
    segs.forEach(([k, v], j) => {
      const yTop = y(acc + v);
      let yBot = y(acc);
      acc += v;
      if (j > 0) yBot -= 2; // 2px surface gap between stacked segments
      const hgt = Math.max(1, yBot - yTop);
      const fill = CAT[k].color;
      if (j === segs.length - 1) {
        const r = Math.min(4, bw / 2, hgt);
        const yb = yTop + hgt;
        svg.append(svgEl('path', {
          class: 'chart-bar', fill,
          d: `M${x},${yb}V${yTop + r}Q${x},${yTop} ${x + r},${yTop}H${x + bw - r}Q${x + bw},${yTop} ${x + bw},${yTop + r}V${yb}Z`,
        }));
      } else {
        svg.append(svgEl('rect', { class: 'chart-bar', fill, x, y: yTop, width: bw, height: hgt }));
      }
    });
  });

  // X labels: as many as fit.
  const labels = series.map((b, i) => bucketLabels(b, i));
  const minGap = labels[0] && labels[0].short.length > 4 ? 46 : 34;
  const every = Math.max(1, Math.ceil(minGap / band));
  labels.forEach((l, i) => {
    if (i % every !== 0) return;
    svg.append(svgEl('text', { class: 'chart-tick x', x: M.l + band * i + band / 2, y: H - 8, 'text-anchor': 'middle' }, l.short));
  });

  const nodes = [svg];
  if (max === 0) {
    nodes.push(h('div', { class: 'chart-empty' },
      icon('stats'),
      h('span', { text: vis.length ? `No passers-by counted ${range === 'day' ? 'today' : 'in this period'} yet` : 'All categories are hidden' })));
  }
  host.replaceChildren(...nodes);
  geom.hl = hl;
  if (activeCol >= series.length) activeCol = -1;
  setActive(activeCol, false);
}

function setActive(i, announce = true) {
  activeCol = i;
  if (!geom || !data) return;
  const { hl, M, band } = geom;
  if (i < 0) {
    hl.setAttribute('visibility', 'hidden');
    els.chartTip.hidden = true;
    return;
  }
  hl.setAttribute('x', M.l + band * i);
  hl.setAttribute('visibility', 'visible');
  const b = data.series[i];
  const lab = bucketLabels(b, i);
  const vis = STACK.filter((k) => !hidden.has(k));
  const rows = [...vis].reverse() // top of the stack first, like the bars
    .map((k) => [k, Number(b.counts?.[k]) || 0]);
  const total = rows.reduce((a, [, v]) => a + v, 0);
  els.chartTip.replaceChildren(
    h('div', { class: 'tip-head', text: lab.long }),
    ...rows.map(([k, v]) => h('div', { class: `tip-row${v ? '' : ' is-zero'}`, 'data-cat': k },
      h('span', { class: 'tip-key' }), h('strong', { text: fmtInt(v) }), h('span', { text: CAT[k].plural }))),
    h('div', { class: 'tip-total' }, h('strong', { text: fmtInt(total) }), h('span', { text: 'Total' })),
  );
  els.chartTip.hidden = false;
  // Beside the column, never on top of it.
  const cx = M.l + band * i + band / 2;
  const tw = els.chartTip.offsetWidth;
  const th = els.chartTip.offsetHeight;
  const wrapW = els.chart.clientWidth;
  let left = cx + band / 2 + 10;
  if (left + tw > wrapW - 4) left = cx - band / 2 - 10 - tw;
  left = clamp(left, 4, Math.max(4, wrapW - tw - 4));
  els.chartTip.style.left = `${left}px`;
  els.chartTip.style.top = `${clamp(M.t + geom.ih / 2 - th / 2, 0, geom.H - th)}px`;
  if (announce) {
    els.live.textContent = `${lab.long}: ${fmtInt(total)} total. ${rows.filter(([, v]) => v).map(([k, v]) => `${fmtInt(v)} ${CAT[k].plural.toLowerCase()}`).join(', ')}`;
  }
}

function colFromEvent(e) {
  if (!geom) return -1;
  const r = els.chart.getBoundingClientRect();
  const x = e.clientX - r.left - geom.M.l;
  if (x < 0 || x > geom.iw) return -1;
  return clamp(Math.floor(x / geom.band), 0, geom.n - 1);
}

function onChartKey(e) {
  if (!data?.series?.length) return;
  const last = data.series.length - 1;
  let i = activeCol;
  if (e.key === 'ArrowRight') i = i < 0 ? 0 : Math.min(last, i + 1);
  else if (e.key === 'ArrowLeft') i = i < 0 ? last : Math.max(0, i - 1);
  else if (e.key === 'Home') i = 0;
  else if (e.key === 'End') i = last;
  else if (e.key === 'Escape') i = -1;
  else return;
  e.preventDefault();
  setActive(i);
}

function renderTable() {
  const series = data.series || [];
  const head = h('tr', null, h('th', { scope: 'col', text: data.range === 'day' ? 'Hour' : 'Day' }),
    ...CATS.map((c) => h('th', { scope: 'col', text: c.plural })), h('th', { scope: 'col', text: 'Total' }));
  const body = series.map((b, i) => h('tr', null,
    h('th', { scope: 'row', text: bucketLabels(b, i).long }),
    ...CATS.map((c) => h('td', { text: fmtInt(b.counts?.[c.key]) })),
    h('td', { text: fmtInt(sum(b.counts)) })));
  const foot = h('tr', null, h('th', { scope: 'row', text: 'Total' }),
    ...CATS.map((c) => h('td', { text: fmtInt(data.totals?.[c.key]) })), h('td', { text: fmtInt(sum(data.totals)) }));
  els.table.replaceChildren(h('table', { class: 'data-table' },
    h('caption', { class: 'sr-only', text: 'Passers-by per period and category' }),
    h('thead', null, head), h('tbody', null, body), h('tfoot', null, foot)));
}

/* ------------------------------------------------------------------ heatmap */

function weekdayNames() {
  // 1 Jan 2024 was a Monday; the API's heatmap rows are Monday first.
  return Array.from({ length: 7 }, (_, i) => new Date(2024, 0, 1 + i).toLocaleDateString([], { weekday: 'short' }));
}

function renderHeatmap() {
  const rows = Array.isArray(data.heatmap) ? data.heatmap : [];
  const max = Math.max(0, ...rows.flat().map(Number).filter(Number.isFinite));
  const [r, g, b] = hexRgb(heatCat ? CAT[heatCat].color : ACCENT);
  const names = weekdayNames();
  const step = els.heat.clientWidth < 520 ? 6 : 3;
  const grid = h('div', { class: 'hm-grid' });
  let peak = { v: 0, d: 0, h: 0 };
  for (let d = 0; d < 7; d++) {
    grid.append(h('span', { class: 'hm-day', text: names[d] }));
    for (let hr = 0; hr < 24; hr++) {
      const v = Number(rows[d]?.[hr]) || 0;
      if (v > peak.v) peak = { v, d, h: hr };
      const cell = h('span', { class: 'hm-cell', 'data-d': d, 'data-h': hr, 'data-v': v });
      if (v > 0) cell.style.backgroundColor = `rgba(${r},${g},${b},${(0.2 + 0.8 * Math.sqrt(v / max)).toFixed(3)})`;
      grid.append(cell);
    }
  }
  grid.append(h('span', { class: 'hm-corner' }));
  for (let hr = 0; hr < 24; hr++) {
    grid.append(h('span', { class: 'hm-hour', text: hr % step === 0 ? fmtHour(hr) : '' }));
  }
  const scope = heatCat ? CAT[heatCat].plural.toLowerCase() : 'passers-by';
  grid.setAttribute('role', 'img');
  grid.setAttribute('aria-label', max
    ? `Heatmap of ${scope} by weekday and hour. Busiest: ${names[peak.d]} ${fmtHour(peak.h)} with ${peak.v}.`
    : `No ${scope} in this period.`);
  const legend = h('div', { class: 'hm-legend', 'aria-hidden': 'true' },
    h('span', { text: 'Less' }),
    ...[0, 0.25, 0.5, 0.75, 1].map((f) => {
      const s = h('span', { class: 'hm-cell' });
      if (f > 0) s.style.backgroundColor = `rgba(${r},${g},${b},${(0.2 + 0.8 * Math.sqrt(f)).toFixed(3)})`;
      return s;
    }),
    h('span', { text: 'More' }));
  els.heat.replaceChildren(grid, legend);
  els.heat.classList.toggle('is-empty', !max);
}

function onHeatOver(e) {
  const cell = e.target.closest('.hm-grid .hm-cell');
  if (!cell) { els.tip.hidden = true; return; }
  const d = Number(cell.dataset.d);
  const hr = Number(cell.dataset.h);
  const v = Number(cell.dataset.v);
  const scope = heatCat ? CAT[heatCat].plural.toLowerCase() : 'passers-by';
  els.tip.replaceChildren(h('strong', { text: fmtInt(v) }), h('span', { text: ` ${scope} · ${weekdayNames()[d]} ${fmtHour(hr)}` }));
  els.tip.hidden = false;
  const panel = els.panel.getBoundingClientRect();
  const c = cell.getBoundingClientRect();
  const tw = els.tip.offsetWidth;
  els.tip.style.left = `${clamp(c.left + c.width / 2 - panel.left - tw / 2, 4, panel.width - tw - 4)}px`;
  els.tip.style.top = `${c.top - panel.top - els.tip.offsetHeight - 6}px`;
}

/* ------------------------------------------------------------------ busiest, directions */

function renderBusiest() {
  const b = data.busiest || {};
  const n = Number(b.count) || 0;
  const scope = heatCat ? CAT[heatCat].plural.toLowerCase() : 'passers-by';
  if (!n) {
    els.busiest.replaceChildren(h('span', { class: 'busiest-icon' }, icon('clock')),
      h('span', { class: 'busiest-text' }, h('span', { class: 'busiest-label', text: 'Busiest hour' }), h('span', { class: 'busiest-sub', text: `No ${scope} yet` })));
    return;
  }
  const m = /^(\d{1,2}):\d{2}$/.exec(String(b.label || ''));
  const when = m ? `${fmtHour(Number(m[1]))} – ${fmtHour((Number(m[1]) + 1) % 24)}` : String(b.label || '');
  const span = range === 'day' ? '' : ` over ${range === 'week' ? '7' : '30'} days`;
  els.busiest.replaceChildren(
    h('span', { class: 'busiest-icon' }, icon('clock')),
    h('span', { class: 'busiest-text' },
      h('span', { class: 'busiest-label', text: 'Busiest hour' }),
      h('strong', { class: 'busiest-value', text: when }),
      h('span', { class: 'busiest-sub', text: `${fmtInt(n)} ${scope}${span}` })));
}

function renderDirections() {
  const dirs = data.directions || {};
  const rows = CATS.map((c) => {
    const d = dirs[c.key] || {};
    const total = DIR_KEYS.reduce((a, k) => a + (Number(d[k]) || 0), 0);
    return { c, d, total };
  }).filter((r) => r.total > 0);
  if (!rows.length) {
    els.dirs.replaceChildren(emptyState('arrow', 'No directions yet', 'Which way people and vehicles go appears here once they are counted.'));
    return;
  }
  els.dirs.replaceChildren(...rows.map(({ c, d, total }) => {
    const [r, g, b] = hexRgb(c.color);
    const parts = DIR_KEYS.map((k) => [k, Number(d[k]) || 0]).filter(([, v]) => v > 0);
    const bar = h('div', { class: 'dir-bar', role: 'img', 'aria-label': `${c.plural}: ${parts.map(([k, v]) => `${fmtInt(v)} ${DIRS[k].label.toLowerCase()}`).join(', ')}` },
      parts.map(([k, v]) => {
        const seg = h('span', { class: 'dir-seg' });
        seg.style.flexGrow = String(v);
        seg.style.backgroundColor = k === 'unknown' ? 'var(--text-3)' : `rgba(${r},${g},${b},${DIR_ALPHA[k]})`;
        return seg;
      }));
    const legend = h('div', { class: 'dir-legend' }, parts.map(([k, v]) => {
      const meta = DIRS[k];
      const swatch = h('span', { class: 'dir-swatch' });
      swatch.style.backgroundColor = k === 'unknown' ? 'var(--text-3)' : `rgba(${r},${g},${b},${DIR_ALPHA[k]})`;
      return h('span', { class: 'dir-item', title: meta.label },
        swatch,
        meta.rot == null ? icon('dot') : icon('arrow', `rot-${meta.rot}`),
        h('span', { text: meta.short }),
        h('strong', { text: fmtInt(v) }),
        h('span', { class: 'dir-pct', text: `${Math.round((v / total) * 100)}%` }));
    }));
    return h('div', { class: 'dir-row', 'data-cat': c.key },
      h('div', { class: 'dir-head' }, h('span', { class: 'dir-icon' }, icon(c.key)), h('span', { class: 'dir-name', text: c.plural }), h('span', { class: 'dir-total', text: fmtInt(total) })),
      bar, legend);
  }));
}

/* ------------------------------------------------------------------ orchestration */

function renderSkeleton() {
  els.kpis.replaceChildren(...Array.from({ length: 6 }, () => h('div', { class: 'kpi skel' })));
  els.chartSvg.replaceChildren(h('div', { class: 'skel chart-skel' }));
}

function renderAll() {
  restoreBody();
  const t = sum(data.totals);
  const subs = { day: 'Today, per hour', week: 'Last 7 days, per day', month: 'Last 30 days, per day' };
  els.chartSub.textContent = `${subs[data.range] || ''}${t ? ` · ${fmtInt(t)} total` : ''}`;
  renderKpis();
  renderLegend();
  renderChart();
  renderTable();
  renderHeatmap();
  renderBusiest();
  renderDirections();
}

function setRange(r) {
  if (r === range) return;
  range = r;
  for (const b of els.range.querySelectorAll('button')) b.setAttribute('aria-pressed', String(b.dataset.range === r));
  activeCol = -1;
  load();
}

export function init() {
  els = {
    panel: document.getElementById('tab-stats'),
    body: document.getElementById('stats-body'),
    range: document.getElementById('stats-range'),
    kpis: document.getElementById('kpis'),
    legend: document.getElementById('chart-legend'),
    chart: document.getElementById('chart'),
    chartSub: document.getElementById('chart-sub'),
    table: document.getElementById('chart-table'),
    heat: document.getElementById('heatmap'),
    heatCat: document.getElementById('heat-cat'),
    busiest: document.getElementById('busiest'),
    dirs: document.getElementById('dir-list'),
    tip: document.getElementById('chart-tip'),
  };
  bodyTemplate = [...els.body.childNodes];
  els.chartSvg = h('div', { class: 'chart-host' });
  els.chartTip = h('div', { class: 'chart-tip in-chart', role: 'tooltip', hidden: true });
  els.live = h('div', { class: 'sr-only', 'aria-live': 'polite' });
  els.chart.append(els.chartSvg, els.chartTip, els.live);

  for (const c of CATS) els.heatCat.append(h('option', { value: c.key, text: c.plural }));

  els.range.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-range]');
    if (b) setRange(b.dataset.range);
  });
  els.legend.addEventListener('click', (e) => {
    const b = e.target.closest('.legend-item');
    if (!b) return;
    const k = b.dataset.key;
    if (hidden.has(k)) hidden.delete(k);
    else hidden.add(k);
    renderLegend();
    renderChart();
  });
  els.chart.addEventListener('pointermove', (e) => {
    if (e.pointerType !== 'mouse') return;
    const i = colFromEvent(e);
    if (i !== activeCol) setActive(i);
  });
  els.chart.addEventListener('pointerdown', (e) => {
    if (e.pointerType === 'mouse') return;
    const i = colFromEvent(e);
    setActive(i === activeCol ? -1 : i);
  });
  els.chart.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse') setActive(-1, false); });
  els.chart.addEventListener('keydown', onChartKey);
  els.chart.addEventListener('blur', () => setActive(-1, false));
  els.heatCat.addEventListener('change', () => { heatCat = els.heatCat.value; load(); });
  els.heat.addEventListener('pointerover', onHeatOver);
  els.heat.addEventListener('pointerleave', () => { els.tip.hidden = true; });

  new ResizeObserver(() => {
    if (!data || !active) return;
    const w = Math.floor(els.chart.clientWidth);
    if (Math.abs(w - lastWidth) > 1) {
      renderChart();
      renderHeatmap();
    }
  }).observe(els.chart);

  bus.on('counts', scheduleRefresh);
  bus.on('connection', ({ connected, reconnected }) => {
    if (connected && reconnected && data) { if (active) load({ quiet: true }); else stale = true; }
  });
}

export function show() {
  active = true;
  if (!data || stale || Date.now() - loadedAt > 60000) load({ quiet: !!data });
  else if (Math.abs(Math.floor(els.chart.clientWidth) - lastWidth) > 1) { renderChart(); renderHeatmap(); }
}

export function hide() {
  active = false;
  els.tip.hidden = true;
  setActive(-1, false);
}

export function refresh() {
  load();
}
