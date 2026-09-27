# -*- coding: utf-8 -*-
"""GPU Monitor tray app for CMP 170HX x2 (MCDM mode).

- NVML channel only (works where NVAPI-based tools fail)
- Tray icon (shows hottest temp), click = show/hide floating window
- Floating always-on-top window: per-GPU temp/power graph + info
- Optional autostart (HKCU Run key, toggle in tray menu)
"""
import ctypes
import faulthandler
import os
import subprocess
import sys
import threading
import time
import traceback
import winreg
from collections import deque
from statistics import mean

import tkinter as tk

import pystray
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- NVML layer
NVML_TEMPERATURE_GPU = 0
NVML_CLOCK_GRAPHICS = 0
NVML_CLOCK_MEM = 2


class NvmlMemory(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong),
                ("free", ctypes.c_ulonglong),
                ("used", ctypes.c_ulonglong)]


class NvmlUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


nv = ctypes.WinDLL("nvml.dll")
NVML_STATE = {"ok": False, "ngpu": 0, "last_try": 0.0, "last_cnt": 0.0}


def acquire_single_instance():
    """Named mutex; returns False if another instance already runs."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    h = k32.CreateMutexW(None, 0, "GPUMonitor170_SingleInstance")
    # ERROR_ALREADY_EXISTS = 183
    if h is None or ctypes.get_last_error() == 183:
        return False
    return True

# ------------------------------------------------- diagnostic log (transitions only)
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
    LOG_PATH = os.path.join(APP_DIR, "monitor.log")
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    LOG_PATH = os.path.join(APP_DIR, "monitor.log")
QUIT_FLAG = os.path.join(APP_DIR, "no-restart.flag")
WATCHDOG_PS1 = os.path.join(APP_DIR, "watchdog.ps1")
MODE_FLAG = os.path.join(APP_DIR, "compact.flag")


def load_compact():
    """Compact-mode preference persists across restarts (file presence)."""
    return os.path.exists(MODE_FLAG)


def save_compact(on):
    try:
        if on:
            open(MODE_FLAG, "w").close()
        elif os.path.exists(MODE_FLAG):
            os.remove(MODE_FLAG)
    except OSError:
        pass
_LOG_LOCK = threading.Lock()


def log(msg):
    line = "%s %s" % (time.strftime("%m-%d %H:%M:%S"), msg)
    try:
        with _LOG_LOCK:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


# black box: capture hard crashes (access violations) and uncaught exceptions
_crash_file = open(LOG_PATH, "a", encoding="utf-8")
faulthandler.enable(_crash_file)


def _excepthook(t, v, tb):
    log("uncaught %s: %s" % (t.__name__, v))
    try:
        traceback.print_exception(t, v, tb, file=_crash_file)
        _crash_file.flush()
    except Exception:
        pass


sys.excepthook = _excepthook


def nvml_reinit(force=False):
    """(Re)initialize NVML. Rate-limited to once per 5s unless force.

    The unlock flow (bind 170_boot -> IOCTL -> restore NVIDIA) tears the
    NVIDIA driver down and reinstalls it, which invalidates every NVML
    handle. We detect dead reads and keep re-initializing until the GPUs
    come back, so the tool survives driver reinstalls.
    """
    if not force and time.time() - NVML_STATE["last_try"] < 5:
        return
    NVML_STATE["last_try"] = time.time()
    try:
        nv.nvmlShutdown()
    except Exception:
        pass
    rc = nv.nvmlInit_v2()
    if rc != 0:
        NVML_STATE["ok"] = False
        NVML_STATE["ngpu"] = 0
        log("reinit: init rc=%d" % rc)
        return
    n = ctypes.c_uint()
    rc = nv.nvmlDeviceGetCount_v2(ctypes.byref(n))
    if rc == 0:
        NVML_STATE["ok"] = True
        NVML_STATE["ngpu"] = n.value
        log("reinit: ok ngpu=%d" % n.value)
    else:
        NVML_STATE["ok"] = False
        NVML_STATE["ngpu"] = 0
        log("reinit: init ok but count rc=%d" % rc)


nvml_reinit(force=True)
log("start ok=%s ngpu=%d" % (NVML_STATE["ok"], NVML_STATE["ngpu"]))


def read_gpu(idx):
    """Read one GPU via NVML; missing fields stay None."""
    d = {"index": idx}
    if not NVML_STATE["ok"] or idx >= NVML_STATE["ngpu"]:
        return d
    h = ctypes.c_void_p()
    if nv.nvmlDeviceGetHandleByIndex_v2(ctypes.c_uint(idx), ctypes.byref(h)) != 0:
        return d
    d["_alive"] = True

    name = ctypes.create_string_buffer(96)
    if nv.nvmlDeviceGetName(h, name, 96) == 0:
        d["name"] = name.value.decode("utf-8", "replace")

    t = ctypes.c_uint()
    if nv.nvmlDeviceGetTemperature(h, NVML_TEMPERATURE_GPU, ctypes.byref(t)) == 0:
        d["temp"] = t.value

    clocks = {}
    for label, ctype in (("gpu", NVML_CLOCK_GRAPHICS), ("mem", NVML_CLOCK_MEM)):
        c = ctypes.c_uint()
        if nv.nvmlDeviceGetClockInfo(h, ctypes.c_uint(ctype), ctypes.byref(c)) == 0:
            clocks[label] = c.value
    d["clocks"] = clocks

    mem = NvmlMemory()
    if nv.nvmlDeviceGetMemoryInfo(h, ctypes.byref(mem)) == 0 and mem.total:
        d["mem_used"] = round(mem.used / (1 << 30), 1)
        d["mem_total"] = round(mem.total / (1 << 30), 1)
        d["mem_pct"] = round(100.0 * mem.used / mem.total, 1)

    util = NvmlUtil()
    if nv.nvmlDeviceGetUtilizationRates(h, ctypes.byref(util)) == 0:
        d["util"] = util.gpu

    pw = ctypes.c_uint()
    if nv.nvmlDeviceGetPowerUsage(h, ctypes.byref(pw)) == 0:
        d["power"] = round(pw.value / 1000.0, 1)
        lim = ctypes.c_uint()
        if nv.nvmlDeviceGetPowerManagementLimit(h, ctypes.byref(lim)) == 0:
            d["power_limit"] = round(lim.value / 1000.0, 1)

    fan = ctypes.c_uint()
    if nv.nvmlDeviceGetFanSpeed(h, ctypes.byref(fan)) == 0:
        d["fan"] = fan.value

    with NSMI_LOCK:
        v = NSMI.get(str(idx))
    if v and time.time() - v[2] < 5:  # entries expire: no frozen temps
        d["temp"] = v[0]   # same value as NVML die temp, from stream
        d["mtemp"] = v[1]  # HBM memory temp
    return d


def pretty_name(raw):
    return "CMP 170HX" if "Graphics Device" in (raw or "") else (raw or "GPU")

# ------------------------------------------------- memory temp via nvidia-smi
# NVML_TEMPERATURE_MEMORY is NOT_SUPPORTED on this stack (probe rc=2), but
# `nvidia-smi --query-gpu=temperature.memory` reports HBM temp (dmon mtemp).
# We keep one streaming child process and parse its 1 Hz lines.
NSMI = {}
NSMI_LOCK = threading.Lock()


def nsmi_reader():
    while True:
        try:
            p = subprocess.Popen(
                ["nvidia-smi", "--query-gpu=index,temperature.gpu,"
                                "temperature.memory",
                 "--format=csv,noheader,nounits", "-l", "1"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True,
                creationflags=0x08000000)  # CREATE_NO_WINDOW: no console popup
            t0 = time.time()
            while True:
                line = p.stdout.readline()
                if line == "":
                    break  # process died
                parts = [x.strip() for x in line.strip().split(",")]
                if len(parts) == 3 and parts[0].isdigit():
                    try:
                        pair = (int(parts[1]), int(parts[2]))
                    except ValueError:
                        continue
                    with NSMI_LOCK:
                        NSMI[parts[0]] = (pair[0], pair[1], time.time())
                if time.time() - t0 > 60:
                    # periodic refresh: the -l loop fixes its device list at
                    # spawn. A stream spawned mid-boot (before both GPUs are
                    # up) keeps printing one GPU forever, which defeats the
                    # stale-NVML detector. Restart to re-enumerate.
                    p.kill()
                    break
        except Exception:
            pass
        time.sleep(2)  # process died -> restart

# ---------------------------------------------------------------- autostart
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "GPUMonitor"


def self_path():
    if getattr(sys, "frozen", False):
        return sys.executable
    return "%s %s" % (sys.executable, os.path.abspath(__file__))


def is_autostart():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, VALUE_NAME)
        return True
    except OSError:
        return False


def set_autostart(enable):
    try:
        if enable:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ,
                                  '"%s"' % self_path())
        else:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                try:
                    winreg.DeleteValue(k, VALUE_NAME)
                except FileNotFoundError:
                    pass
    except OSError:
        pass
    return is_autostart()

# ---------------------------------------------------------------- tray icon
def make_tray_image(text="G", color="#4f8cff"):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 62, 62], radius=14, fill=(28, 31, 38, 255),
                        outline=color, width=3)
    try:
        f = ImageFont.truetype("segoeuib.ttf", 30)
    except OSError:
        f = ImageFont.load_default()
    bbox = d.textbbox((0, 0), text, font=f)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((64 - w) / 2 - bbox[0], (64 - h) / 2 - bbox[1]), text,
           font=f, fill=color)
    return img


def tray_image_for(temps):
    ts = [t for t in temps if t is not None]
    if not ts:
        return make_tray_image()
    t = max(ts)
    color = "#ff5555" if t >= 80 else "#ffb020" if t >= 68 else "#4f8cff"
    return make_tray_image(str(int(round(t))), color)

# ---------------------------------------------------------------- UI
HIST = 180  # seconds of history
TEMP_COLORS = ["#4f8cff", "#3ddc84", "#ffb020", "#ff5555"]
MEM_COLORS = ["#9cc8ff", "#a8f0c0", "#ffd28a", "#ff9c9c"]
PW_COLOR = "#e8a23c"
BG = "#14161a"
CARD = "#1c1f26"
FG = "#e8eaf0"
TXT = "#c8cdd8"
DIM = "#9aa3b2"
LINE = "#2a2f3a"


class MonitorApp:
    def __init__(self, root, tray_icon):
        self.root = root
        self.tray_icon = tray_icon
        self.visible = True
        self.fail_streak = 0
        self.degraded_streak = 0
        self.stale_streak = 0
        self.ui_ngpu = NVML_STATE["ngpu"]
        self.compact = load_compact()

        root.title("GPU Monitor")
        root.configure(bg=BG)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.97)
        root.overrideredirect(True)
        self.apply_geometry()

        self._drag = None
        header = tk.Frame(root, bg=CARD, height=32)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)
        tk.Label(header, text=" GPU Monitor", bg=CARD, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        self.status = tk.Label(header, text="", bg=CARD, fg=DIM,
                               font=("Segoe UI", 9))
        self.status.pack(side="left")
        tk.Button(header, text="—", command=self.hide, bg=CARD, fg=DIM,
                  relief="flat", bd=0, font=("Segoe UI", 11),
                  activebackground=LINE, activeforeground=FG).pack(
            side="right", padx=(0, 8), pady=2)
        self.btn_mode = tk.Button(header, text="完全" if self.compact else "精简",
                                  command=self.toggle_compact, bg=CARD, fg=DIM,
                                  relief="flat", bd=0, font=("Segoe UI", 9),
                                  activebackground=LINE, activeforeground=FG)
        self.btn_mode.pack(side="right", padx=(0, 2), pady=2)

        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.cards = []
        self.canvases = []
        self.hist = self.mhist = self.phist = []
        self.build_cards(max(self.ui_ngpu, 1))

        for w in (header,):
            w.bind("<ButtonPress-1>", self._press)
            w.bind("<B1-Motion>", self._move)

        root.after(300, self.tick)
        self.tray_icon.visible = True

    def apply_geometry(self):
        if self.compact:
            w = 440
            h = 32 + 30 * max(self.ui_ngpu, 1) + 6
        else:
            w, h = 524, 492
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry("%dx%d+%d+%d" % (w, h, sw - w - 16, sh - h - 86))

    def toggle_compact(self):
        self.compact = not self.compact
        save_compact(self.compact)
        self.btn_mode.config(text="完全" if self.compact else "精简")
        self.build_cards(max(self.ui_ngpu, 1))
        self.apply_geometry()

    def build_cards(self, n):
        for w in self.body.winfo_children():
            w.destroy()
        self.cards = []
        self.canvases = []
        self.hist = [deque(maxlen=HIST) for _ in range(n)]
        self.mhist = [deque(maxlen=HIST) for _ in range(n)]
        self.phist = [deque(maxlen=HIST) for _ in range(n)]
        for i in range(n):
            if self.compact:
                # one dense row per GPU: name | temp | mem temp | mem | power
                card = tk.Frame(self.body, bg=CARD, highlightbackground=LINE,
                                highlightthickness=1)
                card.pack(fill="x", padx=6, pady=2)
                name = tk.Label(card, text="GPU %d" % i, bg=CARD, fg=FG,
                                font=("Segoe UI", 9, "bold"))
                name.pack(side="left", padx=(6, 4))
                temp = tk.Label(card, text="--", bg=CARD, fg=DIM, width=6,
                                font=("Segoe UI", 12, "bold"), anchor="w")
                temp.pack(side="left", padx=(0, 6))
                mono = ("Consolas", 9)
                mt = tk.Label(card, text="", bg=CARD, fg=TXT, font=mono)
                mt.pack(side="left", padx=(0, 6))
                mm = tk.Label(card, text="", bg=CARD, fg=TXT, font=mono)
                mm.pack(side="left", padx=(0, 6))
                mp = tk.Label(card, text="", bg=CARD, fg=TXT, font=mono)
                mp.pack(side="left")
                self.cards.append({"name": name, "temp": temp,
                                   "mtemp": mt, "mem": mm, "pw": mp})
                self.canvases.append(None)
                continue
            col = tk.Frame(self.body, bg=BG, width=250)
            if not self.compact:
                col.configure(height=444)
                col.pack_propagate(False)
            col.pack(side="left", fill="both", expand=True,
                     padx=(8, 4 if i == 0 else 2) if i == 0 else (4, 8),
                     pady=8)
            card = tk.Frame(col, bg=CARD, highlightbackground=LINE,
                            highlightthickness=1)
            card.pack(fill="both", expand=True)
            name = tk.Label(card, text="GPU %d" % i, bg=CARD, fg=FG,
                            font=("Segoe UI", 11, "bold"))
            name.pack(anchor="w", padx=10, pady=(8, 0))
            temp = tk.Label(card, text="--", bg=CARD, fg=DIM,
                            font=("Segoe UI", 32, "bold"))
            temp.pack(anchor="w", padx=10)
            info = tk.Label(card, text="", bg=CARD, fg=TXT, justify="left",
                            font=("Consolas", 11), anchor="w")
            info.pack(anchor="w", padx=10, pady=(0, 8))
            self.cards.append({"name": name, "temp": temp, "info": info})

            cv = None
            if not self.compact:
                cv = tk.Canvas(col, bg=CARD, height=170, highlightthickness=0)
                cv.pack(fill="x", pady=(6, 0))
                leg = tk.Frame(col, bg=BG)
                leg.pack(anchor="w", padx=2, pady=(3, 0))
                tc = TEMP_COLORS[i % len(TEMP_COLORS)]
                mc = MEM_COLORS[i % len(MEM_COLORS)]
                for text, color in (("— 核温", tc), ("— 存温", mc),
                                    ("┄ 功耗", PW_COLOR)):
                    tk.Label(leg, text=text, bg=BG, fg=color,
                             font=("Segoe UI", 9)).pack(side="left",
                                                       padx=(0, 10))
            self.canvases.append(cv)

    # window drag / visibility
    def _press(self, e):
        self._drag = (e.x, e.y)

    def _move(self, e):
        if self._drag:
            self.root.geometry("+%d+%d" % (e.x_root - self._drag[0],
                                           e.y_root - self._drag[1]))

    def hide(self):
        self.visible = False
        self.root.withdraw()

    def show(self):
        self.visible = True
        self.root.deiconify()
        self.root.lift()

    def toggle(self):
        if self.visible:
            self.hide()
        else:
            self.show()

    # data + render
    def tick(self):
        try:
            self._tick()
        except Exception:
            log("tick error:\n" + traceback.format_exc())
        self.root.after(1000, self.tick)

    def _tick(self):
        if not NVML_STATE["ok"]:
            nvml_reinit()  # rate-limited retry while driver is away

        # count re-check: during driver reinstall the cards come back one
        # at a time (we once got stuck at ngpu=1 forever) -> keep polling
        if NVML_STATE["ok"] and time.time() - NVML_STATE["last_cnt"] > 5:
            NVML_STATE["last_cnt"] = time.time()
            n = ctypes.c_uint()
            if nv.nvmlDeviceGetCount_v2(ctypes.byref(n)) == 0 \
                    and n.value != NVML_STATE["ngpu"]:
                log("count check: %d -> %d" % (NVML_STATE["ngpu"], n.value))
                NVML_STATE["ngpu"] = n.value

        if NVML_STATE["ngpu"] != self.ui_ngpu:
            log("ngpu %d -> %d, rebuild UI" % (self.ui_ngpu,
                                               NVML_STATE["ngpu"]))
            self.ui_ngpu = NVML_STATE["ngpu"]
            self.build_cards(max(self.ui_ngpu, 1))

        with_data = 0
        healthy = 0
        for i in range(max(NVML_STATE["ngpu"], 1)):
            g = read_gpu(i)
            if g.get("temp") is not None or g.get("name"):
                with_data += 1
            if (g.get("temp") is not None
                    and g.get("clocks", {}).get("gpu") is not None
                    and g.get("mem_total") is not None):
                healthy += 1
            t = g.get("temp")
            m = g.get("mtemp")
            p = g.get("power")
            self.hist[i].append(t if t is not None else None)
            self.mhist[i].append(m if m is not None else None)
            self.phist[i].append(p if p is not None else None)
            card = self.cards[i]
            if t is not None:
                col = "#ff5555" if t >= 80 else "#ffb020" if t >= 68 else "#3ddc84"
                card["temp"].config(text="%d°C" % t, fg=col)
            else:
                card["temp"].config(text="--", fg=DIM)
            card["name"].config(
                text="GPU %d" % i if self.compact
                else "GPU %d · %s" % (i, pretty_name(g.get("name"))))
            mem = "%s / %s GB" % (g.get("mem_used", "?"), g.get("mem_total", "?"))
            pwtxt = ("%s / %s W" % (p, g.get("power_limit", "?"))
                     if p is not None else "--")
            mtxt = ("%d °C" % m) if m is not None else "--"
            if self.compact:
                card["mtemp"].config(text="存温 %s" % mtxt)
                card["mem"].config(text="显存 %s/%sG" % (
                    g.get("mem_used", "?"), g.get("mem_total", "?")))
                pwshort = ("%gW" % p) if p is not None else "--"
                card["pw"].config(text="功耗 " + pwshort)
            else:
                info = ("频率  %s / %s MHz\n"
                        "占用  %s %%      风扇  %s %%\n"
                        "显存  %s\n"
                        "存温  %s\n"
                        "功耗  %s" % (
                            g.get("clocks", {}).get("gpu", "?"),
                            g.get("clocks", {}).get("mem", "?"),
                            g.get("util", "?"), g.get("fan", "?"),
                            mem, mtxt, pwtxt))
                card["info"].config(text=info)

            self.draw_graph(i)

        # no usable data for 5 straight seconds -> driver torn down or
        # reinstalled (stale handles), force-reinit NVML to refresh
        if NVML_STATE["ok"] and with_data == 0:
            self.fail_streak += 1
            if self.fail_streak == 3:
                log("reads dead x3s ok=%s ngpu=%d" % (
                    NVML_STATE["ok"], NVML_STATE["ngpu"]))
            if self.fail_streak >= 5:
                self.fail_streak = 0
                log("force reinit after dead reads")
                nvml_reinit(force=True)
        else:
            self.fail_streak = 0

        # partial degradation: some GPUs read, others dead/stale. Seen when
        # NVML was initialized mid-driver-reinstall (watchdog respawn during
        # the unlock flow): its internal state stays poisoned, some calls
        # work, others fail forever. The all-dead detector never fires here.
        if NVML_STATE["ok"] and healthy < NVML_STATE["ngpu"]:
            self.degraded_streak += 1
            if self.degraded_streak == 10:
                log("degraded %d/%d healthy for 10s" % (
                    healthy, NVML_STATE["ngpu"]))
            if self.degraded_streak >= 15:
                self.degraded_streak = 0
                log("force reinit: degraded %d/%d" % (
                    healthy, NVML_STATE["ngpu"]))
                nvml_reinit(force=True)
        else:
            self.degraded_streak = 0

        # stale NVML device list: NVML initialized mid-reinstall caches the
        # device count at init time (e.g. 1 of 2 cards), and its own
        # GetCount keeps returning the stale value forever, so the count
        # re-check above can never notice. The nvidia-smi stream is a fresh
        # enumeration (it is restarted after each driver teardown) -> if it
        # sees more GPUs than NVML for 10s, NVML is stale, force reinit.
        with NSMI_LOCK:
            now = time.time()
            nseen = sum(1 for v in NSMI.values() if now - v[2] < 5)
        if NVML_STATE["ok"] and nseen > NVML_STATE["ngpu"]:
            self.stale_streak += 1
            if self.stale_streak == 5:
                log("stale nvml? nsmi=%d nvml=%d" % (
                    nseen, NVML_STATE["ngpu"]))
            if self.stale_streak >= 10:
                self.stale_streak = 0
                log("force reinit: nsmi sees %d gpus, nvml %d" % (
                    nseen, NVML_STATE["ngpu"]))
                nvml_reinit(force=True)
        else:
            self.stale_streak = 0

        if not NVML_STATE["ok"]:
            self.status.config(text="  等待显卡(驱动重连中)…", fg="#ffb020")
        else:
            self.status.config(
                text="  NVML · %d 卡" % NVML_STATE["ngpu"], fg=DIM)

        temps = [d[-1] for d in self.hist if d and d[-1] is not None]
        try:
            self.tray_icon.icon = tray_image_for(temps)
        except Exception:
            pass

    def draw_graph(self, i):
        cv = self.canvases[i]
        if cv is None:
            return  # compact mode: no graph
        cv.delete("all")
        w = int(cv.winfo_width() or 240)
        h = 170
        pad_l, pad_r, pad_t, pad_b = 30, 34, 10, 16

        tv = [v for v in self.hist[i] if v is not None]
        mv = [v for v in self.mhist[i] if v is not None]
        pv = [v for v in self.phist[i] if v is not None]
        temp_all = tv + mv
        if not temp_all and not pv:
            return

        tlo, thi = (min(temp_all) - 2, max(temp_all) + 2) if temp_all else (0, 10)
        if thi - tlo < 10:
            mid = (thi + tlo) / 2
            tlo, thi = mid - 5, mid + 5
        plo, phi = (min(pv) - 5, max(pv) + 5) if pv else (0, 100)
        if phi - plo < 20:
            mid = (phi + plo) / 2
            plo, phi = max(0, mid - 10), mid + 10

        def px(j):
            return pad_l + (w - pad_l - pad_r) * j / (HIST - 1)

        def ty(v):
            return pad_t + (h - pad_t - pad_b) * (1 - (v - tlo) / (thi - tlo))

        def py(v):
            return pad_t + (h - pad_t - pad_b) * (1 - (v - plo) / (phi - plo))

        for k in range(5):
            y = pad_t + (h - pad_t - pad_b) * k / 4
            cv.create_line(pad_l, y, w - pad_r, y, fill=LINE)
            cv.create_text(4, y, text="%d°" % round(thi - (thi - tlo) * k / 4),
                           fill=TEMP_COLORS[i % len(TEMP_COLORS)],
                           font=("Segoe UI", 8), anchor="w")
            cv.create_text(w - 4, y,
                           text="%dW" % round(phi - (phi - plo) * k / 4),
                           fill=PW_COLOR, font=("Segoe UI", 8), anchor="e")
        cv.create_text(w - pad_r, h - 5, text="-3 min", fill=DIM,
                       font=("Segoe UI", 8), anchor="e")
        cv.create_text(pad_l, h - 5, text="now", fill=DIM,
                       font=("Segoe UI", 8), anchor="w")

        pts_t = [(px(j), ty(v)) for j, v in enumerate(self.hist[i])
                 if v is not None]
        if len(pts_t) >= 2:
            cv.create_line(pts_t, fill=TEMP_COLORS[i % len(TEMP_COLORS)],
                           width=2, smooth=True)
        pts_m = [(px(j), ty(v)) for j, v in enumerate(self.mhist[i])
                 if v is not None]
        if len(pts_m) >= 2:
            cv.create_line(pts_m, fill=MEM_COLORS[i % len(MEM_COLORS)],
                           width=2, smooth=True)
        pts_p = [(px(j), py(v)) for j, v in enumerate(self.phist[i])
                 if v is not None]
        if len(pts_p) >= 2:
            cv.create_line(pts_p, fill=PW_COLOR, width=1, dash=(4, 3),
                           smooth=True)

# ---------------------------------------------------------------- main
def main():
    if not acquire_single_instance():
        log("second instance blocked, exiting")
        return

    icon = pystray.Icon("GPUMonitor", make_tray_image(), "GPU Monitor")
    show_req = []

    def on_toggle(i, item):
        show_req.append("toggle")

    def on_autostart(i, item):
        set_autostart(not is_autostart())

    def on_quit(i, item):
        log("quit from tray menu")
        try:
            open(QUIT_FLAG, "w").close()
        except Exception:
            pass
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("显示 / 隐藏", on_toggle, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("开机自启", on_autostart,
                         checked=lambda item: is_autostart()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", on_quit),
    )
    icon.menu = menu
    threading.Thread(target=icon.run_detached, daemon=True).start()
    threading.Thread(target=nsmi_reader, daemon=True).start()

    root = tk.Tk()
    root.report_callback_exception = _excepthook
    app = MonitorApp(root, icon)

    def on_close():
        log("root closed via WM_DELETE_WINDOW")
        try:
            open(QUIT_FLAG, "w").close()
        except Exception:
            pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.bind("<Destroy>", lambda e: log("root Destroy event")
              if e.widget is root else None)

    def poll():
        if show_req:
            show_req.pop()
            app.toggle()
        root.after(150, poll)

    poll()
    # watchdog: respawn the exe if something kills it (survives our death);
    # respects no-restart.flag so a deliberate quit stays quit
    try:
        if os.path.exists(QUIT_FLAG):
            os.remove(QUIT_FLAG)
    except Exception:
        pass
    if os.path.exists(WATCHDOG_PS1):
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-File", WATCHDOG_PS1],
            creationflags=0x08000000)  # CREATE_NO_WINDOW
        log("watchdog spawned")
    try:
        root.mainloop()
    finally:
        log("mainloop exited")
        try:
            _crash_file.flush()
        except Exception:
            pass


if __name__ == "__main__":
    main()
