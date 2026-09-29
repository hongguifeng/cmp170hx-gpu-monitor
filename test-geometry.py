# -*- coding: utf-8 -*-
"""Regression test: compact window height must track the GPU count.

The unlock flow after a reboot tears the NVIDIA driver down and reinstalls it,
so the app can come up with NVML reporting only 1 GPU; both cards return a bit
later. The card rows are rebuilt on that count change, but the compact window
height (it is derived from the count) used to be applied only at startup --
the second row ended up outside the window, so the tool "showed one GPU"
while its status line already said "NVML . 2 cards".

Run:  python test-geometry.py        (real NVML / real Tk, ASCII output only)

Steps 4-5 also guard the newer rule: a window the user dragged must resize
about its bottom-right corner, never its top-left, otherwise switching
compact <-> full buries the window under the taskbar or leaves a hole.
"""

import os
import sys
import tempfile
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


class FakeIcon:
    def __init__(self):
        self.visible = False
        self.icon = None


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


def main():
    ok = True
    m.NVML_STATE["ngpu"] = 1              # pretend: driver still half-up
    root = tk.Tk()
    app = m.MonitorApp(root, FakeIcon())
    app.compact = True
    print("[1] start with 1 GPU")
    fits, mapped, h1 = state(app, root)
    ok &= fits and mapped == 1
    ax, ay = root.winfo_x(), root.winfo_y()
    wl, wt, wr, wb = m.work_area()
    print("  default anchor (%d,%d) size %dx%d -> edges must be (%d,%d), got (%d,%d)" % (
        ax, ay, root.winfo_width(), h1, wr, wb,
        ax + root.winfo_width(), ay + h1))
    ok &= ax + root.winfo_width() == wr and ay + h1 == wb

    print("[2] both cards come back (1 -> 2)")
    m.NVML_STATE["ngpu"] = 2
    app._tick()
    fits, mapped, h2 = state(app, root)
    ok &= fits and len(app.cards) == 2 and mapped == 2 and h2 > h1

    print("[3] stable count: no rebuild")
    before = [id(c) for c in app.cards]
    app._tick()
    fits, mapped, h3 = state(app, root)
    ok &= [id(c) for c in app.cards] == before and h3 == h2

    print("[4] user-moved window keeps its bottom-right corner on resize")
    root.geometry("+100+1500")
    app.moved = True
    root.update()
    x0, y0 = root.winfo_x(), root.winfo_y()
    corner = (x0 + root.winfo_width(), y0 + root.winfo_height())
    m.NVML_STATE["ngpu"] = 1
    app._tick()
    root.update()
    x1, y1 = root.winfo_x(), root.winfo_y()
    new_corner = (x1 + root.winfo_width(), y1 + root.winfo_height())
    print("  top-left (%d,%d) -> (%d,%d), corner %s -> %s" % (
        x0, y0, x1, y1, corner, new_corner))
    # the width did not change, so only y moves; the corner must not
    ok &= new_corner == corner and x1 == x0 and y1 > y0

    print("[5] compact -> full also resizes about the bottom-right corner")
    # set .compact by hand: toggle_compact() would rewrite compact.flag
    app.compact = False
    app.build_cards(app.ui_ngpu)
    app.apply_geometry()
    root.update()
    x2, y2 = root.winfo_x(), root.winfo_y()
    w2, h2 = root.winfo_width(), root.winfo_height()
    print("  full mode %dx%d at (%d,%d), corner %s" % (w2, h2, x2, y2,
                                                       (x2 + w2, y2 + h2)))
    ok &= (w2, h2) == (524, 492) and (x2 + w2, y2 + h2) == corner

    app.compact = True
    app.build_cards(app.ui_ngpu)
    app.apply_geometry()
    root.update()
    x3, y3 = root.winfo_x(), root.winfo_y()
    corner3 = (x3 + root.winfo_width(), y3 + root.winfo_height())
    print("  back to compact, corner %s" % (corner3,))
    ok &= corner3 == corner

    root.destroy()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
