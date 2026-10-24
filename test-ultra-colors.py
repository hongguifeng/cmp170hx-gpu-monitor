"""Ultra mode shows two figures per GPU and nothing else -- no index.

What survives on the single line is the memory figure (brightest ink, bold) and
the power figure (the amber the power chart uses). The line used to lead with a
per-GPU index digit painted in that GPU's own colour; the index is gone, so no
blue or green may appear on the line any more, and a cell must lead with the
memory figure instead of an index standing in front of it.

Two things are checked by pixels and one is not, because colour alone cannot
settle it:

* the memory figure (FG white) and the power figure (PW_COLOR amber) must both
  be present in every cell -- a cell that painted nothing is a bug;
* the two separable index inks (blue, green) must be absent;
* the old third index amber sits ~39 from the power amber and the pale mem-temp
  inks sit ~15 from ClearType's fringe on amber glyphs, so no pixel test can
  tell them apart from the figures. test-geometry.py [8] proves the index label
  does not exist any more, which is the stronger statement anyway.

The window is built in ultra mode from the start (load_mode is asked before the
app exists) and the readings go in through paint_card, because PrintWindow on a
root that has just been switched between layouts has repeatedly caught a frame
that was never drawn.
"""
import ctypes
import importlib.util
import sys
import tkinter as tk

spec = importlib.util.spec_from_file_location("gmt", "gpu-monitor-tray.py")
gmt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gmt)
sys.excepthook = sys.__excepthook__

import win32gui
import win32ui
from PIL import Image, ImageGrab

user32 = ctypes.WinDLL("user32")
GA_ROOT = 2

N = 3
FAKE = {"name": "NVIDIA CMP 170HX", "temp": 62, "mtemp": 54, "power": 250,
        "power_limit": 260, "mem_used": 11, "mem_total": 12,
        "clocks": {"gpu": 1350, "mem": 810}, "util": 100, "fan": 55}
# 9pt glyphs are mostly blend plus ClearType fringe, so "present" needs a wide
# net (90) and "absent" a tight one (30). Measured on this machine: the fringe
# of the amber digits reaches 10 pixels within 45 of the index blue, none
# within 30.
PRESENT_TOL = 90
ABSENT_TOL = 30
PRESENT_MIN = 10
MUST_BE_THERE = {"memory figure": gmt.FG, "power figure": gmt.PW_COLOR}
MUST_BE_GONE = {"index blue": gmt.GRAPH_TEMP, "index green": "#3ddc84"}


class FakeTray:
    visible = True
    icon = None


def rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def count_near(im, hexcode, tol):
    c = rgb(hexcode)
    return sum(1 for px in im.getdata() if dist(px, c) <= tol)


def print_window(root):
    for _ in range(8):
        root.update_idletasks()
        root.update()
    hwnd = user32.GetAncestor(root.winfo_id(), GA_ROOT)
    rect = gmt.window_rect(hwnd)
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    dc = win32gui.GetWindowDC(hwnd)
    src = win32ui.CreateDCFromHandle(dc)
    mdc = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(src, w, h)
    mdc.SelectObject(bmp)
    user32.PrintWindow(hwnd, mdc.GetSafeHdc(), 2)
    info = bmp.GetInfo()
    img = Image.frombuffer("RGBA", (info["bmWidth"], info["bmHeight"]),
                           bmp.GetBitmapBits(True), "raw", "BGRA", 0,
                           1).convert("RGB")
    win32gui.DeleteObject(bmp.GetHandle())
    mdc.DeleteDC()
    src.DeleteDC()
    win32gui.ReleaseDC(hwnd, dc)
    return img, rect


gmt.NVML_STATE["ngpu"] = N
gmt.load_mode = lambda: gmt.MODE_ULTRA
root = tk.Tk()
app = gmt.MonitorApp(root, tray_icon=FakeTray())
sys.excepthook = sys.__excepthook__
for aid in root.tk.call("after", "info"):
    root.after_cancel(aid)
app.mode = gmt.MODE_ULTRA
root.geometry("+120+300")
for i in range(N):
    app.paint_card(i, dict(FAKE, power=250 + i, mem_used=11 + i))
root.update()

img, rect = print_window(root)
grab = ImageGrab.grab(bbox=rect, all_screens=True).convert("RGB")


# Whichever capture actually shows the line (the card background) wins.
def painted(im):
    return sum(1 for px in im.getdata() if px == rgb(gmt.CARD))


which = "PrintWindow" if painted(img) >= painted(grab) else "screen grab"
print("using %s (%d card pixels vs %d)" % (which, painted(img), painted(grab)))
img = img if which == "PrintWindow" else grab

row = app.ultra_row
x = row.winfo_rootx() - rect[0]
y = row.winfo_rooty() - rect[1]
crop = img.crop((x, y, x + row.winfo_width(), y + row.winfo_height()))
crop.save("shot-ultra-crop.png")
img.save("shot-ultra.png")

failures = []
for label, hexcode in MUST_BE_GONE.items():
    n = count_near(crop, hexcode, ABSENT_TOL)
    print("%-12s in the whole row: %d" % (label, n))
    if n:
        failures.append("%s (%s) still painted on the line (%d pixels)"
                        % (label, hexcode, n))

for i in range(N):
    cell = app.cards[i]["cell"]
    cx = cell.winfo_rootx() - rect[0]
    cy = cell.winfo_rooty() - rect[1]
    sub = img.crop((cx, cy, cx + cell.winfo_width(), cy + cell.winfo_height()))
    ink = sum(1 for px in sub.getdata() if dist(px, rgb(gmt.CARD)) >= 80)
    got = {name: count_near(sub, hexcode, PRESENT_TOL)
           for name, hexcode in MUST_BE_THERE.items()}
    # the leftmost glyph in the cell: an index would stand here, the memory
    # figure starts 6px in (and Tk's own ~3px bearing on top of that)
    first = next((col for col in range(sub.width)
                  if any(dist(sub.getpixel((col, r)), rgb(gmt.CARD)) >= 80
                         for r in range(sub.height))), None)
    print("cell %d at +%d: ink=%d, %s" % (i, -1 if first is None else first,
                                          ink, got))
    if ink < 20:
        failures.append("cell %d painted nothing (%d ink pixels)" % (i, ink))
    for name, n in got.items():
        if n < PRESENT_MIN:
            failures.append("cell %d: %s missing (%d px, want %d)"
                            % (i, name, n, PRESENT_MIN))
    # An index in front of the figures pushed them ~23px into the cell.
    if first is None or first > 12:
        failures.append("cell %d: first figure starts %s px in, something sits "
                        "in front of it" % (i, first))

root.destroy()
print("FAILURES:", failures)
print("ALL OK" if not failures else "PROBLEM")
sys.exit(1 if failures else 0)
