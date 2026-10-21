"""Ultra mode keeps its per-GPU index colours (only full-mode graphs were unified).

The single crowded line tells GPUs apart by the index digit alone, so GPU 0 has
to stay blue, GPU 1 green and GPU 2 amber. The pale mem-temp inks, which only
ever existed inside a full-mode graph, must not show up here.
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
from PIL import Image

user32 = ctypes.WinDLL("user32")
GA_ROOT = 2

N = 3
EXPECT = {"gpu0 blue": "#4f8cff", "gpu1 green": "#3ddc84",
          "gpu2 amber": "#ffb020"}
INKS = list(EXPECT.values())          # in GPU order
GONE = {"pale mem 1": "#a8f0c0", "pale mem 2": "#ffd28a",
        "pale mem 3": "#ff9c9c"}


class FakeTray:
    visible = True
    icon = None


def rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


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


root = tk.Tk()
app = gmt.MonitorApp(root, tray_icon=FakeTray())
sys.excepthook = sys.__excepthook__
for aid in root.tk.call("after", "info"):
    root.after_cancel(aid)
app.mode = gmt.MODE_ULTRA
app.ui_ngpu = N
app.build_cards(N)
app.apply_geometry()
root.geometry("+120+300")
for _ in range(6):
    root.update_idletasks()
    root.update()
    root.update()

img, rect = print_window(root)
from PIL import ImageGrab
shot = ImageGrab.grab(bbox=rect, all_screens=True).convert("RGB")

# Whichever capture actually shows the line (the card background) wins.
def painted(im):
    return sum(1 for px in im.getdata() if px == rgb(gmt.CARD))

which = "PrintWindow" if painted(img) >= painted(shot) else "screen grab"
print("using %s (%d card pixels vs %d)" % (which, painted(img), painted(shot)))
img = img if which == "PrintWindow" else shot

row = app.ultra_row
x = row.winfo_rootx() - rect[0]
y = row.winfo_rooty() - rect[1]
crop = img.crop((x, y, x + row.winfo_width(), y + row.winfo_height()))
crop.save("shot-ultra-crop.png")
img.save("shot-ultra.png")

failures = []
# A 9pt digit is mostly anti-aliased blend, so "pixels close to the ink" is the
# wrong measure. Take the real ink in each index label -- pixels that are
# nowhere near the card background -- and ask which candidate colour each one
# is closest to.
for i in range(N):
    lbl = app.cards[i]["name"]
    x = lbl.winfo_rootx() - rect[0]
    y = lbl.winfo_rooty() - rect[1]
    sub = img.crop((x, y, x + lbl.winfo_width(), y + lbl.winfo_height()))
    votes, ink = {}, 0
    for px in sub.getdata():
        if dist(px, rgb(gmt.CARD)) < 80:
            continue                       # plain background, not ink
        ink += 1
        best = min(INKS, key=lambda c: dist(px, rgb(c)))
        votes[best] = votes.get(best, 0) + 1
    share = votes.get(INKS[i], 0) / max(ink, 1)
    print("index %d wants %s: ink=%d votes=%s share=%.2f"
          % (i, INKS[i], ink, votes, share))
    if ink < 20 or share < 0.6:
        failures.append("index %d does not read as %s (%.2f)"
                        % (i, INKS[i], share))

for label, hexcode in GONE.items():
    n = sum(1 for px in crop.getdata() if dist(px, rgb(hexcode)) <= 30)
    print("%-13s %d" % (label, n))
    if n:
        failures.append("%s (%s) still present in the row (%d pixels)"
                        % (label, hexcode, n))

root.destroy()
print("FAILURES:", failures)
print("ALL OK" if not failures else "PROBLEM")
sys.exit(1 if failures else 0)
