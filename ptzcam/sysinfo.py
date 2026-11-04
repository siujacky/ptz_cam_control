"""Host metrics read straight from /proc and /sys (no extra dependencies)."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path


class SysInfo:
    def __init__(self, data_dir: Path, media_dir: Path):
        self.data_dir = Path(data_dir)
        self.media_dir = Path(media_dir)
        self._prev_cpu = None
        self._media = (0.0, 0.0)  # (checked_at, gb)

    def cpu_percent(self) -> float:
        with open("/proc/stat") as f:
            vals = [int(v) for v in f.readline().split()[1:9]]
        idle, total = vals[3] + vals[4], sum(vals)
        pct = 0.0
        if self._prev_cpu:
            d_idle, d_total = idle - self._prev_cpu[0], total - self._prev_cpu[1]
            if d_total > 0:
                pct = 100.0 * (1.0 - d_idle / d_total)
        self._prev_cpu = (idle, total)
        return round(pct, 1)

    @staticmethod
    def cpu_temp():
        try:
            return round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000.0, 1)
        except (OSError, ValueError):
            return None

    @staticmethod
    def mem_percent() -> float:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0])
        return round(100.0 * (1 - info["MemAvailable"] / info["MemTotal"]), 1)

    def disk_free_gb(self) -> float:
        return round(shutil.disk_usage(self.data_dir).free / 1e9, 1)

    def disk_total_gb(self) -> float:
        return round(shutil.disk_usage(self.data_dir).total / 1e9, 1)

    def media_gb(self, max_age=60.0) -> float:
        checked, gb = self._media
        if time.monotonic() - checked > max_age:
            gb = round(dir_size(self.media_dir) / 1e9, 2)
            self._media = (time.monotonic(), gb)
        return gb

    def snapshot(self) -> dict:
        return {
            "cpu_percent": self.cpu_percent(),
            "cpu_temp": self.cpu_temp(),
            "mem_percent": self.mem_percent(),
            "disk_free_gb": self.disk_free_gb(),
            "disk_total_gb": self.disk_total_gb(),
            "media_gb": self.media_gb(),
            "load": [round(v, 2) for v in os.getloadavg()],
        }


def dir_size(path: Path) -> int:
    total = 0
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        total += e.stat(follow_symlinks=False).st_size
        except OSError:
            pass
    return total
