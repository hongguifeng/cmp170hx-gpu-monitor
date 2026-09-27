# -*- coding: utf-8 -*-
"""Regression test: compact window height must track the GPU count.

The unlock flow after a reboot tears the NVIDIA driver down and reinstalls it,
so the app can come up with NVML reporting only 1 GPU; both cards return a bit
later. The card rows are rebuilt on that count change, but the compact window
height (it is derived from the count) used to be applied only at startup --
the second row ended up outside the window, so the tool "showed one GPU"
while its status line already said "NVML . 2 cards".

Run:  python test-geometry.py        (real NVML / real Tk, ASCII output only)
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

    print("[4] user-moved window keeps its position on resize")
    root.geometry("+100+100")
    app.moved = True
    m.NVML_STATE["ngpu"] = 1
    app._tick()
    root.update()
    ok &= (root.winfo_x(), root.winfo_y()) == (100, 100)

    root.destroy()
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
