"""HTTP server: REST API, MJPEG live view, Server-Sent Events, media files and the web UI."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import shutil
import time
from urllib.parse import urlsplit

from aiohttp import web

from . import __version__
from .config import CATEGORIES, ConfigError, deep_merge, needs_restart
from .ptz import HOME_SLOT
from .storage import event_json
from .sysinfo import dir_size

log = logging.getLogger("ptzcam.web")
MASK = "********"
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain", ".home.arpa")
CTX = web.AppKey("ctx", object)


class EventBus:
    """Fan-out of Server-Sent Events. `publish` is safe to call from any thread."""

    def __init__(self):
        self._loop = None
        self._clients: set[asyncio.Queue] = set()

    def attach(self, loop):
        self._loop = loop

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def publish(self, name: str, data):
        loop = self._loop
        if loop is None or not self._clients:
            return
        msg = f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()
        try:
            loop.call_soon_threadsafe(self._fanout, msg)
        except RuntimeError:
            pass  # loop closed during shutdown

    def _fanout(self, msg: bytes):
        for q in list(self._clients):
            if q.full():  # slow client: drop its oldest message rather than block everyone
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=200)
        self._clients.add(q)
        return q

    def unsubscribe(self, q):
        self._clients.discard(q)


# ---------------------------------------------------------------- helpers

def _run(fn, *args):
    return asyncio.get_running_loop().run_in_executor(None, fn, *args)


def _error(msg: str, status: int = 400):
    return web.json_response({"error": msg}, status=status)


async def _body(request) -> dict:
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text=json.dumps({"error": "send JSON (Content-Type: application/json)"}),
                                           content_type="application/json")
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid JSON"}), content_type="application/json")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text=json.dumps({"error": "expected a JSON object"}), content_type="application/json")
    return body


def _num(v, lo, hi, name):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise web.HTTPBadRequest(text=json.dumps({"error": f"{name} must be a number"}), content_type="application/json")
    return min(max(float(v), lo), hi)


def _masked(cfg: dict) -> dict:
    out = deep_merge(cfg, {})
    if out["camera"]["password"]:
        out["camera"]["password"] = MASK
    if out["web"]["auth_password"]:
        out["web"]["auth_password"] = MASK
    return out


def build_status(ctx) -> dict:
    st = ctx.pipeline.status()
    st.update({
        "time": time.time(), "uptime": round(time.time() - ctx.started, 1), "version": __version__,
        "system": ctx.sysinfo.snapshot(), "today": ctx.storage.counts_today(),
    })
    return st


def delete_event(ctx, eid: int) -> bool:
    row = ctx.storage.delete_event(eid)
    if row is None:
        return False
    if ctx.pipeline.events is not None:
        ctx.pipeline.events.on_deleted(eid)
    rel = row["snapshot"] or row["clip"]
    if rel:
        d = (ctx.media_dir / rel).parent.resolve()
        if d.parent.parent == ctx.media_dir.resolve():  # only ever <media>/<day>/<id>
            shutil.rmtree(d, ignore_errors=True)
    ctx.bus.publish("event_deleted", {"id": eid})
    return True


def run_retention(ctx):
    s = ctx.cfg.data["storage"]
    removed = 0
    for row in ctx.storage.events_before(time.time() - s["retention_days"] * 86400):
        removed += delete_event(ctx, row["id"])
    cap = s["max_storage_gb"] * 1e9
    size = dir_size(ctx.media_dir)
    while size > cap:
        rows = ctx.storage.oldest_events(20)
        active = ctx.pipeline.events.active.id if ctx.pipeline.events and ctx.pipeline.events.active else None
        rows = [r for r in rows if r["id"] != active]
        if not rows:
            break
        for row in rows:
            if size <= cap:
                break
            rel = row["snapshot"] or row["clip"]
            freed = dir_size((ctx.media_dir / rel).parent) if rel else 0
            removed += delete_event(ctx, row["id"])
            size -= freed
    ctx.storage.purge_tracks_before(time.time() - 365 * 86400)
    for day_dir in ctx.media_dir.iterdir():
        if day_dir.is_dir() and not any(day_dir.iterdir()):
            day_dir.rmdir()
    if removed:
        log.info("retention removed %d events", removed)


# ---------------------------------------------------------------- middleware

@web.middleware
async def error_middleware(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (ConnectionResetError, asyncio.CancelledError):
        raise
    except ConfigError as e:
        return _error(str(e), 400)
    except Exception as e:
        log.exception("%s %s failed", request.method, request.path)
        return _error(str(e) or e.__class__.__name__, 500)


def _host_name(host: str) -> str:
    if host.startswith("["):  # [IPv6]:port
        return host[1:].split("]", 1)[0].lower()
    return host.rsplit(":", 1)[0].lower().rstrip(".") if host.count(":") <= 1 else host.lower()


def host_allowed(host: str, extra=()) -> bool:
    """IP literals, bare names and local-network names only. A DNS-rebinding page carries the attacker's
    public domain in Host, so this keeps outside sites from reading or driving the camera."""
    name = _host_name(host)
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return "." not in name or name.endswith(LOCAL_SUFFIXES) or name in extra


def _guard_middleware(ctx):
    @web.middleware
    async def guard(request, handler):
        host = request.headers.get("Host", "")
        if not host_allowed(host, ctx.cfg.data["web"]["allowed_hosts"]):
            return web.Response(status=421, text="Unknown host name. Add it to web.allowed_hosts in config.json.")
        if request.method not in SAFE_METHODS:  # cross-site request forgery: only our own page may change things
            origin = request.headers.get("Origin")
            site = request.headers.get("Sec-Fetch-Site")
            if (origin is not None and urlsplit(origin).netloc.lower() != host.lower()) or \
                    (origin is None and site not in (None, "same-origin", "none")):
                return _error("cross-site request refused", 403)
        return await handler(request)
    return guard


def _auth_middleware(ctx):
    @web.middleware
    async def auth(request, handler):
        w = ctx.cfg.data["web"]
        if not w["auth_password"]:
            return await handler(request)
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
            except (binascii.Error, UnicodeDecodeError):
                user = pw = None
            if user is not None and hmac.compare_digest(user.encode(), w["auth_user"].encode()) \
                    and hmac.compare_digest(pw.encode(), w["auth_password"].encode()):
                return await handler(request)
        return web.Response(status=401, text="Authentication required",
                            headers={"WWW-Authenticate": 'Basic realm="PTZ Cam"'})
    return auth


# ---------------------------------------------------------------- handlers

def _build_id(web_dir) -> str:
    """Changes whenever any UI file changes, so browsers never mix a new page with cached old modules."""
    h = hashlib.sha1()
    for p in sorted(web_dir.rglob("*")):
        if p.is_file():
            st = p.stat()
            h.update(f"{p.relative_to(web_dir)}:{st.st_mtime_ns}:{st.st_size};".encode())
    return h.hexdigest()[:10]


async def index(request):
    web_dir = request.app[CTX].web_dir
    path = web_dir / "index.html"
    if not path.is_file():
        return web.Response(status=503, text="Web UI not installed")
    build = await _run(_build_id, web_dir)
    html = path.read_text().replace('"/static/', f'"/v/{build}/')
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})


async def static_versioned(request):
    root = request.app[CTX].web_dir.resolve()
    p = (root / request.match_info["path"]).resolve()
    if root not in p.parents or not p.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(p, headers={"Cache-Control": "public, max-age=31536000, immutable"})


async def _no_cache_static(request, response):
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"


async def api_status(request):
    return web.json_response(await _run(build_status, request.app[CTX]))


def _camera(request):
    """?camera=main (default: whichever is main right now), ptz or eye."""
    cam = request.query.get("camera", "main")
    return cam if cam in ("main", "ptz", "eye") else None


async def api_live(request):
    ctx = request.app[CTX]
    overlay = request.query.get("overlay", "1") != "0"
    cam = _camera(request)
    if cam is None:
        return _error("camera must be main, ptz or eye")
    try:
        width = int(request.query.get("width", ctx.cfg.data["web"]["live_width"]))
    except ValueError:
        return _error("width must be an integer")
    width = min(max(width, 160), 1920) // 2 * 2
    resp = web.StreamResponse(headers={
        "Content-Type": "multipart/x-mixed-replace; boundary=frame",
        "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache",
    })
    await resp.prepare(request)
    loop = asyncio.get_running_loop()
    last_id, last_sent = None, 0.0
    try:
        while True:
            interval = 1.0 / ctx.cfg.data["web"]["live_fps"]
            live = ctx.pipeline.live_for(cam)  # re-read each time: "main" follows a failover at once
            fid = (id(live), live.frame_id)
            now = loop.time()
            if fid != last_id or now - last_sent > 2.0:  # repeat the last frame every 2 s (shows "offline")
                jpg = await loop.run_in_executor(None, live.jpeg, overlay, width)
                await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg)
                                 + jpg + b"\r\n")
                last_id, last_sent = fid, now
            await asyncio.sleep(max(0.01, interval - (loop.time() - now)))
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return resp


async def api_frame(request):
    ctx = request.app[CTX]
    overlay = request.query.get("overlay", "0") != "0"
    try:
        width = min(max(int(request.query.get("width", 1280)), 160), 1920) // 2 * 2
    except ValueError:
        return _error("width must be an integer")
    cam = _camera(request)
    if cam is None:
        return _error("camera must be main, ptz or eye")
    jpg = await _run(ctx.pipeline.live_for(cam).jpeg, overlay, width, 88)
    return web.Response(body=jpg, content_type="image/jpeg", headers={"Cache-Control": "no-store"})


async def api_stream(request):
    ctx = request.app[CTX]
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)
    q = ctx.bus.subscribe()
    try:
        await resp.write(b"retry: 3000\n\n")
        status = await _run(build_status, ctx)
        await resp.write(f"event: status\ndata: {json.dumps(status, separators=(',', ':'))}\n\n".encode())
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                msg = b": ping\n\n"
            await resp.write(msg)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        ctx.bus.unsubscribe(q)
    return resp


async def api_events(request):
    ctx, q = request.app[CTX], request.query
    day = q.get("day") or None
    if day and not DAY_RE.match(day):
        return _error("day must be YYYY-MM-DD")
    category = q.get("category") or None
    if category and category not in CATEGORIES:
        return _error("unknown category")
    try:
        before = float(q["before"]) if q.get("before") else None
        limit = min(max(int(q.get("limit", 50)), 1), 200)
    except ValueError:
        return _error("before/limit must be numbers")
    rows, more = await _run(ctx.storage.list_events, day, category, before, limit)
    return web.json_response({"events": [event_json(r) for r in rows], "has_more": more})


async def api_event(request):
    ctx = request.app[CTX]
    eid = int(request.match_info["id"])
    row = await _run(ctx.storage.get_event, eid)
    if row is None:
        return _error("event not found", 404)
    out = event_json(row)
    out["objects"] = [{**o, "max_score": round(o["max_score"] or 0, 3)}
                      for o in await _run(ctx.storage.event_objects, eid)]
    return web.json_response(out)


async def api_event_delete(request):
    ctx = request.app[CTX]
    if not await _run(delete_event, ctx, int(request.match_info["id"])):
        return _error("event not found", 404)
    return web.json_response({"ok": True})


async def api_days(request):
    ctx = request.app[CTX]
    try:
        limit = min(max(int(request.query.get("limit", 60)), 1), 400)
    except ValueError:
        return _error("limit must be an integer")
    return web.json_response({"days": await _run(ctx.storage.days, limit)})


async def api_stats(request):
    ctx, q = request.app[CTX], request.query
    range_ = q.get("range", "day")
    if range_ not in ("day", "week", "month"):
        return _error("range must be day, week or month")
    day = q.get("day") or None
    if day and not DAY_RE.match(day):
        return _error("day must be YYYY-MM-DD")
    category = q.get("category") or None
    if category and category not in CATEGORIES:
        return _error("unknown category")
    return web.json_response(await _run(ctx.storage.stats, range_, day, category))


async def api_ptz_move(request):
    ctx = request.app[CTX]
    b = await _body(request)
    pan = _num(b.get("pan", 0), -1, 1, "pan")
    tilt = _num(b.get("tilt", 0), -1, 1, "tilt")
    speed = _num(b.get("speed", 0.5), 0, 1, "speed")
    ctx.pipeline.autotracker.manual_override()
    ctx.pipeline.ptz.move(pan, tilt, speed=speed, hold=1.0)
    return web.json_response({"ok": True})


async def api_ptz_stop(request):
    request.app[CTX].pipeline.ptz.stop()
    return web.json_response({"ok": True})


async def api_ptz_zoom(request):
    ctx = request.app[CTX]
    b = await _body(request)
    direction = b.get("dir")
    if direction not in ("in", "out", "stop"):
        return _error("dir must be in, out or stop")
    speed = _num(b.get("speed", 0.5), 0, 1, "speed")
    ctx.pipeline.autotracker.manual_override()
    ctx.pipeline.ptz.zoom(direction, speed=speed, hold=1.0)
    return web.json_response({"ok": True})


async def api_ptz_home(request):
    """{"action": "go"} (default) moves Home; "set" stores the current view as Home; "reset" restores
    the camera's own Home position."""
    ctx = request.app[CTX]
    action = (await _body(request) if request.can_read_body else {}).get("action", "go")
    if action == "go":
        ctx.pipeline.autotracker.manual_override()
        ctx.pipeline.ptz.home()
        return web.json_response({"ok": True})
    if action == "set":
        ok, err = await _run(ctx.pipeline.ptz.set_home)
        if not ok:
            return _error(f"the camera did not store Home: {err}", 502)
        await _run(ctx.cfg.update, {"ptz": {"home_preset": HOME_SLOT}})
        ctx.pipeline.autotracker.resume()  # done adjusting the view: auto-follow carries on at once
        return web.json_response({"ok": True, "home_preset": HOME_SLOT})
    if action == "reset":
        await _run(ctx.cfg.update, {"ptz": {"home_preset": 0}})
        return web.json_response({"ok": True, "home_preset": 0})
    return _error("action must be go, set or reset")


async def api_ptz_preset(request):
    ctx = request.app[CTX]
    b = await _body(request)
    action, n = b.get("action"), b.get("preset")
    if action not in ("call", "set"):
        return _error("action must be call or set")
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 254:
        return _error("preset must be an integer 1-254")
    if action == "call":
        ctx.pipeline.autotracker.manual_override()
    ctx.pipeline.ptz.preset(action, n)
    return web.json_response({"ok": True})


async def api_ptz_autotrack(request):
    ctx = request.app[CTX]
    b = await _body(request)
    enabled, track_id = b.get("enabled"), b.get("track_id")
    if not isinstance(enabled, bool):
        return _error("enabled must be true or false")
    if track_id is not None and (isinstance(track_id, bool) or not isinstance(track_id, int)):
        return _error("track_id must be an integer or null")
    at = ctx.pipeline.autotracker
    at.set_enabled(enabled, track_id if enabled else None)
    if ctx.cfg.data["ptz"]["autotrack"] != enabled:
        await _run(ctx.cfg.update, {"ptz": {"autotrack": enabled}})
    return web.json_response({"ok": True, "autotrack": at.enabled, "target_id": at.target_id})


async def api_ptz_position(request):
    pos = request.app[CTX].pipeline.ptz.position()
    return web.json_response(pos if pos else {"error": "position unknown"})


async def api_config_get(request):
    return web.json_response(_masked(request.app[CTX].cfg.data))


async def api_config_put(request):
    ctx = request.app[CTX]
    patch = await _body(request)
    for sec, key in (("camera", "password"), ("web", "auth_password")):
        if isinstance(patch.get(sec), dict) and patch[sec].get(key) == MASK:
            del patch[sec][key]  # the UI echoed the mask back: keep the stored secret
    before = ctx.cfg.snapshot()
    changed = await _run(ctx.cfg.update, patch)
    restarted = False
    if needs_restart(changed):
        try:
            await _run(ctx.pipeline.restart)
        except Exception as e:
            log.exception("could not apply new settings; restoring the previous ones")
            await _run(ctx.cfg.restore, before)
            await _run(ctx.pipeline.restart)
            return _error(f"these settings could not be applied ({e}); the previous settings were restored", 500)
        restarted = True
    ptz = ctx.cfg.data["ptz"]
    if ("ptz", "enabled") in changed or ("ptz", "autotrack") in changed:
        # Off also stops the camera; back on restores the saved auto-follow choice.
        ctx.pipeline.autotracker.set_enabled(ptz["enabled"] and ptz["autotrack"])
    if any(p[0] == "camera" and p[1].startswith("visca") for p in changed):
        ctx.pipeline.ptz.reconfigure()
    return web.json_response({"ok": True, "config": _masked(ctx.cfg.data), "restarted": restarted})


def list_models(models_dir):
    """Detection models in models/, low-light models in models/lowlight/ (each checked by its metadata)."""
    from .detector import hef_kind

    def kind(p):
        try:
            return hef_kind(p)
        except Exception:
            return "other"

    det = sorted(f"models/{p.name}" for p in models_dir.glob("*.hef") if kind(p) in ("detector", None))
    low = sorted(f"models/lowlight/{p.name}" for p in (models_dir / "lowlight").glob("*.hef")
                 if kind(p) in ("enhancer", None))
    return det, low


async def api_models(request):
    ctx = request.app[CTX]
    det, low = await _run(list_models, ctx.cfg.base_dir / "models")
    cfg = ctx.cfg.data
    return web.json_response({"models": det, "current": cfg["detect"]["model"],
                              "lowlight_models": low, "lowlight_current": cfg["lowlight"]["model"]})


# ---------------------------------------------------------------- app

async def _status_ticker(app):
    ctx = app[CTX]
    while True:
        await asyncio.sleep(2.0)
        if ctx.bus.client_count:
            try:
                ctx.bus.publish("status", await _run(build_status, ctx))
            except Exception:
                log.exception("status update failed")


async def _retention_loop(app):
    ctx = app[CTX]
    await asyncio.sleep(60)
    while True:
        try:
            await _run(run_retention, ctx)
        except Exception:
            log.exception("retention failed")
        await asyncio.sleep(3600)


async def _on_startup(app):
    ctx = app[CTX]
    ctx.bus.attach(asyncio.get_running_loop())
    await _run(ctx.pipeline.start)
    app["tasks"] = [asyncio.create_task(_status_ticker(app)), asyncio.create_task(_retention_loop(app))]


async def _on_cleanup(app):
    for t in app.get("tasks", []):
        t.cancel()
    await _run(app[CTX].pipeline.shutdown)


def create_app(ctx) -> web.Application:
    app = web.Application(middlewares=[error_middleware, _guard_middleware(ctx), _auth_middleware(ctx)],
                          client_max_size=1024 ** 2)
    app[CTX] = ctx
    r = app.router
    r.add_get("/", index)
    r.add_get(r"/v/{build}/{path:.+}", static_versioned)
    r.add_static("/static/", ctx.web_dir, show_index=False, follow_symlinks=False)
    r.add_static("/media/", ctx.media_dir, show_index=False, follow_symlinks=False)
    r.add_get("/api/status", api_status)
    r.add_get("/api/live.mjpg", api_live)
    r.add_get("/api/frame.jpg", api_frame)
    r.add_get("/api/stream", api_stream)
    r.add_get("/api/events", api_events)
    r.add_get(r"/api/events/{id:\d+}", api_event)
    r.add_delete(r"/api/events/{id:\d+}", api_event_delete)
    r.add_get("/api/days", api_days)
    r.add_get("/api/stats", api_stats)
    r.add_post("/api/ptz/move", api_ptz_move)
    r.add_post("/api/ptz/stop", api_ptz_stop)
    r.add_post("/api/ptz/zoom", api_ptz_zoom)
    r.add_post("/api/ptz/home", api_ptz_home)
    r.add_post("/api/ptz/preset", api_ptz_preset)
    r.add_post("/api/ptz/autotrack", api_ptz_autotrack)
    r.add_get("/api/ptz/position", api_ptz_position)
    r.add_get("/api/config", api_config_get)
    r.add_put("/api/config", api_config_put)
    r.add_get("/api/models", api_models)
    app.on_response_prepare.append(_no_cache_static)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app
