"""Read and set the camera's USB (UVC) picture controls, such as anti-flicker (`power_line_frequency`).

    .venv/bin/python tools/uvc_controls.py                                 every control: range, default, value
    .venv/bin/python tools/uvc_controls.py --camera unite get power_line_frequency
    .venv/bin/python tools/uvc_controls.py --camera unite set power_line_frequency=0      0 = off, 1 = 50 Hz, 2 = 60 Hz

The camera's USB port must be connected to the Pi. --camera picks a camera by name (node numbers can change after a
reboot or re-plug); without it the first USB camera is used, or pass --device /dev/videoN. A refused value is
reported and the exit code is 1. Only control requests are sent: nothing is streamed over USB, and the app keeps
using the camera's network stream. The camera has no exposure, shutter or gain control here (nor over VISCA, its
web page or ONVIF); anti-flicker is the one exposure-related setting. A camera restart may put values back.
Plain ioctls (ptzcam/uvc.py), so v4l-utils is not needed.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ptzcam.uvc import REFUSED, controls, find_control, find_device, get, key, menu, set_  # noqa: E402

TYPES = {1: "int", 2: "bool", 3: "menu", 4: "button", 5: "int64", 6: "class", 7: "string", 8: "bitmask",
         9: "intmenu"}
FLAGS = {0x1: "disabled", 0x4: "read-only", 0x10: "inactive", 0x40: "write-only", 0x80: "volatile"}


def find(ctrls, ref: str):
    try:
        return find_control(ctrls, ref)
    except LookupError:
        sys.exit(f"no control {ref!r}; run without arguments to list them")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--camera", help="pick the USB camera whose name contains this, e.g. 'unite'")
    ap.add_argument("--device", help="video node, e.g. /dev/video0 (default: the first USB camera)")
    ap.add_argument("action", nargs="?", choices=("list", "get", "set"), default="list")
    ap.add_argument("items", nargs="*", help="control names for get, NAME=VALUE for set")
    args = ap.parse_args()
    try:
        dev = args.device or find_device(args.camera)
    except LookupError as e:
        sys.exit(str(e))
    fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    try:
        ctrls = controls(fd)
        if args.action == "list":
            print(dev)
            for c in ctrls:
                if c["type"] == 6:
                    print(f"-- {c['name']}")
                    continue
                flags = ",".join(n for b, n in FLAGS.items() if c["flags"] & b)
                try:
                    cur = get(fd, c["id"])
                except OSError as e:
                    cur = f"? ({e.strerror})"
                extra = f"  {menu(fd, c)}" if c["type"] in (3, 9) else ""
                print(f"{key(c['name']):30s} {TYPES.get(c['type'], c['type']):5s} {c['min']}..{c['max']} "
                      f"default {c['default']} = {cur}{'  [' + flags + ']' if flags else ''}{extra}")
        elif args.action == "get":
            for ref in args.items:
                c = find(ctrls, ref)
                print(f"{key(c['name'])} = {get(fd, c['id'])}")
        else:
            refused = 0
            for item in args.items:
                ref, _, value = item.partition("=")
                c = find(ctrls, ref)
                old = get(fd, c["id"])
                try:
                    set_(fd, c["id"], int(value))
                except OSError as e:
                    refused += 1
                    print(f"{key(c['name'])}: the camera refused {value} ({e.strerror}: {REFUSED.get(e.errno, '')})")
                    continue
                print(f"{key(c['name'])}: {old} -> {get(fd, c['id'])}")
            if refused:
                sys.exit(1)
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
