#!/usr/bin/env python3
"""Headless-browser smoke test for the PTZ Camera Web UI.

Run with a Python that has Playwright and its Chromium installed, against the mock
(tools/mock_server.py) or the real backend:

    python tools/ui_smoke.py --base http://127.0.0.1:8090 --out /path/to/screenshots

For a phone (390x844) and a desktop (1440x900) viewport it opens Live, History, the event
viewer, Stats and Settings (with the zone editor), writes screenshots, and collects console
errors, page errors, failed requests and HTTP error responses. It also checks behaviour the
backend relies on: SSE connects, MJPEG frames arrive and the stream is released when the
Live tab or the page is hidden, the PTZ pad repeats moves while held and stops on release,
click-to-follow sends the clicked track, a clip actually plays, focus stays in the viewer,
Escape closes it, there is no horizontal scrolling at 360 px and every control has a name.

State-changing checks only run against the mock unless --mutate yes is given. With it, a real
backend sees a short PTZ move then Home, preset 3 called and preset 5 overwritten, auto-follow
and auto-zoom toggled back, clip length changed and restored, and a test zone saved and then
restored. Deleting an event and the SSE checks never run against a real backend.
Exit status is non-zero if anything failed.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

VIEWPORTS = {
    "phone": {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True},
    "desktop": {"viewport": {"width": 1440, "height": 900}, "device_scale_factor": 1, "is_mobile": False, "has_touch": False},
}
NARROW = {"viewport": {"width": 360, "height": 740}, "device_scale_factor": 2, "is_mobile": True, "has_touch": True}
TIMEOUT = 20000

# JS helpers evaluated in the page --------------------------------------------------------

JS_UNNAMED = """() => {
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'; };
  const named = (el) => {
    if ((el.getAttribute('aria-label') || '').trim()) return true;
    const lb = el.getAttribute('aria-labelledby');
    if (lb && lb.split(/\\s+/).some((id) => (document.getElementById(id)?.textContent || '').trim())) return true;
    if ((el.textContent || '').trim()) return true;
    if ((el.getAttribute('title') || '').trim()) return true;
    if (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) return true;
    if (el.closest('label') && (el.closest('label').textContent || '').trim()) return true;
    return false;
  };
  return [...document.querySelectorAll('button, a[href], input:not([type=hidden]), select, textarea')]
    .filter((el) => visible(el) && !named(el))
    .map((el) => el.outerHTML.slice(0, 120));
}"""

JS_OVERFLOW = """() => {
  const vw = document.documentElement.clientWidth;
  const over = document.documentElement.scrollWidth - vw;
  if (over <= 0) return null;
  const bad = [];
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width && r.right > vw + 1 && getComputedStyle(el).position !== 'fixed') {
      let p = el.parentElement, clipped = false;
      while (p && p !== document.body) {
        const s = getComputedStyle(p);
        if (s.overflowX !== 'visible') { clipped = true; break; }
        p = p.parentElement;
      }
      if (!clipped) bad.push(`${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}.${(el.className && el.className.baseVal === undefined ? el.className : '').toString().split(' ').join('.')} right=${Math.round(r.right)}`);
    }
    if (bad.length > 8) break;
  }
  return { over, bad };
}"""

JS_SMALL_TARGETS = """() => {
  const out = [];
  for (const el of document.querySelectorAll('button, a[href], input, select, summary, [role=switch]')) {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    if (!r.width || !r.height || cs.visibility === 'hidden' || cs.opacity === '0') continue;
    if (el.closest('.tl-bar') || el.matches('input[type=checkbox]:not([role=switch])')) continue; // dense timeline / chips wrap
    const target = el.closest('label') ? el.closest('label').getBoundingClientRect() : r;
    if (target.width < 43.5 || target.height < 43.5) out.push(`${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''} ${(el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 24)} ${Math.round(target.width)}x${Math.round(target.height)}`);
  }
  return out;
}"""

JS_TRACK_POINT = """async () => {
  const { state } = await import('/static/js/api.js');
  const tracks = (state.tracks && state.tracks.tracks) || [];
  const t = tracks.find((t) => (t.box[2] - t.box[0]) > 0.025 && (t.box[3] - t.box[1]) > 0.04 && t.box[1] > 0.12);
  if (!t) return null;
  const img = document.getElementById('live-img');
  const r = img.getBoundingClientRect();
  const nw = img.naturalWidth || 16, nh = img.naturalHeight || 9;
  const s = Math.min(r.width / nw, r.height / nh);
  const w = nw * s, h = nh * s;
  const ox = r.left + (r.width - w) / 2, oy = r.top + (r.height - h) / 2;
  return { id: t.id, x: ox + ((t.box[0] + t.box[2]) / 2) * w, y: oy + ((t.box[1] + t.box[3]) / 2) * h };
}"""


class Run:
    """Collects problems, check results and screenshots for one browser context."""

    def __init__(self, name: str, out: Path):
        self.name = name
        self.out = out
        self.problems: list[tuple[str, str]] = []
        self.checks: list[tuple[str, bool, str]] = []
        self.shots: list[Path] = []
        self.aborted = 0
        self.requests: list[tuple[float, str, str, str | None]] = []
        self.warnings: list[str] = []

    def attach(self, page):
        def on_console(msg):
            if msg.type == "error":
                loc = msg.location or {}
                self.problems.append(("console", f"{msg.text} ({loc.get('url', '')}:{loc.get('lineNumber', '')})"))

        def on_failed(req):
            failure = req.failure or ""
            if "ERR_ABORTED" in failure:
                self.aborted += 1  # the page cancelled it: MJPEG/SSE/video teardown, re-rendered images
                return
            self.problems.append(("requestfailed", f"{req.method} {req.url} {failure}"))

        def on_response(res):
            if res.status >= 400:
                self.problems.append(("http", f"{res.status} {res.request.method} {res.url}"))

        page.on("console", on_console)
        page.on("pageerror", lambda exc: self.problems.append(("pageerror", str(exc))))
        page.on("requestfailed", on_failed)
        page.on("response", on_response)
        page.on("request", lambda r: self.requests.append((time.time(), r.method, r.url, r.post_data)))

    def check(self, label: str, ok: bool, detail: str = ""):
        self.checks.append((label, bool(ok), detail))
        mark = "ok  " if ok else "FAIL"
        print(f"  [{mark}] {label}{(': ' + detail) if detail else ''}", flush=True)

    def step(self, label: str, fn, page=None):
        """Run one step; a timeout or error becomes a failed check instead of aborting."""
        try:
            fn()
        except (PWError, AssertionError, TimeoutError) as exc:
            self.check(label, False, str(exc).splitlines()[0][:300])
            if page is not None:
                try:
                    self.shot(page, f"FAILED-{label.replace(' ', '-')}")
                    for _ in range(2):  # close any dialog so later steps are not blocked by it
                        page.keyboard.press("Escape")
                except PWError:
                    pass

    def shot(self, page, label: str, full: bool = False, max_height: int = 2800, element=None):
        path = self.out / f"{self.name}-{len(self.shots) + 1:02d}-{label}.png"
        page.evaluate("document.getElementById('toasts')?.replaceChildren()")  # transient, not layout
        if element is not None:
            element.screenshot(path=str(path), animations="disabled")
        elif full:
            height = page.evaluate("document.documentElement.scrollHeight")
            width = page.viewport_size["width"]
            # A full-page capture draws position:fixed bars at the first viewport's bottom,
            # mid-image. Pin the phone tab bar to the page bottom for the capture instead.
            page.evaluate("""() => { const t = document.querySelector('.tabbar');
                if (t && getComputedStyle(t).position === 'fixed') {
                  const h = document.documentElement.scrollHeight;
                  t.style.position = 'absolute'; t.style.bottom = 'auto'; t.style.top = `${h - t.offsetHeight}px`;
                } }""")
            try:
                page.screenshot(path=str(path), full_page=True, animations="disabled",
                                clip={"x": 0, "y": 0, "width": width, "height": min(height, max_height)})
            finally:
                page.evaluate("() => { const t = document.querySelector('.tabbar'); if (t) t.removeAttribute('style'); }")
        else:
            page.screenshot(path=str(path), animations="disabled")
        self.shots.append(path)
        return path

    def posts(self, since: float, path: str) -> list[tuple[float, str, str, str | None]]:
        return [r for r in self.requests if r[0] >= since and r[1] in ("POST", "PUT") and r[2].split("?")[0].endswith(path)]


def wait_js(page, js: str, label: str, arg=None, timeout=TIMEOUT):
    """wait_for_function with a readable failure message."""
    try:
        page.wait_for_function(js, arg=arg, timeout=timeout)
    except PWError as exc:
        raise AssertionError(f"timed out waiting for {label}") from exc


def wait_images(page, selector: str, timeout=TIMEOUT):
    page.wait_for_function(
        """(sel) => { const imgs = [...document.querySelectorAll(sel)].slice(0, 12);
             return imgs.length > 0 && imgs.every((i) => i.complete); }""", arg=selector, timeout=timeout)


def goto_tab(page, tab: str):
    page.locator(f'.tab-link[data-tab="{tab}"]').click()
    page.wait_for_selector(f"#tab-{tab}:not([hidden])", timeout=TIMEOUT)


def fake_visibility(page, hidden: bool):
    page.evaluate("""(h) => {
        Object.defineProperty(document, 'hidden', { value: h, configurable: true });
        Object.defineProperty(document, 'visibilityState', { value: h ? 'hidden' : 'visible', configurable: true });
        document.dispatchEvent(new Event('visibilitychange'));
    }""", hidden)


# ------------------------------------------------------------------------------- the tour

def mock_post(base: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(f"{base}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def bodies(posts) -> list[dict]:
    return [json.loads(p[3]) if p[3] else {} for p in posts]


def tour(browser, base: str, out: Path, name: str, opts: dict, mutate: bool, is_mock: bool) -> Run:
    run = Run(name, out)
    ctx = browser.new_context(**opts, locale="en-US")
    page = ctx.new_page()
    page.set_default_timeout(TIMEOUT)
    run.attach(page)
    desktop = not opts["is_mobile"]
    print(f"\n== {name} ({opts['viewport']['width']}x{opts['viewport']['height']}) ==", flush=True)

    # ---- Live
    def live():
        page.goto(f"{base}/#live", wait_until="domcontentloaded")
        page.wait_for_selector("#conn.is-online")
        run.check("SSE connected", True)
        page.wait_for_function("document.getElementById('live-img').naturalWidth > 0")
        run.check("MJPEG frame received", True, page.evaluate("document.getElementById('live-img').getAttribute('src')"))
        page.wait_for_function("[...document.querySelectorAll('#today-grid [data-num]')].every((e) => e.textContent.trim() !== '–')")
        page.wait_for_selector("#recent-grid .ev-card")
        wait_images(page, "#recent-grid .ev-thumb img")
        n = page.locator("#recent-grid .ev-card").count()
        run.check("recent activity shows six events", n == 6, f"{n} cards")
        run.check("PTZ status line", True, page.locator("#ptz-line-text").inner_text())
    run.step("live tab", live, page)

    def overlay_toggle():
        page.locator("#btn-overlay").click()
        page.wait_for_function("(document.getElementById('live-img').getAttribute('src') || '').includes('overlay=0')")
        page.wait_for_function("document.getElementById('live-img').naturalWidth > 0")
        page.locator("#btn-overlay").click()
        page.wait_for_function("(document.getElementById('live-img').getAttribute('src') || '').includes('overlay=1')")
        page.wait_for_function("document.getElementById('live-img').naturalWidth > 0")
        run.check("overlay toggle switches overlay=0/1", True)
    run.step("overlay toggle", overlay_toggle, page)

    def visibility():
        fake_visibility(page, True)
        released = page.evaluate("!document.getElementById('live-img').getAttribute('src')")
        fake_visibility(page, False)
        page.wait_for_function("(document.getElementById('live-img').getAttribute('src') || '').includes('live.mjpg')")
        run.check("MJPEG released while the page is hidden, resumed after", released)
    run.step("mjpeg visibility", visibility, page)

    if mutate:
        def ptz_hold():
            btn = page.locator('.dpad-btn[aria-label="Right"]')
            btn.scroll_into_view_if_needed()
            page.wait_for_function("!document.querySelector('.dpad-btn[aria-label=\"Right\"]').disabled")
            box = btn.bounding_box()
            t0 = time.time()
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.down()
            page.wait_for_timeout(1300)
            t_up = time.time()
            page.mouse.up()
            page.wait_for_timeout(500)
            moves = run.posts(t0, "/api/ptz/move")
            stops = run.posts(t0, "/api/ptz/stop")
            body = json.loads(moves[0][3]) if moves else {}
            ok = len(moves) >= 3 and len(stops) == 1 and stops[0][0] >= t_up - 0.05 and body.get("pan") == 1 and body.get("tilt") == 0
            gaps = [round(b[0] - a[0], 2) for a, b in zip(moves, moves[1:])]
            run.check("PTZ hold repeats moves then stops once", ok, f"{len(moves)} moves (gaps {gaps}), {len(stops)} stop, body {body}")
            t1 = time.time()
            page.locator("#btn-home").click()
            page.wait_for_timeout(300)
            run.check("Home button posts /api/ptz/home", bool(run.posts(t1, "/api/ptz/home")))
        run.step("ptz hold", ptz_hold, page)

        def ptz_more():
            page.locator('.dpad-btn[aria-label="Up"]').focus()
            t0 = time.time()
            page.keyboard.down("ArrowUp")
            page.keyboard.down("ArrowRight")
            page.wait_for_timeout(600)
            page.keyboard.up("ArrowRight")
            page.keyboard.up("ArrowUp")
            page.wait_for_timeout(400)
            moves = bodies(run.posts(t0, "/api/ptz/move"))
            stops = run.posts(t0, "/api/ptz/stop")
            run.check("arrow keys on the pad drive diagonals, then stop", any(b.get("pan") == 1 and b.get("tilt") == 1 for b in moves) and len(stops) == 1,
                      f"{len(moves)} moves {[(b.get('pan'), b.get('tilt')) for b in moves]}, {len(stops)} stop")
            zin = page.locator('.zoom-btn[data-zoom="in"]')
            box = zin.bounding_box()
            t0 = time.time()
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.mouse.down()
            page.wait_for_timeout(900)
            page.mouse.up()
            page.wait_for_timeout(400)
            z = bodies(run.posts(t0, "/api/ptz/zoom"))
            run.check("zoom hold repeats dir=in, release sends dir=stop", len(z) >= 3 and all(b.get("dir") == "in" for b in z[:-1]) and z[-1].get("dir") == "stop",
                      str([b.get("dir") for b in z]))
            t0 = time.time()
            page.locator('.preset-btn[data-preset="3"]').click()
            page.locator(".preset-save").click()
            page.locator('.preset-btn[data-preset="5"]').click()
            page.wait_for_timeout(500)
            pre = bodies(run.posts(t0, "/api/ptz/preset"))
            run.check("presets: tap calls, Save mode sets", pre[:2] == [{"action": "call", "preset": 3}, {"action": "set", "preset": 5}]
                      and page.locator(".preset-save").get_attribute("aria-pressed") == "false", str(pre))
            t0 = time.time()
            page.locator("label.switch-row:has(#sw-zoom)").click()
            page.wait_for_timeout(400)
            page.locator("label.switch-row:has(#sw-zoom)").click()
            page.wait_for_timeout(400)
            az = bodies(run.posts(t0, "/api/config"))
            vals = [b.get("ptz", {}).get("autozoom") for b in az]
            ok = len(az) == 2 and all(list(b) == ["ptz"] and list(b["ptz"]) == ["autozoom"] for b in az) and vals[0] is (not vals[1])
            run.check("auto-zoom switch saves {ptz:{autozoom}} only", ok, str(az))
            page.locator("#btn-home").click()
            page.wait_for_timeout(300)
        run.step("ptz keys zoom presets", ptz_more, page)

        def click_follow():
            page.locator("#stage").scroll_into_view_if_needed()
            pt = None
            for _ in range(40):
                pt = page.evaluate(JS_TRACK_POINT)
                if pt:
                    break
                page.wait_for_timeout(250)
            if not pt:
                run.check("click-to-follow", True, "skipped: no box in view")
                return
            t0 = time.time()
            page.mouse.click(pt["x"], pt["y"])
            page.wait_for_timeout(600)
            posts = run.posts(t0, "/api/ptz/autotrack")
            body = json.loads(posts[0][3]) if posts else {}
            run.check("click-to-follow posts the clicked track", body.get("enabled") is True and body.get("track_id") == pt["id"],
                      f"clicked track {pt['id']}, sent {body}")
            page.wait_for_function("document.getElementById('ptz-line-text').textContent.startsWith('Following')", timeout=5000)
            run.check("status line shows the target", True, page.locator("#ptz-line-text").inner_text())
            t1 = time.time()
            page.locator("label.switch-row:has(#sw-follow)").click()
            page.wait_for_timeout(500)
            off = run.posts(t1, "/api/ptz/autotrack")
            run.check("auto-follow switch turns it off", bool(off) and json.loads(off[0][3]).get("enabled") is False)
        run.step("click to follow", click_follow, page)

    if is_mock:
        def sse_events():
            ev = mock_post(base, "/_mock/event", {"hold": 4})
            sel = f'#recent-grid .ev-card[data-id="{ev["id"]}"]'
            page.wait_for_selector(f"{sel}.is-pending", timeout=6000)
            page.wait_for_selector(f"{sel}.is-ready", timeout=12000)
            run.check("SSE event_new shows a pending card, event_update makes it ready", True, f"event {ev['id']}")
        run.step("sse events", sse_events, page)

    def live_shot():
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_function("document.getElementById('live-img').naturalWidth > 0")
        page.wait_for_timeout(400)
        run.shot(page, "live", full=True)
        if not desktop:
            run.shot(page, "live-first-screen")
    run.step("live screenshot", live_shot, page)

    def viewer_pauses_live():
        page.locator("#recent-grid .ev-card").first.click()
        page.wait_for_selector("#viewer[open]")
        paused = page.evaluate("!document.getElementById('live-img').getAttribute('src')")
        page.keyboard.press("Escape")
        page.wait_for_selector("#viewer:not([open])", state="attached")
        page.wait_for_function("(document.getElementById('live-img').getAttribute('src') || '').includes('live.mjpg')")
        run.check("MJPEG paused while the viewer is open", paused)
    run.step("viewer pauses live", viewer_pauses_live, page)

    # ---- History
    def history():
        goto_tab(page, "history")
        released = page.evaluate("!document.getElementById('live-img').getAttribute('src')")
        run.check("MJPEG released when leaving Live", released)
        page.wait_for_selector("#event-list .ev-card")
        page.wait_for_selector("#tl-bar .tl-mark")
        wait_images(page, "#event-list .ev-thumb img")
        n_marks = page.locator("#tl-bar .tl-mark").count()
        n_cards = page.locator("#event-list .ev-card").count()
        run.check("timeline and cards rendered", n_marks >= n_cards > 0, f"{n_marks} markers, {n_cards} cards")
        run.check("filter chips with counts", page.locator("#hist-chips .chip").count() == 6,
                  page.locator("#hist-chips").inner_text().replace("\n", " "))
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
        run.shot(page, "history", full=True)
    run.step("history tab", history, page)

    def history_interactions():
        t0 = time.time()
        # Filter by a category that has events today (real data may have no people).
        cat = page.evaluate("""() => {
            for (const c of document.querySelectorAll('#hist-chips .chip[data-key]')) {
                const n = parseInt((c.textContent.match(/\\d+/) || ['0'])[0], 10);
                if (c.dataset.key && n > 0) return c.dataset.key;
            }
            return 'person';
        }""")
        page.locator(f'#hist-chips .chip[data-key="{cat}"]').click()
        page.wait_for_function(f"document.querySelector('#hist-chips .chip[data-key=\"{cat}\"]').getAttribute('aria-pressed') === 'true'")
        page.wait_for_selector("#event-list .ev-card, #event-list .empty")
        cats_ok = page.evaluate(f"[...document.querySelectorAll('#event-list .ev-card')].every((c) => c.querySelector('.cat-chip[data-cat=\"{cat}\"]'))")
        asked = any(f"category={cat}" in r[2] for r in run.requests if r[0] >= t0)
        run.check(f"category filter ({cat})", cats_ok and asked)
        page.locator('#hist-chips .chip[data-key=""]').click()
        page.wait_for_selector("#event-list .ev-card")
        if page.locator("#load-more").is_visible():
            before = page.locator("#event-list .ev-card").count()
            page.locator("#load-more").click()
            page.wait_for_function("(n) => document.querySelectorAll('#event-list .ev-card').length > n", arg=before)
            run.check("load more pages with `before`", any("before=" in r[2] for r in run.requests if r[0] >= t0),
                      f"{before} -> {page.locator('#event-list .ev-card').count()} cards")
        if page.locator("#day-prev").is_disabled():  # real data may have no earlier day with activity
            run.check("day navigator back disabled without earlier activity", True)
            run.check("next day disabled on today", page.locator("#day-next").is_disabled())
            return
        page.locator("#day-prev").click()
        page.wait_for_function("document.getElementById('day-main').textContent !== 'Today'")
        page.wait_for_selector("#event-list .ev-card, #event-list .empty")
        label = page.locator("#day-main").inner_text()
        page.locator("#day-today").click()
        page.wait_for_function("document.getElementById('day-main').textContent === 'Today'")
        page.wait_for_selector("#event-list .ev-card")
        run.check("day navigator back and to today", True, f"visited {label}")
        run.check("next day disabled on today", page.locator("#day-next").is_disabled())
    run.step("history interactions", history_interactions, page)

    if is_mock:
        def viewer_swap():
            ev = mock_post(base, "/_mock/event", {"hold": 5})
            sel = f'#event-list .ev-card[data-id="{ev["id"]}"]'
            page.wait_for_selector(f"{sel}.is-pending", timeout=8000)
            page.locator(sel).click()
            page.wait_for_selector("#viewer[open]")
            wait_js(page, "() => !document.getElementById('viewer-overlay').hidden && document.getElementById('viewer-video').hidden",
                    "the processing indicator over the snapshot", timeout=5000)
            wait_js(page, """() => { const v = document.getElementById('viewer-video');
                return !v.hidden && v.readyState >= 2; }""", "the video to replace the snapshot when the clip is ready", timeout=15000)
            run.check("viewer swaps pending snapshot for the clip on event_update", True, f"event {ev['id']}")
            page.keyboard.press("Escape")
            page.wait_for_selector("#viewer:not([open])", state="attached")
        run.step("viewer swap", viewer_swap, page)

        def keep_focus():
            card = page.locator("#event-list .ev-card").nth(2)
            card.focus()
            eid = card.get_attribute("data-id")
            ev = mock_post(base, "/_mock/event", {"hold": 3})
            page.wait_for_selector(f'#event-list .ev-card[data-id="{ev["id"]}"]', timeout=8000)
            same = page.evaluate("(id) => !!document.activeElement && document.activeElement.dataset.id === id", eid)
            run.check("keyboard focus survives a live list update", same, f"focused card {eid}, new event {ev['id']}")
        run.step("keep focus", keep_focus, page)

    # ---- Viewer
    def viewer():
        page.evaluate("window.scrollTo(0, 0)")
        card = page.locator("#event-list .ev-card.is-ready").first
        card.scroll_into_view_if_needed()
        card.click()
        page.wait_for_selector("#viewer[open]")
        wait_js(page, """() => { const v = document.getElementById('viewer-video');
            return !v.hidden && v.readyState >= 2 && v.currentTime > 0.3; }""", "the clip to play", timeout=15000)
        t = page.evaluate("document.getElementById('viewer-video').currentTime")
        run.check("clip plays in the viewer", True, f"currentTime {t:.1f}s")
        page.wait_for_selector("#obj-list .obj")
        run.check("object list", True, page.locator("#obj-list").inner_text().replace("\n", " | ")[:120])
        href = page.locator("#dl-mp4").get_attribute("href") or ""
        run.check("MP4 download link", href.endswith(".mp4") and page.locator("#dl-mp4").get_attribute("download") is not None, href)
        page.wait_for_timeout(300)
        run.shot(page, "viewer")
        title0 = page.locator("#viewer-sub").inner_text()
        page.locator("#viewer-close").focus()
        page.keyboard.press("ArrowLeft")
        wait_js(page, "(t) => document.getElementById('viewer-sub').textContent !== t", "ArrowLeft to show an older event", arg=title0)
        page.keyboard.press("ArrowRight")
        wait_js(page, "(t) => document.getElementById('viewer-sub').textContent === t", "ArrowRight to come back", arg=title0)
        run.check("arrow keys move between events", True)
        page.locator("#vm-snap").click()
        wait_js(page, "!document.getElementById('viewer-img').hidden && document.getElementById('viewer-img').complete", "the snapshot to show")
        run.check("snapshot toggle", page.evaluate("document.getElementById('viewer-video').hidden"))
        inside = True
        for _ in range(14):
            page.keyboard.press("Tab")
            inside = inside and page.evaluate("document.getElementById('viewer').contains(document.activeElement)")
        run.check("focus stays inside the viewer", inside)
        page.keyboard.press("Escape")
        page.wait_for_selector("#viewer:not([open])", state="attached")
        run.check("Escape closes the viewer", True)
    run.step("viewer", viewer, page)

    if mutate and is_mock:  # deleting is destructive: only ever against the mock
        def delete_event():
            card = page.locator("#event-list .ev-card").last
            card.scroll_into_view_if_needed()
            eid = card.get_attribute("data-id")
            card.click()
            page.wait_for_selector("#viewer[open]")
            page.locator("#viewer-delete").click()
            page.wait_for_selector("#confirm[open]")
            run.check("delete asks for confirmation", "Delete" in page.locator("#confirm-ok").inner_text())
            t0 = time.time()
            page.locator("#confirm-ok").click()
            page.wait_for_function("(id) => !document.querySelector(`#event-list .ev-card[data-id=\"${id}\"]`)", arg=eid)
            dels = [r for r in run.requests if r[0] >= t0 and r[1] == "DELETE"]
            run.check("DELETE /api/events/{id} removes the card", bool(dels) and dels[0][2].endswith(f"/api/events/{eid}"), dels[0][2] if dels else "no DELETE")
            if page.locator("#viewer[open]").count():
                page.keyboard.press("Escape")
                page.wait_for_selector("#viewer:not([open])", state="attached")
        run.step("delete event", delete_event, page)

    # ---- Stats
    def stats():
        goto_tab(page, "stats")
        page.wait_for_selector("#kpis .kpi:not(.skel)")
        page.wait_for_selector("#chart .chart-bar")
        page.wait_for_selector("#heatmap .hm-grid")
        page.wait_for_selector("#dir-list .dir-row")
        run.check("stats rendered", True, page.locator("#chart-sub").inner_text())
        if desktop:
            idx = page.evaluate("""() => { const bars = [...document.querySelectorAll('#chart .chart-bar')];
                const best = bars.reduce((a, b) => (b.getBBox().y < a.getBBox().y ? b : a));
                const r = best.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + 4 }; }""")
            page.locator(".chart-card").scroll_into_view_if_needed()
            idx = page.evaluate("""() => { const bars = [...document.querySelectorAll('#chart .chart-bar')];
                const best = bars.reduce((a, b) => (b.getBBox().y < a.getBBox().y ? b : a));
                const r = best.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + 4 }; }""")
            page.mouse.move(idx["x"], idx["y"])
            page.wait_for_selector("#chart .chart-tip:not([hidden])")
            run.check("chart tooltip on hover", True, page.locator("#chart .chart-tip").inner_text().replace("\n", " ")[:90])
            run.shot(page, "stats-tooltip", element=page.locator(".chart-card"))
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(200)
        run.shot(page, "stats", full=True)
        page.mouse.move(5, 5)
        page.locator('#stats-range button[data-range="week"]').click()
        page.wait_for_function("document.getElementById('chart-sub').textContent.startsWith('Last 7 days')")
        bars = page.locator("#chart .chart-bar").count()
        page.locator('#stats-range button[data-range="month"]').click()
        page.wait_for_function("document.getElementById('chart-sub').textContent.startsWith('Last 30 days')")
        run.check("7 and 30 day ranges", bars > 0, f"{bars} week segments")
        page.locator('#chart-legend .legend-item[data-key="vehicle"]').click()
        hidden = page.evaluate("""() => [...document.querySelectorAll('#chart .chart-bar')].every((b) => b.getAttribute('fill').toUpperCase() !== '#FFB020')""")
        page.locator('#chart-legend .legend-item[data-key="vehicle"]').click()
        run.check("legend toggle hides a category", hidden)
        page.locator('#stats-range button[data-range="day"]').click()
        page.wait_for_function("document.getElementById('chart-sub').textContent.startsWith('Today')")
    run.step("stats tab", stats, page)

    # ---- Settings
    def settings():
        goto_tab(page, "settings")
        page.wait_for_selector("#settings-form:not([hidden])")
        page.wait_for_function("document.getElementById('zone-img').naturalWidth > 0")
        page.wait_for_selector("#sys-grid dt")
        run.check("settings form and frame loaded", True, page.locator("#f-model").input_value())
        # Editing shows the save bar; Discard puts things back without saving.
        page.locator("#f-clip").focus()
        page.keyboard.press("ArrowRight")
        page.wait_for_selector("#savebar:not([hidden])")
        txt = page.locator("#savebar-text").inner_text()
        page.locator("#settings-discard").click()
        page.wait_for_selector("#savebar", state="hidden")
        run.check("edit shows the save bar, Discard resets", True, txt)
        if mutate:
            t0 = time.time()
            at_max = page.evaluate("(() => { const el = document.getElementById('f-clip'); return +el.value >= +el.max; })()")
            away, back = ("ArrowLeft", "ArrowRight") if at_max else ("ArrowRight", "ArrowLeft")
            page.locator("#f-clip").focus()
            page.keyboard.press(away)
            page.locator("#settings-save").click()
            page.wait_for_selector("#savebar", state="hidden")
            page.locator("#f-clip").focus()
            page.keyboard.press(back)
            page.locator("#settings-save").click()
            page.wait_for_selector("#savebar", state="hidden")
            puts = bodies(run.posts(t0, "/api/config"))
            ok = len(puts) == 2 and list(puts[0]) == ["events"] and list(puts[0]["events"]) == ["clip_seconds"]
            run.check("Save sends only the changed field", ok, str(puts))
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(200)
        run.shot(page, "settings", full=True, max_height=4200)
    run.step("settings tab", settings, page)

    def night_ui():
        st = page.evaluate("fetch('/api/status').then(r => r.json())")
        active = bool((st.get("lowlight") or {}).get("active"))
        mode = page.locator("#f-ll-mode").input_value()
        opts = page.evaluate("[...document.querySelectorAll('#f-ll-model option')].map(o => o.value)")
        run.check("night vision settings card", mode in ("auto", "on", "off") and "models/lowlight/zero_dce.hef" in opts,
                  f"mode={mode}, models={opts}")
        wait_js(page, "['On now', 'Off now'].includes(document.getElementById('night-pill').textContent)", "night pill")
        run.check("night status line", True, page.locator("#night-status").inner_text())
        goto_tab(page, "live")
        try:
            wait_js(page, f"document.getElementById('night-badge').hidden === {str(not active).lower()}", "night badge")
            run.check("night badge matches night vision state", True, f"active={active}")
            evs = page.evaluate("fetch('/api/events?limit=6').then(r => r.json())")["events"]
            night = next((e for e in evs if e.get("clip_night_url")), None)
            if night is None:
                run.check("viewer night toggle", True, "skipped: no recent night clip")
                return
            page.locator(f"#recent-grid [data-id='{night['id']}']").click()
            page.wait_for_selector("#viewer[open]")
            wait_js(page, "(document.getElementById('viewer-video').getAttribute('src') || '').endsWith('clip_night.mp4')", "night clip")
            pressed = page.locator("#vm-night").get_attribute("aria-pressed")
            box, panel = page.locator("#vm-night").bounding_box(), page.locator(".viewer-side").bounding_box()
            inside = box["x"] >= panel["x"] - 1 and box["x"] + box["width"] <= panel["x"] + panel["width"] + 1
            page.locator("#vm-night").click()
            wait_js(page, "(document.getElementById('viewer-video').getAttribute('src') || '').endsWith('/clip.mp4')", "original clip")
            page.locator("#vm-night").click()  # leave the preference as it was
            page.keyboard.press("Escape")
            page.wait_for_selector("#viewer:not([open])", state="attached")
            run.check("viewer plays the night clip and toggles to the original", pressed == "true", f"event {night['id']}")
            run.check("night toggle fits the viewer panel", inside, f"button {box}, panel {panel}")
        finally:
            goto_tab(page, "settings")  # the zone editor check that follows starts on Settings
    if not is_mock:
        run.step("night vision ui", night_ui, page)

    def zone():
        stage = page.locator("#zone-stage")
        stage.scroll_into_view_if_needed()
        box = stage.bounding_box()
        for fx, fy in [(0.12, 0.62), (0.88, 0.58), (0.95, 0.97), (0.05, 0.97)]:
            page.mouse.click(box["x"] + fx * box["width"], box["y"] + fy * box["height"])
        n = page.locator("#zone-handles .zone-handle").count()
        run.check("zone editor adds corners", n == 4, f"{n} corners")
        h = page.locator("#zone-handles .zone-handle").nth(1).bounding_box()
        page.mouse.move(h["x"] + h["width"] / 2, h["y"] + h["height"] / 2)
        page.mouse.down()
        page.mouse.move(h["x"] + h["width"] / 2 - 40, h["y"] + h["height"] / 2 - 25, steps=6)
        page.mouse.up()
        moved = page.locator("#zone-handles .zone-handle").count() == 4
        run.check("zone corner drag (no extra corner)", moved)
        page.wait_for_timeout(150)
        run.shot(page, "zone", element=page.locator(".zone-card"))
        if mutate:
            with urllib.request.urlopen(f"{base}/api/config", timeout=10) as r:
                original_zone = json.load(r).get("detect", {}).get("zone", [])
            t0 = time.time()
            page.locator("#zone-save").click()
            page.wait_for_timeout(600)
            puts = run.posts(t0, "/api/config")
            body = json.loads(puts[0][3]) if puts else {}
            zone_pts = body.get("detect", {}).get("zone", [])
            run.check("zone saved as partial PUT", list(body) == ["detect"] and len(zone_pts) == 4, json.dumps(body)[:120])
            req = urllib.request.Request(f"{base}/api/config", data=json.dumps({"detect": {"zone": original_zone}}).encode(),
                                         headers={"Content-Type": "application/json"}, method="PUT")
            urllib.request.urlopen(req, timeout=10).close()  # put the configured zone back
        else:
            page.locator("#zone-clear").click()
    run.step("zone editor", zone, page)

    def a11y():
        for tab in ("live", "history", "stats", "settings"):
            goto_tab(page, tab)
            page.wait_for_timeout(300)
            unnamed = page.evaluate(JS_UNNAMED)
            run.check(f"every control on {tab} has an accessible name", not unnamed, "; ".join(unnamed[:4]))
            if not desktop:
                small = page.evaluate(JS_SMALL_TARGETS)
                if small:
                    run.warnings.append(f"{tab}: {len(small)} targets under 44px: " + "; ".join(small[:6]))
    run.step("accessibility", a11y, page)

    ctx.close()
    return run


def narrow(browser, base: str, out: Path) -> Run:
    """No horizontal scrolling at 360 px wide, on every tab and in the viewer."""
    run = Run("narrow360", out)
    ctx = browser.new_context(**NARROW, locale="en-US")
    page = ctx.new_page()
    page.set_default_timeout(TIMEOUT)
    run.attach(page)
    print("\n== narrow (360x740) ==", flush=True)

    def check_all():
        page.goto(f"{base}/#live", wait_until="domcontentloaded")
        page.wait_for_selector("#recent-grid .ev-card")
        waits = {"live": "#today-grid .stat-tile", "history": "#event-list .ev-card", "stats": "#chart .chart-bar",
                 "settings": "#settings-form:not([hidden])"}
        for tab, sel in waits.items():
            if tab != "live":
                goto_tab(page, tab)
            page.wait_for_selector(sel)
            page.wait_for_timeout(400)
            res = page.evaluate(JS_OVERFLOW)
            run.check(f"no horizontal scroll on {tab}", res is None, json.dumps(res) if res else "")
            if res:
                run.shot(page, f"overflow-{tab}")
        goto_tab(page, "history")
        page.locator("#event-list .ev-card").first.click()
        page.wait_for_selector("#viewer[open]")
        page.wait_for_timeout(400)
        over = page.evaluate("(() => { const d = document.querySelector('#viewer .viewer-side'); return d.scrollWidth - d.clientWidth; })()")
        run.check("no horizontal overflow in the viewer", over <= 0, f"{over}px")
        page.keyboard.press("Escape")
    run.step("narrow layout", check_all, page)
    ctx.close()
    return run


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default="http://127.0.0.1:8090", help="server base URL")
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "ptzcam-ui-smoke"), help="screenshot directory")
    ap.add_argument("--only", choices=["phone", "desktop"], help="run a single viewport")
    ap.add_argument("--mutate", choices=["auto", "yes", "no"], default="auto",
                    help="run state-changing checks (PTZ, auto-follow, zone save). auto = only against the mock")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()

    try:
        with urllib.request.urlopen(f"{base}/api/status", timeout=10) as r:
            status = json.load(r)
    except OSError as exc:
        print(f"SMOKE FAIL: {base} is not reachable ({exc})")
        return 2
    is_mock = "mock" in str(status.get("version", ""))
    mutate = args.mutate == "yes" or (args.mutate == "auto" and is_mock)
    print(f"server {base}: version {status.get('version')} ({'mock' if is_mock else 'real backend'}); "
          f"state-changing checks {'on' if mutate else 'off'}; screenshots in {out}")

    runs = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        for name, opts in VIEWPORTS.items():
            if args.only and name != args.only:
                continue
            runs.append(tour(browser, base, out, name, opts, mutate, is_mock))
        if not args.only:
            runs.append(narrow(browser, base, out))
        browser.close()

    print("\n== summary ==")
    totals = {"console": 0, "pageerror": 0, "requestfailed": 0, "http": 0}
    failed_checks = 0
    shots = []
    for run in runs:
        for kind, text in run.problems:
            totals[kind] = totals.get(kind, 0) + 1
            print(f"  {run.name}: {kind}: {text}")
        for label, ok, detail in run.checks:
            if not ok:
                failed_checks += 1
                print(f"  {run.name}: check failed: {label}: {detail}")
        for w in run.warnings:
            print(f"  {run.name}: warning: {w}")
        shots += run.shots
        print(f"  {run.name}: {sum(ok for _, ok, _ in run.checks)}/{len(run.checks)} checks passed, "
              f"{len(run.shots)} screenshots, {run.aborted} requests cancelled by the page (expected)")
    for p in shots:
        print(f"  screenshot: {p}")
    bad = sum(totals.values()) + failed_checks
    line = (f"{len(shots)} screenshots, {totals['console']} console errors, {totals['pageerror']} page errors, "
            f"{totals['requestfailed']} failed requests, {totals['http']} HTTP errors, {failed_checks} failed checks")
    print(("SMOKE OK: " if not bad else "SMOKE FAIL: ") + line)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
