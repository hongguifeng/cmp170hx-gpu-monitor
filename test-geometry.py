# -*- coding: utf-8 -*-
"""Regression test: window size must follow the GPU count.

History: the unlock flow after a reboot tears the NVIDIA driver down and
reinstalls it, so the app can come up with NVML reporting only 1 GPU; the
others return a bit later. The card widgets are rebuilt on that count change,
but the compact window height (derived from the count) and the full window
width (one 250px column per GPU) used to be applied only at startup. A window
sized for 2 cards silently clipped the 3rd one, which looked like "only two
GPUs exist" while the status line already said "NVML . 3 cards".

Run:  python test-geometry.py        (real NVML / real Tk, ASCII output only)

Steps 4-7 also guard the rule that a user-dragged window resizes about its
bottom-right corner, never its top-left, otherwise switching compact <-> full
buries the window under the taskbar or leaves a hole.
"""

import os
import sys
import tempfile
import time
import tkinter as tk
import types

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "gpu-monitor-tray.py")
# Load the shipped file, but redirect its diagnostic log to a temp file: a
# plain import would append bogus "reinit/start" lines to the real monitor.log
# and look like a phantom restart.
_src = open(APP, encoding="utf-8").read()
_src = _src.replace('LOG_PATH = os.path.join(APP_DIR, "monitor.log")',
                    'LOG_PATH = os.environ["GPUMON_TEST_LOG"]')
assert "GPUMON_TEST_LOG" in _src, "LOG_PATH line changed; update test-geometry.py"
os.environ["GPUMON_TEST_LOG"] = os.path.join(tempfile.gettempdir(),
                                             "gpu-monitor-test.log")
m = types.ModuleType("gputray")
m.__file__ = APP
sys.modules["gputray"] = m
exec(compile(_src, APP, "exec"), m.__dict__)
sys.excepthook = sys.__excepthook__


class FakeIcon:
    def __init__(self):
        self.visible = False
        self.icon = None


def fake_count(n):
    """Pretend NVML sees n GPUs, and suppress the real 5s count re-check."""
    m.NVML_STATE["ngpu"] = n
    m.NVML_STATE["last_cnt"] = time.time()


def cancel_after(root):
    # MonitorApp.__init__ scheduled a tick that would overwrite fake counts.
    for aid in root.tk.call("after", "info"):
        root.after_cancel(aid)


def state(app, root):
    root.update()
    h = root.winfo_height()
    body = app.body.winfo_reqheight()
    mapped = sum(1 for c in app.cards if c["name"].master.winfo_ismapped())
    fits = 32 + body <= h
    print("  ui_ngpu=%d cards=%d mapped=%d win_h=%d body_req=%d %s" % (
        app.ui_ngpu, len(app.cards), mapped, h, body,
        "fits" if fits else "CLIPPED"))
    return fits, mapped, h


def width_state(app, root):
    root.update()
    req = app.body.winfo_reqwidth()
    win = root.winfo_width()
    edge = root.winfo_rootx() + win
    right = max(c["name"].master.winfo_rootx()
                + c["name"].master.winfo_width() for c in app.cards)
    ok = req <= win and right <= edge
    print("  win_w=%d body_req=%d rightmost=%d edge=%d %s" % (
        win, req, right, edge, "fits" if ok else "CLIPPED"))
    return ok


def main():
    ok = True
    fake_count(1)                        # pretend: driver still half-up
    root = tk.Tk()
    app = m.MonitorApp(root, FakeIcon())
    cancel_after(root)
    app.compact = True
    app.build_cards(1)
    app.apply_geometry()
    print("[1] start with 1 GPU")
    fits, mapped, h1 = state(app, root)
    ok &= fits and mapped == 1
    ax, ay = root.winfo_x(), root.winfo_y()
    wl, wt, wr, wb = m.work_area()
    print("  default anchor (%d,%d) size %dx%d -> edges must be (%d,%d), got (%d,%d)" % (
        ax, ay, root.winfo_width(), h1, wr, wb,
        ax + root.winfo_width(), ay + h1))
    ok &= ax + root.winfo_width() == wr and ay + h1 == wb

    print("[2] all three cards come back (1 -> 3)")
    fake_count(3)
    app._tick()
    fits, mapped, h2 = state(app, root)
    ok &= fits and len(app.cards) == 3 and mapped == 3 and h2 > h1

    print("[3] stable count: no rebuild")
    before = [id(c) for c in app.cards]
    app._tick()
    fits, mapped, h3 = state(app, root)
    ok &= [id(c) for c in app.cards] == before and h3 == h2

    print("[4] user-moved window keeps its bottom-right corner on resize")
    # Place it on-screen and leave room to grow to the 3-card full width.
    # (Hardcoding +3000+1500 broke on a 2560x1600 display.)
    _, _, vx, vy, vw, vh = m.desktop_metrics()
    mx = vx + max(0, min(3000, vw - 900))
    my = vy + max(0, min(1500, vh - 400))
    root.geometry("+%d+%d" % (mx, my))
    app.moved = True
    root.update()
    x0, y0 = root.winfo_x(), root.winfo_y()
    corner = (x0 + root.winfo_width(), y0 + root.winfo_height())
    w0, h0 = root.winfo_width(), root.winfo_height()
    fake_count(1)
    app._tick()
    root.update()
    x1, y1 = root.winfo_x(), root.winfo_y()
    new_corner = (x1 + root.winfo_width(), y1 + root.winfo_height())
    print("  top-left (%d,%d) -> (%d,%d), size %dx%d -> %dx%d, corner %s -> %s" % (
        x0, y0, x1, y1, w0, h0, root.winfo_width(), root.winfo_height(),
        corner, new_corner))
    ok &= new_corner == corner and x1 == x0 and y1 == y0 + h0 - 71

    print("[5] compact -> full also resizes about the bottom-right corner")
    # set .compact by hand: toggle_compact() would rewrite compact.flag
    app.compact = False
    app.build_cards(app.ui_ngpu)
    app.apply_geometry()
    root.update()
    x2, y2 = root.winfo_x(), root.winfo_y()
    w2, h2 = root.winfo_width(), root.winfo_height()
    print("  full mode 1 GPU %dx%d at (%d,%d), corner %s" % (
        w2, h2, x2, y2, (x2 + w2, y2 + h2)))
    ok &= (w2, h2) == (262, 492) and (x2 + w2, y2 + h2) == corner

    print("[6] full mode with 3 GPUs shows all three columns")
    fake_count(3)
    app._tick()
    root.update()
    x3, y3 = root.winfo_x(), root.winfo_y()
    w3, h3 = root.winfo_width(), root.winfo_height()
    print("  full mode 3 GPUs %dx%d at (%d,%d), corner %s" % (
        w3, h3, x3, y3, (x3 + w3, y3 + h3)))
    ok &= (w3, h3) == (786, 492) and (x3 + w3, y3 + h3) == corner
    ok &= width_state(app, root)
    ok &= state(app, root)[:2] == (True, 3)

    print("[7] back to compact with 3 GPUs")
    app.compact = True
    app.build_cards(3)
    app.apply_geometry()
    root.update()
    x4, y4 = root.winfo_x(), root.winfo_y()
    corner4 = (x4 + root.winfo_width(), y4 + root.winfo_height())
    print("  compact 3 GPUs %dx%d at (%d,%d), corner %s" % (
        root.winfo_width(), root.winfo_height(), x4, y4, corner4))
    ok &= (root.winfo_width(), root.winfo_height()) == (440, 137)
    ok &= corner4 == corner

    root.destroy()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
