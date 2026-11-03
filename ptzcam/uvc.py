"""USB (UVC) camera controls, such as anti-flicker (`power_line_frequency`), through plain V4L2 ioctls.

Only control requests are sent, so they work while another process streams the camera, and v4l-utils is not
needed. tools/uvc_controls.py is the command line for these.
"""
from __future__ import annotations

import errno
import fcntl
import os
import struct
from pathlib import Path

SYSFS = Path("/sys/class/video4linux")
VIDIOC_QUERYCTRL = 0xC0445624
VIDIOC_G_CTRL = 0xC008561B
VIDIOC_S_CTRL = 0xC008561C
VIDIOC_QUERYMENU = 0xC02C5625
NEXT_CTRL = 0x80000000
QFMT = "II32siiiiI8x"  # struct v4l2_queryctrl
MFMT = "II32sI"  # struct v4l2_querymenu
REFUSED = {errno.EINVAL: "that value or mode is not supported",
           errno.EACCES: "the control is inactive in the current mode, e.g. exposure time while exposure is automatic",
           errno.ERANGE: "out of range"}


def find_device(name: str | None = None) -> str:
    """The picture node (index 0; index 1 is metadata) of the first UVC camera whose name contains `name` (any
    case), or `name` itself if it is an absolute path such as /dev/v4l/by-id/... Node numbers can change after a
    reboot or re-plug, so pick cameras by name or by-id path. Raises LookupError if there is no such camera."""
    if name and name.startswith("/"):
        if not os.path.exists(name):
            raise LookupError(f"{name} does not exist: is the camera's USB cable connected to the Pi?")
        return name
    found = []
    for node in sorted(SYSFS.glob("video*"), key=lambda p: int(p.name[5:])):
        try:
            if (node / "device" / "driver").resolve().name != "uvcvideo" or (node / "index").read_text().strip() != "0":
                continue
            cam = (node / "name").read_text().strip()
        except OSError:  # unplugged while we looked
            continue
        if name is None or name.lower() in cam.lower():
            return f"/dev/{node.name}"
        found.append(cam)
    if found:
        raise LookupError(f"no USB camera named {name!r}; connected: {', '.join(found)}")
    raise LookupError("no USB camera found: is the camera's USB cable connected to the Pi?")


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


def find_control(ctrls, ref: str) -> dict:
    """The control called `ref` (any spelling key() maps to the same name), or with that id (decimal or hex)."""
    for c in ctrls:
        if key(c["name"]) == key(ref) or ref.lower() in (str(c["id"]), hex(c["id"])):
            return c
    raise LookupError(f"no control {ref!r}")


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


def read_control(dev: str, name: str) -> int:
    """The value of control `name` (e.g. 'power_line_frequency') on the video node `dev`."""
    fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    try:
        return get(fd, find_control(controls(fd), name)["id"])
    finally:
        os.close(fd)


def write_control(dev: str, name: str, value: int):
    """Set control `name` on `dev`; returns (old, new) as read back. A refused value raises OSError (REFUSED says
    why for the usual errnos), a missing control LookupError."""
    fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    try:
        cid = find_control(controls(fd), name)["id"]
        old = get(fd, cid)
        set_(fd, cid, int(value))
        return old, get(fd, cid)
    finally:
        os.close(fd)
