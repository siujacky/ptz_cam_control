"""Run the camera sync (tools/calibrate_sync.py) by itself at the next clear daytime moment.

    sudo systemd-run --unit=ptzcam-sync-once /home/pi/ptz_cam/.venv/bin/python /home/pi/ptz_cam/tools/sync_when_clear.py
    journalctl -u ptzcam-sync-once        # what it did

It watches the app's status until both cameras' pictures have been clear (not washed out, not dark, not night) for
STEADY_S, inside the allowed hours, then stops the app, runs the calibration (the PTZ steps over a grid for about
3 minutes) and starts the app again, whatever happened. It gives up after MAX_WAIT_S. Run as root (it stops and
starts the service). Its progress goes to data/sync_pending.json, which the app shows in Settings (Camera sync);
the file is removed once the cameras are synced, and says why if it failed.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STEADY_S = 5 * 60  # long enough to rule out a passing break in the clouds
MAX_WAIT_S = 7 * 24 * 3600  # long enough to wait for a camera to be moved so the two pictures match
PREFLIGHT_EVERY_S = 10 * 60  # while they do not, look again this often (the app keeps running meanwhile)
ATTEMPTS = 3  # a failed calibration (the PTZ dropped off the network, a cloud mid-sweep) is tried again...
RETRY_GAP_S = 30 * 60  # ...after this, at the next clear moment
# Local hours (Pi clock) to consider at all. The picture checks below are what matter: this window faces the
# morning sun, which washes the PTZ out from about 7:00 (2026-10-31), so the early morning is often the best time.
HOURS = (5, 18)
POLL_S = 30
IDLE_WAIT_S = 3 * 60  # before matching, wait this long at most for the PTZ to stop following and be back Home
IDLE_POLL_S = 5


def data_dir() -> Path:
    try:
        d = Path(json.loads((ROOT / "config.json").read_text()).get("storage", {}).get("data_dir") or "data")
    except (OSError, ValueError, AttributeError):
        d = Path("data")
    return d if d.is_absolute() else ROOT / d


PENDING = data_dir() / "sync_pending.json"
STARTED = time.time()
LAST_ERROR = None  # why the previous attempt stopped, shown while waiting for the next


def report(state: str, **extra):
    """Progress for the app's Settings page (world-readable: the app runs as another user)."""
    try:
        tmp = PENDING.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "state": state, "since": STARTED, "need": STEADY_S,
                                   "updated": time.time(), **extra}))
        os.chmod(tmp, 0o644)
        tmp.replace(PENDING)
    except OSError as e:
        print(f"cannot write {PENDING}: {e}", flush=True)


def clear(c: dict) -> bool:
    # Broad burnt-white areas are what spoil matching (failover calls 50 % washed out); a bright picture with a
    # few percent of glare (a car's windscreen, a patch of sky, which come and go as the PTZ follows someone) is fine.
    return (c.get("online") and not c.get("blinded") and not c.get("night") and (c.get("white") or 0) < 5.0
            and 60 < (c.get("luma") or 0) < 230)


def status():
    with urllib.request.urlopen("http://127.0.0.1:8080/api/status", timeout=5) as r:
        return json.load(r)


def ptz_idle(st: dict) -> bool:
    """The PTZ sits still at Home: not following anything, not moving, not paused by a manual move."""
    p = st.get("ptz", {})
    return p.get("target_id") is None and not p.get("moving") and not p.get("paused_for")


def wait_ptz_idle(limit_s: float = IDLE_WAIT_S) -> bool:
    """Until the PTZ has been idle on two polls in a row. When the pictures turn clear it may be following someone,
    zoomed in, and that picture would not be found in the eye's: a false "no match" costs a quarter of an hour
    of a morning window that lasts about one. False if it stayed busy for limit_s."""
    end, calm = time.time() + limit_s, 0
    while time.time() < end:
        try:
            calm = calm + 1 if ptz_idle(status()) else 0
        except Exception:  # the app restarting
            calm = 0
        if calm >= 2:
            return True
        time.sleep(IDLE_POLL_S)
    return False


def preflight() -> bool:
    """Whether the PTZ's live picture is found in the eye's (the calibration's first step), checked on the running
    app's own pictures, so the app is only stopped when the calibration can work."""
    sys.path.insert(0, str(ROOT))
    import cv2
    import numpy as np
    from ptzcam.syncal import match_views
    if not wait_ptz_idle():
        print(f"preflight: the PTZ stayed busy for {IDLE_WAIT_S // 60} min; matching its picture anyway", flush=True)
    try:
        pics = []
        for cam in ("ptz", "eye"):
            with urllib.request.urlopen(f"http://127.0.0.1:8080/api/frame.jpg?camera={cam}&overlay=0&width=1920",
                                        timeout=10) as r:
                pics.append(cv2.imdecode(np.frombuffer(r.read(), np.uint8), cv2.IMREAD_COLOR))
        return match_views(*pics) is not None
    except Exception as e:
        print(f"preflight failed: {e}", flush=True)
        return False


NO_MATCH = ("the PTZ's view is not found in the wide camera's picture: the wide camera must see the same street "
            "clearly (not through an insect screen, and not mostly sky)")


def wait_until_clear(start):
    """Until both pictures have been clear for STEADY_S inside HOURS. Returns the time they became clear, or None
    when MAX_WAIT_S (from `start`) ran out."""
    since = None
    while time.time() - start < MAX_WAIT_S:
        try:
            cams = status().get("cameras", {})
            ok = "eye" in cams and clear(cams["ptz"]) and clear(cams["eye"]) \
                and HOURS[0] <= time.localtime().tm_hour < HOURS[1]
        except Exception as e:  # the app restarting
            print(f"status unavailable: {e}", flush=True)
            ok = False
        since = (since or time.time()) if ok else None
        if since is not None and time.time() - since >= STEADY_S:
            return since
        report("waiting", clear_for=round(time.time() - since) if since is not None else 0, error=LAST_ERROR)
        time.sleep(POLL_S)
    return None


def calibrate() -> int:
    """Stop the app, run the calibration, start the app again whatever happened. Returns its exit code."""
    report("syncing")
    r = None
    try:
        subprocess.run(["systemctl", "stop", "ptzcam"], check=True)
        r = subprocess.run([str(ROOT / ".venv/bin/python"), str(ROOT / "tools/calibrate_sync.py")], cwd=ROOT)
        print(f"calibration exit code {r.returncode}", flush=True)
    finally:
        subprocess.run(["rm", "-rf", str(ROOT / "ptzcam" / "__pycache__")])
        subprocess.run(["systemctl", "start", "ptzcam"], check=False)
    return r.returncode if r is not None else -1


def main():
    global LAST_ERROR
    start = time.time()
    attempt = 0
    while attempt < ATTEMPTS:
        since = wait_until_clear(start)
        if since is None:
            report("failed", error=LAST_ERROR or f"the pictures were never clear for {STEADY_S // 60} minutes in "
                                                 f"{MAX_WAIT_S // 3600} hours")
            sys.exit("never clear (or never matching) for long enough: nothing done")
        if not preflight():  # not an attempt: the app keeps running, and it looks again later
            LAST_ERROR = NO_MATCH
            print(f"preflight: no match ({time.strftime('%H:%M')}); looking again in {PREFLIGHT_EVERY_S // 60} min",
                  flush=True)
            until = time.time() + PREFLIGHT_EVERY_S
            while time.time() < until:
                report("waiting", clear_for=0, error=LAST_ERROR)
                time.sleep(POLL_S)
            continue
        attempt += 1
        print(f"clear since {time.strftime('%H:%M', time.localtime(since))}: syncing the cameras "
              f"(attempt {attempt} of {ATTEMPTS})", flush=True)
        code = calibrate()
        if code == 0:
            PENDING.unlink(missing_ok=True)
            sys.exit(0)
        if attempt == ATTEMPTS:
            report("failed", error=f"the calibration stopped {ATTEMPTS} times (last exit code {code}); "
                                   "see journalctl -u ptzcam-sync-once")
            sys.exit(code)
        LAST_ERROR = f"attempt {attempt} of {ATTEMPTS} stopped (exit code {code})"
        until = time.time() + RETRY_GAP_S
        while time.time() < until:  # keep the progress fresh (the app drops it after 10 minutes without news)
            report("waiting", clear_for=0, error=LAST_ERROR)
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
