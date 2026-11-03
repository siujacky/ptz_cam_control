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
STEADY_S = 10 * 60
MAX_WAIT_S = 48 * 3600
ATTEMPTS = 3  # a failed calibration (the PTZ dropped off the network, a cloud mid-sweep) is tried again...
RETRY_GAP_S = 30 * 60  # ...after this, at the next clear moment
HOURS = (7, 16)  # local hours in which the street is lit but the sun is not yet low in the window
POLL_S = 30


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
    return (c.get("online") and not c.get("blinded") and not c.get("night") and (c.get("white") or 0) < 2.0
            and 60 < (c.get("luma") or 0) < 200)


def status():
    with urllib.request.urlopen("http://127.0.0.1:8080/api/status", timeout=5) as r:
        return json.load(r)


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
    for attempt in range(1, ATTEMPTS + 1):
        since = wait_until_clear(start)
        if since is None:
            report("failed", error=f"the pictures were never clear for {STEADY_S // 60} minutes in "
                                   f"{MAX_WAIT_S // 3600} hours")
            sys.exit("never clear for long enough: nothing done")
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
