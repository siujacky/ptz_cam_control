"""Read and set the camera's USB (UVC) picture controls, such as anti-flicker (`power_line_frequency`).

    .venv/bin/python tools/uvc_controls.py                                 every control: range, default, value
    .venv/bin/python tools/uvc_controls.py --camera unite get power_line_frequency
    .venv/bin/python tools/uvc_controls.py --camera unite set power_line_frequency=0      0 = off, 1 = 50 Hz, 2 = 60 Hz

The camera's USB port must be connected to the Pi. --camera picks a camera by name (node numbers can change after a
reboot or re-plug); without it the first USB camera is used, or pass --device /dev/videoN. A refused value is
reported and the exit code is 1. Only control requests are sent: nothing is streamed over USB, and the app keeps
using the camera's network stream. The camera has no exposure, shutter or gain control here (nor over VISCA, its
web page or ONVIF); anti-flicker is the one exposure-related setting. A camera restart may put values back.
Plain ioctls, so v4l-utils is not needed.
"""
import argparse
import errno
import fcntl
import os
import struct
import sys
from pathlib import Path

VIDIOC_QUERYCTRL = 0xC0445624
VIDIOC_G_CTRL = 0xC008561B
VIDIOC_S_CTRL = 0xC008561C
VIDIOC_QUERYMENU = 0xC02C5625
NEXT_CTRL = 0x80000000
QFMT = "II32siiiiI8x"  # struct v4l2_queryctrl
MFMT = "II32sI"  # struct v4l2_querymenu
TYPES = {1: "int", 2: "bool", 3: "menu", 4: "button", 5: "int64", 6: "class", 7: "string", 8: "bitmask",
         9: "intmenu"}
FLAGS = {0x1: "disabled", 0x4: "read-only", 0x10: "inactive", 0x40: "write-only", 0x80: "volatile"}
REFUSED = {errno.EINVAL: "that value or mode is not supported",
           errno.EACCES: "the control is inactive in the current mode, e.g. exposure time while exposure is automatic",
           errno.ERANGE: "out of range"}


def find_device(name: str | None = None) -> str:
    """The picture node (index 0; index 1 is metadata) of the first UVC camera whose name contains `name`.
    Node numbers can change after a reboot or re-plug, so pick cameras by name when more than one is connected."""
    found = []
    for node in sorted(Path("/sys/class/video4linux").glob("video*"), key=lambda p: int(p.name[5:])):
        driver = (node / "device" / "driver").resolve().name
        if driver == "uvcvideo" and (node / "index").read_text().strip() == "0":
            cam = (node / "name").read_text().strip()
            if name is None or name.lower() in cam.lower():
                return f"/dev/{node.name}"
            found.append(cam)
    if found:
        sys.exit(f"no USB camera named {name!r}; connected: {', '.join(found)}")
    sys.exit("no USB camera found: is the camera's USB cable connected to the Pi?")


def controls(fd):
    out, cid = [], NEXT_CTRL
    while True:
        buf = bytearray(struct.pack(QFMT, cid, 0, b"", 0, 0, 0, 0, 0))
        try:
            fcntl.ioctl(fd, VIDIOC_QUERYCTRL, buf)
        except OSError:
            return out
        i, t, name, mn, mx, st, df, fl = struct.unpack(QFMT, buf)
        out.append({"id": i, "type": t, "name": name.split(b"\0")[0].decode(), "min": mn, "max": mx, "step": st,
                    "default": df, "flags": fl})
        cid = i | NEXT_CTRL


def key(name: str) -> str:
    """'Power Line Frequency' -> 'power_line_frequency', the names v4l2-ctl uses."""
    return "_".join("".join(ch if ch.isalnum() else " " for ch in name.lower()).split())


def menu(fd, c) -> dict:
    items = {}
    for idx in range(c["min"], c["max"] + 1):
        buf = bytearray(struct.pack(MFMT, c["id"], idx, b"", 0))
        try:
            fcntl.ioctl(fd, VIDIOC_QUERYMENU, buf)
        except OSError:
            continue
        raw = struct.unpack(MFMT, buf)[2]
        items[idx] = raw.split(b"\0")[0].decode() if c["type"] == 3 else struct.unpack("<q", raw[:8])[0]
    return items


def get(fd, cid: int) -> int:
    buf = bytearray(struct.pack("Ii", cid, 0))
    fcntl.ioctl(fd, VIDIOC_G_CTRL, buf)
    return struct.unpack("Ii", buf)[1]


def set_(fd, cid: int, value: int):
    fcntl.ioctl(fd, VIDIOC_S_CTRL, bytearray(struct.pack("Ii", cid, value)))


def find(ctrls, ref: str):
    for c in ctrls:
        if key(c["name"]) == key(ref) or ref.lower() in (str(c["id"]), hex(c["id"])):
            return c
    sys.exit(f"no control {ref!r}; run without arguments to list them")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--camera", help="pick the USB camera whose name contains this, e.g. 'unite'")
    ap.add_argument("--device", help="video node, e.g. /dev/video0 (default: the first USB camera)")
    ap.add_argument("action", nargs="?", choices=("list", "get", "set"), default="list")
    ap.add_argument("items", nargs="*", help="control names for get, NAME=VALUE for set")
    args = ap.parse_args()
    dev = args.device or find_device(args.camera)
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
