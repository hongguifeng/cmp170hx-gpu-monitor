"""Capture the live GPU Monitor window with PrintWindow and save it.

    python shot-window.py docs/img/window-compact.png 440 137

PrintWindow rather than a screen grab because the widget is layered and a
screen grab keeps a ghost of the previous frame after a resize.
"""
import ctypes
import sys

import win32gui
import win32ui

user32 = ctypes.WinDLL("user32")
out, want_w, want_h = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])

found = []
win32gui.EnumWindows(
    lambda h, _: found.append(h)
    if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) == "GPU Monitor"
    else None,
    None)
if not found:
    sys.exit("no GPU Monitor window")
hwnd = found[0]
rect = win32gui.GetWindowRect(hwnd)
w, h = rect[2] - rect[0], rect[3] - rect[1]
if (w, h) != (want_w, want_h):
    sys.exit("window is %dx%d at %s, wanted %dx%d" % (w, h, rect, want_w, want_h))

from PIL import Image
dc = win32gui.GetWindowDC(hwnd)
src = win32ui.CreateDCFromHandle(dc)
mdc = src.CreateCompatibleDC()
bmp = win32ui.CreateBitmap()
bmp.CreateCompatibleBitmap(src, w, h)
mdc.SelectObject(bmp)
ok = user32.PrintWindow(hwnd, mdc.GetSafeHdc(), 2)
info = bmp.GetInfo()
img = Image.frombuffer("RGBA", (info["bmWidth"], info["bmHeight"]),
                       bmp.GetBitmapBits(True), "raw", "BGRA", 0, 1).convert("RGB")
win32gui.DeleteObject(bmp.GetHandle())
mdc.DeleteDC()
src.DeleteDC()
win32gui.ReleaseDC(hwnd, dc)
img.save(out)
print("PrintWindow %s -> %s (%dx%d)" % (ok, out, w, h))
