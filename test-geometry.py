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
bottom-right corner, never its top-left, otherwise switching between the three
layouts (full / compact / ultra) buries the window under the taskbar or leaves
a hole. Steps 8-9 cover the ultra layout: a single line with no title bar at
all, whose width follows the count while its height stays fixed. Step 10 checks
the rule that keeps that line reachable: it may hide under the taskbar, but
never so deep that there is nothing left to click.
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


def card_box(c):
    """The widget that is one card, however the current layout builds it.

    Full and compact print a GPU name, so the name label's master is the card
    frame. An ultra cell has no name at all -- the cell itself is the card.
    """
    return c["name"].master if "name" in c else c["cell"]


def state(app, root):
    root.update()
    h = root.winfo_height()
    body = app.body.winfo_reqheight()
    # ultra has no title bar at all, so the body starts at the very top
    base = 32 if app.header.winfo_manager() == "pack" else 0
    mapped = sum(1 for c in app.cards if card_box(c).winfo_ismapped())
    fits = base + body <= h
    print("  ui_ngpu=%d cards=%d mapped=%d win_h=%d body_req=%d %s" % (
        app.ui_ngpu, len(app.cards), mapped, h, body,
        "fits" if fits else "CLIPPED"))
    return fits, mapped, h


def width_state(app, root):
    root.update()
    req = app.body.winfo_reqwidth()
    win = root.winfo_width()
    edge = root.winfo_rootx() + win
    right = max(card_box(c).winfo_rootx()
                + card_box(c).winfo_width() for c in app.cards)
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
    app.mode = m.MODE_COMPACT
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
    # set .mode by hand: cycle_mode() would rewrite compact.flag
    app.mode = m.MODE_FULL
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
    app.mode = m.MODE_COMPACT
    app.build_cards(3)
    app.apply_geometry()
    root.update()
    x4, y4 = root.winfo_x(), root.winfo_y()
    corner4 = (x4 + root.winfo_width(), y4 + root.winfo_height())
    print("  compact 3 GPUs %dx%d at (%d,%d), corner %s" % (
        root.winfo_width(), root.winfo_height(), x4, y4, corner4))
    ok &= (root.winfo_width(), root.winfo_height()) == (440, 137)
    ok &= corner4 == corner

    print("[8] ultra mode puts all three GPUs on one line")
    app.mode = m.MODE_ULTRA
    app.build_cards(3)
    app.apply_geometry()
    root.update()
    x5, y5 = root.winfo_x(), root.winfo_y()
    w5, h5 = root.winfo_width(), root.winfo_height()
    corner5 = (x5 + w5, y5 + h5)
    req = app._ultra_need()
    mapped = sum(1 for c in app.cards
                 if c["cell"].winfo_ismapped() and c["used"].winfo_ismapped()
                 and c["pw"].winfo_ismapped())
    print("  ultra 3 GPUs %dx%d at (%d,%d), corner %s, line_needs=%d mapped=%d" % (
        w5, h5, x5, y5, corner5, req, mapped))
    # one line, no title bar: width follows the count, height is fixed. The
    # width has to hold every segment plus both buttons at the right end.
    ok &= (w5, h5) == (m.ULTRA_PAD + m.ULTRA_SEG_W * 3
                       + m.ULTRA_SEP_W * 2 + m.ULTRA_BTN_W, m.ULTRA_H)
    ok &= corner5 == corner
    ok &= req <= w5 and mapped == 3
    ok &= app.header.winfo_manager() == ""
    # Nothing on this line says which GPU a cell is: no index label survived,
    # a cell holds its two figures and nothing else.
    ok &= all(sorted(c) == ["cell", "pw", "used"] for c in app.cards)

    # Every card owns a cell of exactly ULTRA_SEG_W, evenly spaced. That is the
    # whole point: a number changing on GPU 0 must not push GPU 1's figures.
    cells = [(c["cell"].winfo_width(), c["cell"].winfo_rootx(),
              c["used"].winfo_rootx()) for c in app.cards]
    print("  cells width/x/figure %s" % (cells,))
    ok &= all(w == m.ULTRA_SEG_W for w, _, _ in cells)
    ok &= all(cells[i][1] - cells[i - 1][1] == m.ULTRA_SEG_W + m.ULTRA_SEP_W
              for i in range(1, len(cells)))
    app._tick()
    root.update()
    after = [(c["cell"].winfo_width(), c["cell"].winfo_rootx(),
              c["used"].winfo_rootx()) for c in app.cards]
    print("  after fresh readings %s" % (after,))
    ok &= after == cells

    print("[9] ultra with a single GPU: one segment, plus the two buttons")
    fake_count(1)
    app._tick()
    root.update()
    x6, y6 = root.winfo_x(), root.winfo_y()
    w6, h6 = root.winfo_width(), root.winfo_height()
    print("  ultra 1 GPU %dx%d at (%d,%d), corner %s" % (
        w6, h6, x6, y6, (x6 + w6, y6 + h6)))
    ok &= (w6, h6) == (max(m.ULTRA_MIN_W,
                           m.ULTRA_PAD + m.ULTRA_SEG_W + m.ULTRA_BTN_W),
                       m.ULTRA_H)
    ok &= (x6 + w6, y6 + h6) == corner
    ok &= state(app, root)[:2] == (True, 1)

    print("[10] the taskbar must never swallow the widget")
    tb = (0, 2112, 3840, 2160)          # taskbar docked at the bottom, 48px tall
    cases = [
        ((2000, 2084), 28, False),      # clear above it: nothing to worry about
        ((2000, 2096), 16, False),      # a third covered: still grabbable
        ((2000, 2104), 8, True),        # only 8px left: hopeless to aim at
        ((2000, 2112), 0, True),        # parked inside: completely unreachable
        ((2000, 2132), 0, True),        # below its top edge: same story
    ]
    ultra_w = m.ULTRA_PAD + m.ULTRA_SEG_W * 3 + m.ULTRA_SEP_W * 2 \
        + m.ULTRA_BTN_W
    for (x, y), want_strip, want_flip in cases:
        wr = (x, y, x + ultra_w, y + 28)  # the ultra line, as it is now
        share = m.covered_share(wr, tb)
        strip = m.visible_strip(wr, tb)
        flip = (share >= m.TASKBAR_COVER_LIMIT
                or (share > 0.0 and strip < m.TASKBAR_KEEP_VISIBLE_PX))
        print("  line at (%d,%d): covered %.0f%%, %dpx sticking out, "
              "goes above taskbar=%r" % (x, y, share * 100, strip, flip))
        ok &= strip == want_strip and flip == want_flip
    ok += 0
    ok &= m.covered_share((2000, 2084, 2356, 2112), None) == 0.0
    ok &= m.visible_strip(None, tb) == 0

    root.destroy()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
