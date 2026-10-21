"""Capture the live full-mode window for docs/img/window-full.png.

Finds the running GPU Monitor window, renders it with PrintWindow (no
layered-window ghosting), checks that every graph band uses the shared inks and
that none of the old per-GPU curve colours survives there.
"""
import ctypes
import sys

import win32gui
import win32process
import win32ui
from PIL import Image

user32 = ctypes.WinDLL("user32")

UNIFIED = {"core blue": "#4f8cff", "mem pale blue": "#9cc8ff",
           "power amber": "#e8a23c"}
OLD_CURVE_INKS = ["#3ddc84", "#ffb020", "#ff5555",
                  "#a8f0c0", "#ffd28a", "#ff9c9c"]


def rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def hexpx(p):
    return "#%02x%02x%02x" % p


found = []


def cb(hwnd, _):
    if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd) == "GPU Monitor":
        found.append(hwnd)


win32gui.EnumWindows(cb, None)
if not found:
    print("no GPU Monitor window")
    sys.exit(1)
hwnd = found[0]
pid = win32process.GetWindowThreadProcessId(hwnd)[1]
rect = win32gui.GetWindowRect(hwnd)
w, h = rect[2] - rect[0], rect[3] - rect[1]
print("hwnd=%d pid=%d rect=%s size=%dx%d" % (hwnd, pid, rect, w, h))

dc = win32gui.GetWindowDC(hwnd)
src = win32ui.CreateDCFromHandle(dc)
mdc = src.CreateCompatibleDC()
bmp = win32ui.CreateBitmap()
bmp.CreateCompatibleBitmap(src, w, h)
mdc.SelectObject(bmp)
print("PrintWindow ->", user32.PrintWindow(hwnd, mdc.GetSafeHdc(), 2))
info = bmp.GetInfo()
img = Image.frombuffer("RGBA", (info["bmWidth"], info["bmHeight"]),
                       bmp.GetBitmapBits(True), "raw", "BGRA", 0, 1).convert("RGB")
win32gui.DeleteObject(bmp.GetHandle())
mdc.DeleteDC()
src.DeleteDC()
win32gui.ReleaseDC(hwnd, dc)

# Three fixed columns; the graph band of each card sits at the same place.
BANDS = [(8, 290, 258, 460), (266, 290, 516, 460), (528, 290, 778, 460)]
failures = []
for i, box in enumerate(BANDS):
    crop = img.crop(box)
    near, exact_old = {}, {}
    for px in crop.getdata():
        hh = hexpx(px)
        if hh in OLD_CURVE_INKS:
            exact_old[hh] = exact_old.get(hh, 0) + 1
        for name, want in UNIFIED.items():
            if dist(px, rgb(want)) <= 60:
                near[name] = near.get(name, 0) + 1
    print("band %d %s" % (i, box))
    print("  unified:", near)
    print("  old curve inks:", exact_old)
    if near.get("core blue", 0) < 20:
        failures.append("band %d shows almost no core-temp blue" % i)
    if exact_old:
        failures.append("band %d still draws in %s" % (i, sorted(exact_old)))

img.save("shot-live-full.png")
print("saved shot-live-full.png")
print("FAILURES:", failures)
print("ALL OK" if not failures else "PROBLEM")
sys.exit(1 if failures else 0)
