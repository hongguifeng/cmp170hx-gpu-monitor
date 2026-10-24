"""Capture the live full-mode window for docs/img/window-full.png.

Finds the running GPU Monitor window, renders it with PrintWindow (no
layered-window ghosting), checks that every chart band uses the shared inks and
that none of the old per-GPU curve colours survives there. Each card now holds
two charts stacked: the upper one carries the power draw, the lower one the two
temperature series -- so each band is checked on its own, and the ink that must
not appear there is as important as the ink that must.
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

# Where the two charts of each card land in the 492px window: the power chart
# runs 264..354 and the temperature chart 360..484, both 250px wide, and the
# columns start at 8 / 266 / 528 (card width 250 plus its own padding).
TEMP_X = [8, 266, 528]
PW_BOX = (264, 354)
TEMP_BOX = (360, 484)
TEMP_WANT = {"core blue": 20, "mem pale blue": 20}
PW_WANT = {"power amber": 20}
# the other unit's ink must stay out of the chart it does not belong to
TEMP_OUT = {"power amber": 25}
PW_OUT = {"core blue": 25, "mem pale blue": 25}


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

# Three fixed columns; the two chart bands of each card sit at the same place.
BANDS = [(x, PW_BOX, "power", PW_WANT, PW_OUT) for x in TEMP_X] \
    + [(x, TEMP_BOX, "temps", TEMP_WANT, TEMP_OUT) for x in TEMP_X]
failures = []
for i, (x, box, kind, want, out) in enumerate(BANDS):
    card = i % len(TEMP_X)
    crop = img.crop((x, box[0], x + 250, box[1]))
    near, exact_old = {}, {}
    for px in crop.getdata():
        hh = hexpx(px)
        if hh in OLD_CURVE_INKS:
            exact_old[hh] = exact_old.get(hh, 0) + 1
        for name, colour in UNIFIED.items():
            if dist(px, rgb(colour)) <= 60:
                near[name] = near.get(name, 0) + 1
    print("card %d %s band x=%d y=%d..%d" % (card, kind, x, box[0], box[1]))
    print("  unified:", near)
    print("  old curve inks:", exact_old)
    for name, floor in want.items():
        if near.get(name, 0) < floor:
            failures.append("card %d %s band shows almost no %s"
                            % (card, kind, name))
    for name, ceiling in out.items():
        if near.get(name, 0) > ceiling:
            failures.append("card %d %s band draws in %s"
                            % (card, kind, name))
    if exact_old:
        failures.append("card %d %s band still draws in %s"
                        % (card, kind, sorted(exact_old)))

img.save("shot-live-full.png")
print("saved shot-live-full.png")
print("FAILURES:", failures)
print("ALL OK" if not failures else "PROBLEM")
sys.exit(1 if failures else 0)
