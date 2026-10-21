"""Full-mode graphs must use one shared set of inks for every GPU.

Every chart has to be drawn in #4f8cff (core temp) / #9cc8ff (mem temp) /
#e8a23c (power) and must hold no trace of the old per-GPU palette
(#3ddc84 / #ffb020 / #ff5555 and their pale mem-temp variants). The two charts
stacked under each card are checked apart from each other: the upper one holds
the temperatures and nothing amber, the lower one holds the watts and nothing
blue, which is what proves they really are two separate charts.
"""
import ctypes
import importlib.util
import math
import sys
import tkinter as tk

spec = importlib.util.spec_from_file_location("gmt", "gpu-monitor-tray.py")
gmt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gmt)
sys.excepthook = sys.__excepthook__

import win32gui
import win32ui
from PIL import Image

user32 = ctypes.WinDLL("user32")
GA_ROOT = 2

N = 3
SHAPE = [
    (lambda j: 62 + 6 * math.sin(j / 12.0), lambda j: 71 + 5 * math.sin(j / 9.0),
     lambda j: 150 + 40 * math.sin(j / 15.0)),
    (lambda j: 44 + 4 * math.sin(j / 17.0), lambda j: 52 + 3 * math.sin(j / 11.0),
     lambda j: 105 + 25 * math.sin(j / 13.0)),
    (lambda j: 79 + 7 * math.sin(j / 10.0), lambda j: 86 + 6 * math.sin(j / 8.0),
     lambda j: 220 + 30 * math.sin(j / 14.0)),
]

UNIFIED = {"temp": "#4f8cff", "mem": "#9cc8ff", "pw": "#e8a23c"}
# what each of the two charts must show, and what must stay out of it
EXPECT = {
    "temps": ({"temp": 300, "mem": 300}, {"pw": 20}),
    "power": ({"pw": 200}, {"temp": 20, "mem": 20}),
}
OLD_INKS = ["#3ddc84", "#ffb020", "#ff5555",
            "#a8f0c0", "#ffd28a", "#ff9c9c"]


class FakeTray:
    visible = True
    icon = None


def rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def hexpx(p):
    return "#%02x%02x%02x" % p


def shoot(root, before=None, tries=3):
    """PrintWindow the toplevel into a PIL image; returns (image, origin).

    Tk paints lazily, so a single capture can come back with a canvas that was
    never flushed; shoot twice and keep the second picture.
    """
    for _ in range(6):
        root.update_idletasks()
        root.update()
    if before:
        before()
    for _ in range(3):
        root.update_idletasks()
        root.update()
    last = None
    for _ in range(tries):
        hwnd = user32.GetAncestor(root.winfo_id(), GA_ROOT)
        rect = gmt.window_rect(hwnd)
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        dc = win32gui.GetWindowDC(hwnd)
        src = win32ui.CreateDCFromHandle(dc)
        mdc = src.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(src, w, h)
        mdc.SelectObject(bmp)
        ok = user32.PrintWindow(hwnd, mdc.GetSafeHdc(), 2)
        info = bmp.GetInfo()
        img = Image.frombuffer("RGBA", (info["bmWidth"], info["bmHeight"]),
                               bmp.GetBitmapBits(True), "raw", "BGRA", 0,
                               1).convert("RGB")
        win32gui.DeleteObject(bmp.GetHandle())
        mdc.DeleteDC()
        src.DeleteDC()
        win32gui.ReleaseDC(hwnd, dc)
        last = (img, rect, ok)
        for _ in range(2):
            root.update_idletasks()
            root.update()
    img, rect, ok = last
    print("PrintWindow -> %s, window %dx%d at %d,%d"
          % (ok, rect[2] - rect[0], rect[3] - rect[1], rect[0], rect[1]))
    return img, rect


root = tk.Tk()
app = gmt.MonitorApp(root, tray_icon=FakeTray())
sys.excepthook = sys.__excepthook__
for aid in root.tk.call("after", "info"):
    root.after_cancel(aid)          # no live tick: synthetic data only

app.mode = gmt.MODE_FULL
app.ui_ngpu = N
app.build_cards(N)
app.apply_geometry()
root.geometry("+120+120")
for j in range(gmt.HIST):
    for i, (ft, fm, fp) in enumerate(SHAPE):
        app.hist[i].append(round(ft(j), 1))
        app.mhist[i].append(round(fm(j), 1))
        app.phist[i].append(round(fp(j), 1))

img, rect = shoot(root, before=lambda: [app.draw_graph(i) for i in range(N)])

failures = []
for i in range(N):
    for kind, cv in (("temps", app.canvases[i]), ("power", app.pw_canvases[i])):
        want, off = EXPECT[kind]
        x = cv.winfo_rootx() - rect[0]
        y = cv.winfo_rooty() - rect[1]
        cw, ch = cv.winfo_width(), cv.winfo_height()
        crop = img.crop((x, y, x + cw, y + ch))
        near, exact_old = {}, {}
        for px in crop.getdata():
            h = hexpx(px)
            if h in OLD_INKS:                    # exact: #ffb020 is only ~39
                exact_old[h] = exact_old.get(h, 0) + 1   # away from the amber line
            for name, colour in UNIFIED.items():
                if dist(px, rgb(colour)) <= 60:
                    near[name] = near.get(name, 0) + 1
        print("card %d %s chart at %d,%d %dx%d" % (i, kind, x, y, cw, ch))
        print("  unified inks :", near)
        print("  old per-GPU  :", exact_old)
        for name, floor in want.items():
            if near.get(name, 0) < floor:
                failures.append("card %d %s chart has too little %s" %
                                (i, kind, name))
        for name, ceiling in off.items():
            if near.get(name, 0) > ceiling:
                failures.append("card %d %s chart is drawing in %s" %
                                (i, kind, name))
        if exact_old:
            failures.append("card %d %s chart still draws in %s" %
                            (i, kind, sorted(exact_old)))
        if kind == "temps" and (y + ch > app.pw_canvases[i].winfo_rooty() - rect[1]):
            failures.append("card %d charts overlap" % i)

img.save("shot-full.png")
print("saved shot-full.png")
root.destroy()
print("FAILURES:", failures)
print("ALL OK" if not failures else "PROBLEM")
sys.exit(1 if failures else 0)
