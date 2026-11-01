"""Subprocess helpers for the ffmpeg workers."""
from __future__ import annotations

import subprocess
import threading
import time


def spawn(cmd, log, name, stdout=subprocess.DEVNULL):
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.PIPE, bufsize=0)
    threading.Thread(target=_drain, args=(proc.stderr, log, name), name=f"{name}-stderr", daemon=True).start()
    return proc


def _drain(stream, log, name):
    """Log ffmpeg's stderr, at most one line per 5 s so a flapping stream cannot flood the log."""
    last, suppressed = 0.0, 0
    for raw in iter(stream.readline, b""):
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        now = time.monotonic()
        if now - last >= 5:
            log.warning("%s: %s%s", name, line, f" (+{suppressed} more)" if suppressed else "")
            last, suppressed = now, 0
        else:
            suppressed += 1
    stream.close()


def terminate(proc, timeout=3.0):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(2)
