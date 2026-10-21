"""Full mode draws two stacked charts per GPU, and nothing gets clipped.

Checks the geometry the split relies on: the window stays exactly as tall as it
was, the two canvases fill the band that used to hold one plus its legend, the
text card above them still gets the height it asks for, both charts share one x
mapping, the axis numbers stay out of the plot, and an empty history draws a box
without lying about its scale. Colours are test-colors.py's job.
"""
import ctypes
import importlib.util
import math
import sys
import tkinter as tk
import tkinter.font as tkfont

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
     lambda j: 250 - 30 * math.sin(j / 14.0)),
]


class FakeTray:
    visible = True
    icon = None


def rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def hexpx(p):
    return "#%02x%02x%02x" % p


def rooty(w, root):
    return w.winfo_rooty() - root.winfo_rooty()


def rootx(w, root):
    return w.winfo_rootx() - root.winfo_rootx()


INKS = {"temp": "#4f8cff", "mem": "#9cc8ff", "power": "#e8a23c",
        "time": "#9aa3b2"}


def ink_count(crop, names):
    """How many pixels of this picture are drawn in one of the named inks."""
    out = {}
    for name in names:
        want = rgb(INKS[name])
        n = sum(1 for p in crop.getdata() if dist(p, want) <= 60)
        if n:
            out[name] = n
    return out


def shoot(root, before=None, tries=3):
    """PrintWindow the toplevel into a PIL image; returns (image, origin).

    Tk paints lazily, so a single capture can come back with a canvas that was
    never flushed; shoot several times and keep the last picture.
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


ok = True
root = tk.Tk()
app = gmt.MonitorApp(root, tray_icon=FakeTray())
sys.excepthook = sys.__excepthook__
for aid in root.tk.call("after", "info"):
    root.after_cancel(aid)          # no live tick: synthetic data only

app.mode = gmt.MODE_FULL
app.ui_ngpu = N
app.build_cards(N)
app.apply_geometry()
root.geometry("+140+140")
for j in range(gmt.HIST):
    for i, (ft, fm, fp) in enumerate(SHAPE):
        app.hist[i].append(round(ft(j), 1))
        app.mhist[i].append(round(fm(j), 1))
        app.phist[i].append(round(fp(j), 1))
for i in range(N):
    app.cards[i]["info"].config(text=("频率  1350 / 810 MHz\n"
                                      "占用  100 %      风扇  55 %\n"
                                      "显存  11 / 12 GB\n"
                                      "存温  54 °C\n"
                                      "功耗  250 / 260 W"))
    app.cards[i]["name"].config(text="GPU %d · CMP 170HX" % i)
    app.cards[i]["temp"].config(text="62°C")
root.update()

# [1] the split must not change the window: same width, same 492 height
w, h = root.winfo_width(), root.winfo_height()
want = ((gmt.FULL_CARD_W + gmt.FULL_CARD_PAD) * N, gmt.FULL_H)
print("[1] window %dx%d, want %dx%d" % (w, h, want[0], want[1]))
ok &= (w, h) == want

# [2] the two charts sit one above the other and fill the whole band
for i in range(N):
    cv, pcv = app.canvases[i], app.pw_canvases[i]
    col = cv.master
    print("[2] card %d: temp chart x=%d %dx%d y=%d..%d | power y=%d..%d | col y=%d..%d"
          % (i, rootx(cv, root), cv.winfo_width(), cv.winfo_height(),
             rooty(cv, root), rooty(cv, root) + cv.winfo_height(),
             rooty(pcv, root), rooty(pcv, root) + pcv.winfo_height(),
             rooty(col, root), rooty(col, root) + col.winfo_height()))
    ok &= (cv.winfo_width(), cv.winfo_height()) == (gmt.FULL_CARD_W,
                                                    gmt.GRAPH_H_TEMP)
    ok &= (pcv.winfo_width(), pcv.winfo_height()) == (gmt.FULL_CARD_W,
                                                     gmt.GRAPH_H_PW)
    ok &= rooty(cv, root) + cv.winfo_height() + gmt.GRAPH_GAP == rooty(pcv, root)
    ok &= (rooty(pcv, root) + pcv.winfo_height()
           == rooty(col, root) + col.winfo_height())     # flush with the bottom
    ok &= cv.winfo_width() == pcv.winfo_width()          # one shared x mapping

# [3] the text card above them is not clipped
for i in range(N):
    card = app.cards[i]["info"].master
    info = app.cards[i]["info"]
    print("[3] card %d: card %dpx, content asks %dpx, slack %d"
          % (i, card.winfo_height(), card.winfo_reqheight(),
             card.winfo_height() - card.winfo_reqheight()))
    ok &= card.winfo_height() >= card.winfo_reqheight()
    ok &= (rooty(info, root) + info.winfo_height()
           <= rooty(card, root) + card.winfo_height())

img, rect = shoot(root, before=lambda: [app.draw_graph(i) for i in range(N)])

# [4] the axis numbers stay out of the plot: the widest number still fits the
# left margin, which is what pays for putting the watt scale on the left edge
f8 = tkfont.Font(family="Segoe UI", size=8)
widest = {"temp": 0, "power": 0}
for i in range(N):
    thi = max(v for v in app.mhist[i] if v is not None) + 2
    phi = max(v for v in app.phist[i] if v is not None) + 5
    widest["temp"] = max(widest["temp"], f8.measure("%d°" % round(thi)))
    widest["power"] = max(widest["power"], f8.measure("%dW" % round(phi)))
for name, need in widest.items():
    print("[4] %s axis: widest number %dpx starting at x=4, margin %d"
          % (name, need, gmt.GRAPH_PAD_L))
    ok &= 4 + need <= gmt.GRAPH_PAD_L

# [5] the key line of each chart sits inside its own top strip and apart
f9 = tkfont.Font(family="Segoe UI", size=9)
key_w = f9.measure("— 核温")
print("[5] key %dpx wide, gap to the second one %d" % (key_w, gmt.GRAPH_KEY_GAP))
ok &= key_w <= gmt.GRAPH_KEY_GAP
ok &= gmt.GRAPH_KEY_Y + f9.metrics("linespace") / 2 <= gmt.GRAPH_PAD_T

# [5b] the x mapping must put the oldest sample at the plot's left edge and
# must never smear a curve into the axis column. Pale blue only ever comes from
# the mem curve in this chart -- the axis numbers are the other blue and the key
# sits inside the plot -- so its leftmost pixel is where the timeline starts.
for i in range(N):
    cv = app.canvases[i]
    x = cv.winfo_rootx() - rect[0]
    y = cv.winfo_rooty() - rect[1]
    crop = img.crop((x, y, x + cv.winfo_width(), y + cv.winfo_height()))
    want = rgb(gmt.GRAPH_MEM)
    xs = [px for px in range(cv.winfo_width())
          for py in range(cv.winfo_height())
          if dist(crop.getpixel((px, py)), want) <= 60]
    lo, hi = min(xs), max(xs)
    print("[5b] card %d mem curve spans x=%d..%d; plot %d..%d, axis column 0..%d"
          % (i, lo, hi, gmt.GRAPH_PAD_L, cv.winfo_width() - gmt.GRAPH_PAD_R,
             gmt.GRAPH_PAD_L - 2))
    ok &= lo >= gmt.GRAPH_PAD_L - 2       # never in the axis numbers
    ok &= hi <= cv.winfo_width() - gmt.GRAPH_PAD_R + 2
    ok &= hi - lo >= (cv.winfo_width() - gmt.GRAPH_PAD_L - gmt.GRAPH_PAD_R - 4)

# [6] a blind card keeps its box, its key and its time axis, but prints no
# numbers nobody measured: the axis strip left of the plot must stay empty
app.hist = [[] for _ in range(N)]
app.mhist = [[] for _ in range(N)]
app.phist = [[] for _ in range(N)]
img2, rect2 = shoot(root, before=lambda: [app.draw_graph(i) for i in range(N)])
for i in range(N):
    for kind, cv, ink in (("temp", app.canvases[i], "temp"),
                          ("power", app.pw_canvases[i], "power")):
        x = cv.winfo_rootx() - rect2[0]
        y = cv.winfo_rooty() - rect2[1]
        crop = img2.crop((x, y, x + cv.winfo_width(), y + cv.winfo_height()))
        axis = img.crop((cv.winfo_rootx() - rect[0], cv.winfo_rooty() - rect[1],
                         cv.winfo_rootx() - rect[0] + gmt.GRAPH_PAD_L,
                         cv.winfo_rooty() - rect[1] + cv.winfo_height()))
        blind_axis = crop.crop((0, 0, gmt.GRAPH_PAD_L, cv.winfo_height()))
        frame = sum(1 for p in crop.getdata() if hexpx(p) == gmt.LINE)
        n_data = sum(ink_count(axis, [ink]).values())
        n_blind = sum(ink_count(blind_axis, [ink]).values())
        n_key = sum(ink_count(crop, [ink]).values())
        print("[6] card %d %s chart: axis ink with data %d, blind %d, "
              "whole-chart ink %d, frame px %d"
              % (i, kind, n_data, n_blind, n_key, frame))
        ok &= n_data > 60                         # the scale is printed
        ok &= n_blind == 0                        # but never invented
        ok &= n_key > 40                          # the key is still there
        ok &= frame > 200                         # the box is still there

img.save("shot-charts-data.png")
img2.save("shot-charts-empty.png")
print("saved shot-charts-data.png / shot-charts-empty.png")

# [7] the other two layouts must still survive the extra list
for mode in (gmt.MODE_COMPACT, gmt.MODE_ULTRA):
    app.mode = mode
    app.build_cards(N)
    app.draw_graph(0)
    print("[7] mode %d: canvases=%s pw=%s" % (mode, app.canvases, app.pw_canvases))
    ok &= all(c is None for c in app.canvases)
    ok &= all(c is None for c in app.pw_canvases)

root.destroy()
print("RESULT:", "ALL OK" if ok else "PROBLEM")
sys.exit(0 if ok else 1)
