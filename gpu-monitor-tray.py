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
import queue
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


class NvmlPciInfo(ctypes.Structure):
    # struct nvmlPciInfo_t as returned by nvmlDeviceGetPciInfo_v3
    _fields_ = [("busIdLegacy", ctypes.c_char * 16),
                ("domain", ctypes.c_uint),
                ("bus", ctypes.c_uint),
                ("device", ctypes.c_uint),
                ("pciDeviceId", ctypes.c_uint),
                ("pciSubSystemId", ctypes.c_uint),
                ("busId", ctypes.c_char * 32)]


nv = ctypes.WinDLL("nvml.dll")
NVML_STATE = {"ok": False, "ngpu": 0, "last_try": 0.0, "last_cnt": 0.0}
# Supplemental NVAPI state. nvidia-smi exposes no memory-junction temperature
# for GDDR6X consumer cards (the RTX 3090 reports N/A), but NVAPI's thermal
# sensor array does. NVAPI cannot enumerate the CMPs in MCDM mode, so this is
# only used to fill mtemp for cards the nvidia-smi stream cannot report.
NVAPI_STATE = {"ok": False, "inited": False, "last_refresh": 0.0,
               "last_try": 0.0, "gpus": {}, "signature": (),
               "load_error": "", "init_rc": None, "enum_rc": None}
nvml_pci_info = (getattr(nv, "nvmlDeviceGetPciInfo_v3", None)
                 or getattr(nv, "nvmlDeviceGetPciInfo", None))


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

# Three layouts, cycled by the single title-bar button: 完全 -> 精简 -> 极简.
# The flag file keeps the choice across restarts. Its *content* now matters:
# an empty (or unreadable) file means compact, which is exactly what older
# builds wrote, so an existing compact.flag keeps working unchanged.
MODE_FULL = 0
MODE_COMPACT = 1
MODE_ULTRA = 2
MODE_NAMES = {MODE_FULL: "完全", MODE_COMPACT: "精简", MODE_ULTRA: "极简"}
MODE_NEXT = {MODE_FULL: MODE_COMPACT, MODE_COMPACT: MODE_ULTRA,
             MODE_ULTRA: MODE_FULL}
ULTRA_FLAG_TEXT = b"ultra"


def load_mode():
    """Layout preference persists across restarts via compact.flag."""
    try:
        if os.path.exists(MODE_FLAG):
            txt = open(MODE_FLAG, "rb").read().strip().lower()
            return MODE_ULTRA if txt == ULTRA_FLAG_TEXT else MODE_COMPACT
    except OSError:
        return MODE_COMPACT   # file there but unreadable: it meant compact
    return MODE_FULL


def save_mode(mode):
    try:
        if mode == MODE_FULL:
            if os.path.exists(MODE_FLAG):
                os.remove(MODE_FLAG)
        elif mode == MODE_COMPACT:
            open(MODE_FLAG, "w").close()
        else:
            with open(MODE_FLAG, "wb") as f:
                f.write(ULTRA_FLAG_TEXT)
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
        # A driver reinstall invalidates NVAPI's device handles too. Drop the
        # cache and let the next read re-enumerate (NvAPI_Initialize itself is
        # kept; re-enumeration is what matters).
        NVAPI_STATE["gpus"] = {}
        NVAPI_STATE["signature"] = ()
        NVAPI_STATE["last_refresh"] = 0.0
        log("reinit: ok ngpu=%d" % n.value)
    else:
        NVML_STATE["ok"] = False
        NVML_STATE["ngpu"] = 0
        log("reinit: init ok but count rc=%d" % rc)


nvml_reinit(force=True)
log("start ok=%s ngpu=%d" % (NVML_STATE["ok"], NVML_STATE["ngpu"]))


# ------------------------------------------------- memory temp via NVAPI
# `nvidia-smi --query-gpu=temperature.memory` is HBM-only on this stack: the
# RTX 3090 reports N/A even though its GDDR6X memory-junction sensor exists.
# NVAPI's (undocumented) NvAPI_GPU_GetThermalSensors exposes it, so use that
# as a fallback for cards where the nvidia-smi stream has no memory reading.
# NVAPI does not enumerate the CMPs in MCDM mode, so HBM cards are unaffected.
NVAPI_TS_VERSION = 0x000200A8  # sizeof(NvThermalSensors) | (2 << 16)


class NvThermalSensors(ctypes.Structure):
    _pack_ = 8
    _fields_ = [("version", ctypes.c_uint),
                ("mask", ctypes.c_uint),
                ("reserved", ctypes.c_int * 8),
                ("temperatures", ctypes.c_int * 32)]


_NVAPI = {"dll": None, "init": None, "enum": None, "therm": None,
          "bus": None, "name": None}
_NVAPI_INIT_T = ctypes.WINFUNCTYPE(ctypes.c_int)
_NVAPI_ENUM_T = ctypes.WINFUNCTYPE(ctypes.c_int,
                                   ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.POINTER(ctypes.c_uint))
_NVAPI_THERM_T = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                    ctypes.POINTER(NvThermalSensors))
_NVAPI_BUS_T = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                  ctypes.POINTER(ctypes.c_uint))
_NVAPI_NAME_T = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                   ctypes.c_char_p)

NVAPI_FN_INIT = 0x0150E828
NVAPI_FN_ENUM = 0xE5AC921F
NVAPI_FN_THERMAL_SENSORS = 0x65FE3AAD
NVAPI_FN_BUS_ID = 0x1BE0B8E5
NVAPI_FN_FULL_NAME = 0xCEEE8E9F


def nvapi_load():
    """Resolve NVAPI's dynamic entry points once; False if unavailable."""
    if _NVAPI["therm"] is not None:
        return True
    try:
        dll = ctypes.WinDLL("nvapi64.dll")
        q = dll.nvapi_QueryInterface
        q.restype = ctypes.c_void_p
        q.argtypes = [ctypes.c_uint]
        init = _NVAPI_INIT_T(q(NVAPI_FN_INIT))
        enum = _NVAPI_ENUM_T(q(NVAPI_FN_ENUM))
        therm = _NVAPI_THERM_T(q(NVAPI_FN_THERMAL_SENSORS))
        bus = _NVAPI_BUS_T(q(NVAPI_FN_BUS_ID))
        name = _NVAPI_NAME_T(q(NVAPI_FN_FULL_NAME))
    except Exception as e:
        # Do not latch this: nvapi64.dll disappears briefly while the driver
        # is being reinstalled. Keep retrying (rate-limited by the caller) but
        # only log the first failure so a missing DLL cannot flood the log.
        if not NVAPI_STATE["load_error"]:
            NVAPI_STATE["load_error"] = repr(e)
            log("nvapi load failed: %r" % (e,))
        return False
    _NVAPI.update(dll=dll, init=init, enum=enum, therm=therm, bus=bus,
                  name=name)
    NVAPI_STATE["load_error"] = ""
    return True


def nvapi_mem_index(name):
    """Sensor-array slot of the memory-junction temperature (LHM's tables).

    The NVAPI thermal array is not documented; 40/50-series moved the memory
    sensor. Ampere (RTX 3090) uses slot 9, which is our case here.
    """
    if "RTX 50" in name:
        return 2
    if "RTX 40" in name:
        return 7
    return 9


def nvapi_refresh(force=False):
    """Re-enumerate NVAPI physical GPUs (cheap; cached for 30s)."""
    now = time.time()
    if not force:
        if now - NVAPI_STATE["last_refresh"] < 30:
            return
        if now - NVAPI_STATE["last_try"] < 5:
            return
    NVAPI_STATE["last_try"] = now
    if not nvapi_load():
        NVAPI_STATE["ok"] = False
        return
    if not NVAPI_STATE["inited"]:
        rc = _NVAPI["init"]()
        if rc != 0:
            NVAPI_STATE["ok"] = False
            if NVAPI_STATE["init_rc"] != rc:
                NVAPI_STATE["init_rc"] = rc
                log("nvapi init rc=%d" % rc)
            return
        NVAPI_STATE["inited"] = True
    handles = (ctypes.c_void_p * 64)()
    cnt = ctypes.c_uint()
    rc = _NVAPI["enum"](handles, ctypes.byref(cnt))
    if rc != 0:
        NVAPI_STATE["ok"] = False
        if NVAPI_STATE["enum_rc"] != rc:
            NVAPI_STATE["enum_rc"] = rc
            log("nvapi enum rc=%d" % rc)
        return
    gpus = {}
    sig = []
    for i in range(min(cnt.value, 64)):
        h = handles[i]
        b = ctypes.c_uint()
        if _NVAPI["bus"](h, ctypes.byref(b)) != 0:
            continue
        nm = ctypes.create_string_buffer(64)
        name = ""
        if _NVAPI["name"](h, nm) == 0:
            name = nm.value.decode("utf-8", "replace")
        idx = nvapi_mem_index(name)
        gpus[b.value] = {"handle": h, "name": name, "mem_idx": idx}
        sig.append((b.value, name, idx))
    NVAPI_STATE.update(ok=True, gpus=gpus, last_refresh=now,
                       init_rc=None, enum_rc=None)
    sig = tuple(sig)
    if sig != NVAPI_STATE["signature"]:
        NVAPI_STATE["signature"] = sig
        log("nvapi: %s" % (sig,))


def nvapi_memtemp(bus):
    """Memory-junction temp for a PCI bus, or None when unavailable."""
    nvapi_refresh()
    g = NVAPI_STATE["gpus"].get(bus)
    if not g:
        return None
    idx = g["mem_idx"]
    ts = NvThermalSensors()
    ts.version = NVAPI_TS_VERSION
    ts.mask = 1 << idx
    try:
        rc = _NVAPI["therm"](g["handle"], ctypes.byref(ts))
    except Exception as e:
        log("nvapi thermal failed: %r" % (e,))
        NVAPI_STATE["last_refresh"] = 0.0
        return None
    if rc != 0:
        # Stale handle after a driver reset, or a slot this GPU lacks.
        NVAPI_STATE["last_refresh"] = 0.0
        return None
    v = ts.temperatures[idx]
    return int(round(v / 256.0)) if v > 0 else None


def read_gpu(idx):
    """Read one GPU via NVML; missing fields stay None."""
    d = {"index": idx}
    if not NVML_STATE["ok"] or idx >= NVML_STATE["ngpu"]:
        return d
    h = ctypes.c_void_p()
    if nv.nvmlDeviceGetHandleByIndex_v2(ctypes.c_uint(idx), ctypes.byref(h)) != 0:
        return d
    d["_alive"] = True
    pci = NvmlPciInfo()
    if nvml_pci_info is not None and nvml_pci_info(h, ctypes.byref(pci)) == 0:
        d["bus"] = pci.bus

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
        if v[0] is not None:
            d["temp"] = v[0]      # same value as NVML die temp, from stream
        d["mtemp"] = v[1]         # HBM memory temp, None when sensor says N/A
    if d.get("mtemp") is None and d.get("bus") is not None:
        # nvidia-smi says N/A for GDDR6X cards on Windows (RTX 3090); NVAPI
        # still exposes the memory-junction sensor. CMPs are invisible to
        # NVAPI here, so they keep the HBM value from the stream above.
        m = nvapi_memtemp(d["bus"])
        if m is not None:
            d["mtemp"] = m
    return d


def pretty_name(raw):
    raw = raw or "GPU"
    if "CMP 170HX" in raw or "Graphics Device" in raw:
        return "CMP 170HX"
    return raw.replace("NVIDIA GeForce ", "").replace("NVIDIA ", "")

# ------------------------------------------------- memory temp via nvidia-smi
# NVML's temperature API has no memory sensor, so HBM temp is read from
# `nvidia-smi --query-gpu=temperature.memory` (dmon mtemp) via one streaming
# child process parsed at 1 Hz. GDDR6X cards report N/A there and are covered
# by the NVAPI fallback in read_gpu().
NSMI = {}
NSMI_LOCK = threading.Lock()
NSMI_CMD = ["nvidia-smi", "--query-gpu=index,temperature.gpu,"
                        "temperature.memory",
            "--format=csv,noheader,nounits", "-l", "1"]
NSMI_RESTART = 60   # -l fixes its device list at spawn: re-enumerate often
NSMI_SILENT = 20    # no line at all for this long -> child is wedged
NSMI_STALE = 15     # lines arrive but carry no usable data -> restart
NSMI_CHILD = None   # current child, killed on quit so no orphan survives


def smi_num(text):
    """Parse one nvidia-smi field; '[N/A]' and friends become None."""
    try:
        return int(text)
    except ValueError:
        return None


def nsmi_child_kill():
    """Kill the streaming child (app quit used to leave it running forever)."""
    global NSMI_CHILD
    with NSMI_LOCK:
        p, NSMI_CHILD = NSMI_CHILD, None
    if p is not None:
        try:
            p.kill()
        except Exception:
            pass


def nsmi_reader():
    """Keep exactly one feeding nvidia-smi stream.

    The old reader could wedge forever: a child that survives the unlock
    (device disabled + driver reinstalled underneath it) stops producing
    usable lines but never exits, so readline() blocked for good and the
    restart check -- which only ran after a line was read -- never fired.
    Symptom: NVML recovers, every field looks fine, 存温 stays "--" until
    the app itself is restarted. Every wait here now has a deadline, and
    every exit path kills its child.
    """
    last_death_log = 0.0
    while True:
        p = None
        try:
            p = subprocess.Popen(
                NSMI_CMD,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True,
                creationflags=0x08000000)  # CREATE_NO_WINDOW: no console popup
            with NSMI_LOCK:
                NSMI_CHILD = p
            t0 = time.time()
            last_data = t0
            lines = 0
            data = 0
            junk = 0
            q = queue.Queue()

            def pump(proc=p, out=q):  # reader thread: never blocks our loop
                try:
                    for ln in proc.stdout:
                        out.put(ln)
                except Exception:
                    pass
                out.put(None)  # EOF, or the pipe went away

            threading.Thread(target=pump, daemon=True).start()
            while True:
                try:
                    line = q.get(timeout=NSMI_SILENT)
                except queue.Empty:
                    log("nsmi stream silent %ds (lines=%d data=%d), restart"
                        % (int(time.time() - t0), lines, data))
                    break
                if line is None:
                    now = time.time()
                    if now - last_death_log > 10:
                        last_death_log = now
                        log("nsmi stream died rc=%s after %ds "
                            "(lines=%d data=%d)"
                            % (p.poll(), int(now - t0), lines, data))
                    break  # process died -> respawn below
                if not line.strip():
                    continue
                lines += 1
                parts = [x.strip() for x in line.strip().split(",")]
                if len(parts) == 3 and parts[0].isdigit():
                    g = smi_num(parts[1])
                    m = smi_num(parts[2])
                    if g is not None or m is not None:
                        with NSMI_LOCK:
                            NSMI[parts[0]] = (g, m, time.time())
                        data += 1
                        last_data = time.time()
                        junk = 0
                    else:
                        junk += 1
                else:
                    # "No devices were found" and friends: the device list
                    # this child enumerated is gone for good -- its own loop
                    # can never recover, so count it as no-data instead of
                    # skipping the restart check like the old `continue` did.
                    junk += 1
                    if junk == 1:
                        log("nsmi stream says: %r" % line.strip())
                if time.time() - t0 > NSMI_RESTART:
                    break  # periodic refresh: re-enumerate the device list
                if time.time() - last_data > NSMI_STALE:
                    log("nsmi stream fed no data for %ds, restart"
                        % int(time.time() - last_data))
                    break
        except Exception as e:
            log("nsmi spawn failed: %r" % (e,))
        finally:
            if p is not None:
                try:
                    p.kill()
                except Exception:
                    pass
                try:
                    p.stdout.close()   # one pipe per restart: free it promptly
                except Exception:
                    pass
                with NSMI_LOCK:
                    if NSMI_CHILD is p:
                        NSMI_CHILD = None
        time.sleep(2)  # dead or wedged -> give the driver a moment, retry

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
def make_tray_image(text="G", color="#3ddc84"):
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
    color = "#ff5555" if t >= 80 else "#ffb020" if t >= 68 else "#3ddc84"
    return make_tray_image(str(int(round(t))), color)

# ---------------------------------------------------------------- UI
HIST = 180  # seconds of history
# Ultra mode paints each GPU index in its own colour so one crowded line can
# still be read; TEMP_COLORS exists for that purpose alone. Every full-mode
# graph uses the same three inks whatever GPU it belongs to -- the line style
# separates core temp / mem temp / power, the card it sits in separates GPUs.
TEMP_COLORS = ["#4f8cff", "#3ddc84", "#ffb020", "#ff5555"]
GRAPH_TEMP = "#4f8cff"
GRAPH_MEM = "#9cc8ff"
PW_COLOR = "#e8a23c"
BG = "#14161a"
CARD = "#1c1f26"
FG = "#e8eaf0"
TXT = "#c8cdd8"
DIM = "#9aa3b2"
LINE = "#2a2f3a"

# window geometry: compact height tracks the GPU count, full width does too
COMPACT_W = 440
COMPACT_ROW_H = 33
FULL_CARD_W = 250
FULL_CARD_PAD = 12   # left + right padding of one full-mode column
FULL_COL_H = 444     # one full-mode column, header and padding excluded
FULL_H = 492

# Full mode splits its history band into two charts stacked one above the
# other: watts on top, degrees underneath. Core temp and mem temp are both
# degrees and share one scale, so they stay together in one chart; watts have
# nothing in common with degrees, so power gets its own chart and its own
# grid. The band used to be one 170px canvas plus a legend frame outside it;
# the key now lives inside each chart's own top strip, which is what pays for
# the second chart without the window growing by a single pixel: 444 - 124 -
# 90 - (6 + 6) padding = 218 left for the text card, which asks for 205.
# Each chart keeps the height its own content needs, so the taller one (two
# series, four grid lines) now sits at the bottom.
GRAPH_H_TEMP = 124
GRAPH_H_PW = 90
GRAPH_GAP = 6        # dark strip between the two charts: what tells them apart
GRAPH_PAD_L = 38     # room for the axis numbers: "255W" is 29px wide at 8pt
GRAPH_PAD_R = 14     # used to be 34: the watt numbers moved to the left edge
GRAPH_PAD_T = 16     # strip above the top grid line, where the key sits
GRAPH_PAD_B = 16     # room for "now" / "-3 min" on the bottom chart
GRAPH_PAD_B_TOP = 10 # the upper chart shares the time axis, so it needs less
GRAPH_KEY_Y = 7      # vertical centre of the key line
GRAPH_KEY_GAP = 56   # second key sits this far right of the first (key is 39px)
# Ultra mode packs every GPU into one line: "0 12G 250W | 1 11G 220W". Only
# the memory in use and the power draw survive, both rounded to whole numbers
# and with no tag in front of them, and there is no title bar above them --
# just this one line.
ULTRA_SEG_W = 90       # every GPU gets a cell of exactly this width, so a
                       # figure changing on one card cannot shove the others
                       # sideways. Sized for the widest reading we expect and
                       # nothing more: index 13+10, "99G" 25, "999W" 34+8
ULTRA_SEP_W = 6        # divider between two cells: 2px bar plus its 2+2 padding
ULTRA_SEP_COLOR = "#4a5262"   # brighter than the box border, so the cells are
                              # clearly split apart without shouting at the data
ULTRA_PAD = 12         # left + right padding around the single row
ULTRA_BTN_W = 62       # — plus the layout button, with their padding
ULTRA_MIN_W = 176      # floor: one cell and the two buttons still fit
ULTRA_ROW_H = 26       # the line itself, tall enough for the dash button
ULTRA_H = ULTRA_ROW_H + 2   # plus the 1px padding above and below it


def fmt_power_w(p):
    """Whole watts; past 999 W fall back to whole kW (no decimals either)."""
    try:
        p = float(p)
    except (TypeError, ValueError):
        return "--"
    if p >= 1000.0:
        return "%dkW" % int(round(p / 1000.0))
    return "%dW" % int(round(p))


def fmt_mem_used(used):
    """Memory in use in whole gigabytes with its unit, e.g. 12G."""
    try:
        return "%dG" % int(round(float(used)))
    except (TypeError, ValueError):
        return "--"

user32 = ctypes.WinDLL("user32")
user32.GetSystemMetrics.restype = ctypes.c_int

# Tk's -topmost puts the widget in the topmost band, which also places it
# above the taskbar. We want the opposite z-order: above ordinary windows
# (topmost) but below the taskbar, so dragging the widget over the taskbar
# never hides the Start button / tray / clock.
GA_ROOT = 2
MONITOR_DEFAULTTONEAREST = 2
GW_HWNDPREV = 3
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200
HWND_TOPMOST = ctypes.c_void_p(-1)

user32.GetAncestor.restype = ctypes.c_void_p
user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
user32.FindWindowW.restype = ctypes.c_void_p
user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
user32.FindWindowExW.restype = ctypes.c_void_p
user32.FindWindowExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_wchar_p, ctypes.c_wchar_p]
user32.GetWindow.restype = ctypes.c_void_p
user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.SetWindowPos.restype = ctypes.c_int
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]


def taskbar_for(hwnd):
    """Taskbar on this window's monitor (primary or secondary)."""
    mon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    h = user32.FindWindowExW(None, None, "Shell_SecondaryTrayWnd", None)
    while h:
        if mon and user32.MonitorFromWindow(
                h, MONITOR_DEFAULTTONEAREST) == mon:
            return h
        h = user32.FindWindowExW(None, h, "Shell_SecondaryTrayWnd", None)
    return user32.FindWindowW("Shell_TrayWnd", None)


def desktop_metrics():
    """Live desktop sizes in pixels: (prim_w, prim_h, vx, vy, vw, vh).

    Tk caches winfo_screenwidth/height when the interpreter starts, so after a
    display mode switch (4K -> 2K) they keep reporting the old screen and the
    window stays placed for it (i.e. off-screen). The OS numbers are live, and
    for this DPI-unaware process they share Tk's coordinate space.
    """
    g = user32.GetSystemMetrics
    # SM_CXSCREEN/SM_CYSCREEN = primary monitor; SM_X/Y/CX/CYVIRTUALSCREEN =
    # the desktop that spans every connected monitor
    return (g(0), g(1), g(76), g(77), g(78), g(79))


SPI_GETWORKAREA = 0x0030


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


user32.SystemParametersInfoW.restype = ctypes.c_int
user32.SystemParametersInfoW.argtypes = [ctypes.c_uint, ctypes.c_uint,
                                         ctypes.c_void_p, ctypes.c_uint]


def work_area():
    """Desktop that is not covered by the taskbar/tray: (left, top, right, bottom).

    The widget's default spot is the bottom-right corner of this area, so its
    right edge lands on the screen edge and its bottom edge on the taskbar's
    top edge. The old placement hardcoded a 86px bottom gap, which only made
    sense for a 48px taskbar and left a 38px hole above it (plus 16px slack on
    the right); the work area is live, so a taskbar that grows, shrinks or
    docks to another side is followed on the next tick.
    """
    r = _RECT()
    if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
        sw, sh = desktop_metrics()[:2]
        return (0, 0, sw, sh)
    return (r.left, r.top, r.right, r.bottom)


user32.GetWindowRect.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]


def window_rect(hwnd):
    """A window's frame as (left, top, right, bottom), or None on failure."""
    r = _RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right, r.bottom)


# How much of the widget the taskbar is allowed to hide before the widget
# climbs back on top of it, and how wide a strip of it has to stay visible in
# order to be worth leaving under the taskbar in the first place.
TASKBAR_COVER_LIMIT = 0.6
TASKBAR_KEEP_VISIBLE_PX = 12


def covered_share(win, barrier):
    """Share (0..1) of the window that sits underneath the barrier rectangle.

    Living below the taskbar is the whole point of the z-order dance: the
    taskbar paints over the widget and the start menu, the tray and the clock
    stay usable. That only works while the widget still sticks out. Once the
    taskbar covers most of it there is nothing left to click, and the monitor
    is unreachable until it is dragged back out -- so this is what the z-order
    code measures.
    """
    if not win or not barrier:
        return 0.0
    area = (win[2] - win[0]) * (win[3] - win[1])
    if area <= 0:
        return 0.0
    w = min(win[2], barrier[2]) - max(win[0], barrier[0])
    h = min(win[3], barrier[3]) - max(win[1], barrier[1])
    return max(w, 0) * max(h, 0) / float(area)


def visible_strip(win, barrier):
    """Thickest strip of the window that still sticks out of the barrier.

    A 356x28 line with 8px left above the taskbar is technically clickable but
    hopeless to grab, so coverage alone is not enough to decide: the strip that
    remains outside the taskbar matters just as much.
    """
    if not win or not barrier:
        return 0
    return max(
        max(barrier[1], win[1]) - win[1],    # above the barrier
        win[3] - min(barrier[3], win[3]),    # below it
        max(barrier[0], win[0]) - win[0],    # left of it
        win[2] - min(barrier[2], win[2]))    # right of it


class MonitorApp:
    def __init__(self, root, tray_icon):
        self.root = root
        self.tray_icon = tray_icon
        self.visible = True
        self.fail_streak = 0
        self.degraded_streak = 0
        self.stale_streak = 0
        self.nsmi_streak = 0
        self.read_err_log = 0.0
        self._zorder_state = None
        self._zorder_err_log = 0.0
        self.ui_ngpu = NVML_STATE["ngpu"]
        self.mode = load_mode()
        self.ultra_row = None  # set by build_cards; apply_geometry reads it
        self.moved = False  # user dragged the window: keep their position
        self.last_desktop = (desktop_metrics(), work_area())

        root.title("GPU Monitor")
        root.configure(bg=BG)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.97)
        root.overrideredirect(True)
        self.apply_geometry()

        self._drag = None
        header = self.header = tk.Frame(root, bg=CARD, height=32)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)
        self.title_lbl = tk.Label(header, text=" GPU Monitor", bg=CARD, fg=FG,
                                  font=("Segoe UI", 10, "bold"))
        self.title_lbl.pack(side="left")
        self.status = tk.Label(header, text="", bg=CARD, fg=DIM,
                               font=("Segoe UI", 9))
        self.status.pack(side="left")
        self.btn_hide = tk.Button(header, text="—", command=self.hide, bg=CARD,
                                  fg=DIM, relief="flat", bd=0,
                                  font=("Segoe UI", 11),
                                  activebackground=LINE, activeforeground=FG)
        self.btn_hide.pack(side="right", padx=(0, 8), pady=2)
        # The one button walks the whole ring of layouts; its label names the
        # mode you get by clicking it, never the one you are already in.
        self.btn_mode = tk.Button(header, text=MODE_NAMES[MODE_NEXT[self.mode]],
                                  command=self.cycle_mode, bg=CARD, fg=DIM,
                                  relief="flat", bd=0, font=("Segoe UI", 9),
                                  activebackground=LINE, activeforeground=FG)
        self.btn_mode.pack(side="right", padx=(0, 2), pady=2)
        # Ultra mode has its own pair, built inside the line itself.
        self.ultra_btn_hide = None
        self.ultra_btn_mode = None

        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.cards = []
        self.canvases = []
        self.pw_canvases = []
        self.hist = self.mhist = self.phist = []
        self.build_cards(max(self.ui_ngpu, 1))

        for w in (header,):
            w.bind("<ButtonPress-1>", self._press)
            w.bind("<B1-Motion>", self._move)

        # Clicking or focusing the widget can raise it above the taskbar;
        # re-assert the "below taskbar" z-order after any such event.
        root.bind("<ButtonRelease-1>", self._zorder_soon, add="+")
        root.bind("<FocusIn>", self._zorder_soon, add="+")

        root.after(50, self.keep_below_taskbar)
        root.after(300, self.tick)
        self.tray_icon.visible = True

    def _window_hwnd(self):
        # Do NOT cache this: before the Tk window is mapped, winfo_id()
        # resolves to a different (temporary) HWND, and caching that one made
        # every later SetWindowPos a silent no-op on a dead handle.
        try:
            return user32.GetAncestor(self.root.winfo_id(), GA_ROOT)
        except Exception:
            return 0

    def _zorder_soon(self, _e=None):
        self.root.after_idle(self.keep_below_taskbar)

    def keep_below_taskbar(self):
        """Keep the widget above normal windows, below the taskbar if possible.

        Tk's -topmost puts the window in the topmost band. That band is usually
        ordered above Shell_TrayWnd, so inserting the widget right after the
        taskbar lets the taskbar cover it. But the shell's taskbar is not
        always WS_EX_TOPMOST (resolution switches, Explorer restarts and some
        fullscreen helpers drop it); inserting after a non-topmost window
        would demote the widget into the normal band, where every other
        window covers it. So re-assert topmost first, and only go below the
        taskbar when the taskbar itself is topmost.
        """
        if not self.visible:
            return
        hwnd = self._window_hwnd()
        if not hwnd:
            return
        try:
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if not style & WS_EX_TOPMOST:
                user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                    SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
                                    | SWP_NOOWNERZORDER)
                style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            tray = taskbar_for(hwnd)
            tray_topmost = bool(
                tray and (user32.GetWindowLongW(tray, GWL_EXSTYLE)
                          & WS_EX_TOPMOST))
            if not (style & WS_EX_TOPMOST):
                # Could not restore topmost: report (rate-limited) and retry
                # on the next tick.
                now = time.time()
                if now - self._zorder_err_log > 30:
                    self._zorder_err_log = now
                    log("z-order: widget lost topmost and cannot restore it")
                return
            if not tray or not tray_topmost:
                # A topmost window cannot be below a non-topmost taskbar
                # without leaving the topmost band. Topmost wins: never let
                # ordinary windows cover the widget.
                state = ("topmost", tray)
                if state != self._zorder_state:
                    self._zorder_state = state
                    log("z-order: taskbar 0x%x not topmost; keeping widget "
                        "topmost" % (tray or 0))
                return
            # Partial overlap is the intended look; being swallowed whole is
            # not. If the taskbar would hide most of the widget, or leave less
            # of it sticking out than one can aim at, climb back on top of the
            # taskbar and stay there until the widget is dragged clear again.
            wr = window_rect(hwnd)
            tr = window_rect(tray)
            share = covered_share(wr, tr)
            strip = visible_strip(wr, tr)
            if share >= TASKBAR_COVER_LIMIT or (share > 0.0
                                               and strip < TASKBAR_KEEP_VISIBLE_PX):
                state = ("above", tray)
                if state != self._zorder_state:
                    self._zorder_state = state
                    log("z-order: taskbar covers %.0f%% of the widget, "
                        "%dpx left of it visible; keeping it above so it "
                        "stays clickable" % (share * 100, strip))
                user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                    SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
                                    | SWP_NOOWNERZORDER)
                return
            # Already directly below the taskbar? Walk the z-order upwards.
            h = user32.GetWindow(hwnd, GW_HWNDPREV)
            while h:
                if h == tray:
                    state = ("below", tray)
                    if state != self._zorder_state:
                        self._zorder_state = state
                        log("z-order: hwnd 0x%x below taskbar 0x%x"
                            % (hwnd, tray))
                    return
                h = user32.GetWindow(h, GW_HWNDPREV)
            user32.SetWindowPos(hwnd, tray, 0, 0, 0, 0,
                                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
                                | SWP_NOOWNERZORDER)
            state = ("below", tray)
            if state != self._zorder_state:
                self._zorder_state = state
                log("z-order: hwnd 0x%x below taskbar 0x%x" % (hwnd, tray))
        except Exception as e:
            now = time.time()
            if now - self._zorder_err_log > 30:
                self._zorder_err_log = now
                log("z-order failed: %r" % (e,))

    def apply_geometry(self):
        n = max(self.ui_ngpu, 1)
        if self.mode == MODE_ULTRA:
            # Everything sits on one line, so the height is constant and only
            # the width has to follow the GPU count. Never go below the title
            # bar's own needs, or the status text starts to overlap the buttons.
            w = max(ULTRA_MIN_W,
                    ULTRA_PAD + ULTRA_SEG_W * n + ULTRA_SEP_W * (n - 1)
                    + ULTRA_BTN_W)
            if self.ultra_row is not None:
                self.root.update_idletasks()
                # Live widgets win over the estimate: a machine without
                # Consolas falls back to a wider font and the line would
                # otherwise be clipped at the right edge.
                w = max(w, self._ultra_need())
            h = ULTRA_H
        elif self.mode == MODE_COMPACT:
            # height is derived from the GPU count, and it must be re-applied
            # whenever that count changes (driver reinstall brings the cards
            # back one at a time: a window sized while ngpu was still 0/1
            # silently clips the rows that appear later).
            w = COMPACT_W
            h = 32 + COMPACT_ROW_H * n + 6
        else:
            # Width follows the column count. It used to be hardcoded to 524
            # (= two 250px cards + padding), so a third GPU's column was laid
            # out outside the window and simply never appeared.
            w, h = (FULL_CARD_W + FULL_CARD_PAD) * n, FULL_H
        if self.moved:
            # Resize about the bottom-right corner instead of the top-left: a
            # widget parked in a screen corner grows out of that corner, and
            # keeping the top-left fixed made the 104 -> 492 jump shove the
            # window down past the taskbar (and the 492 -> 104 jump left a
            # hole where the header used to be).
            self.root.update_idletasks()  # so winfo reports the live size
            ow = self.root.winfo_width()
            oh = self.root.winfo_height()
            x, y = self.root.winfo_x(), self.root.winfo_y()
            if ow > 1 and oh > 1 and (ow, oh) != (w, h):
                x += ow - w          # keep (x + w, y + h) where it was
                y += oh - h
            self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
            self.ensure_on_screen((w, h))
        else:
            wl, wt, wr, wb = work_area()
            # flush against the edges: right on the screen edge, bottom on the
            # taskbar's top edge (this is the corner the widget is meant to sit
            # in, so no margins)
            self.root.geometry("%dx%d+%d+%d" % (w, h, wr - w, wb - h))

    def ensure_on_screen(self, size=None):
        """Pull the window back onto the desktop.

        Placing the window for a 4K screen and then switching to 2K used to
        leave it off-screen for good: the old anchor is far right of a 2560px
        desktop, and nothing ever moved it again.
        """
        self.root.update_idletasks()  # flush a geometry request made just now
        w, h = size or (self.root.winfo_width(), self.root.winfo_height())
        x, y = self.root.winfo_x(), self.root.winfo_y()
        _, _, vx, vy, vw, vh = desktop_metrics()
        if w <= 0 or h <= 0:
            return
        if vw <= w or vh <= h:
            # desktop smaller than the window: pin to the desktop origin
            if (x, y) != (vx, vy):
                self.root.geometry("+%d+%d" % (vx, vy))
            return
        nx = max(vx, min(x, vx + vw - w))
        ny = max(vy, min(y, vy + vh - h))
        if (nx, ny) != (x, y):
            log("window off desktop (%d,%d) -> (%d,%d)" % (x, y, nx, ny))
            self.root.geometry("+%d+%d" % (nx, ny))

    def _check_display(self):
        """Watch for a resolution / monitor switch and re-place the window.

        Not-moved windows are re-anchored to the new bottom-right corner;
        user-placed ones keep their spot but get clamped back into the new
        desktop (a shrunk one, or a secondary monitor that got unplugged).
        The work area is part of the comparison so a taskbar that changes
        height or auto-hide state re-glues the default spot too.
        """
        m = (desktop_metrics(), work_area())
        if m == self.last_desktop:
            return
        log("display change: %s -> %s" % (self.last_desktop, m))
        self.last_desktop = m
        self.apply_geometry()

    def _bind_drag(self, w):
        """Let any part of the ultra line move the window.

        The compact/full layouts are dragged by their title bar. Ultra has no
        title bar, so the line itself (and every label inside it) has to answer
        the drag events -- Tk delivers them to the widget under the pointer,
        not to the frame around it. Buttons are skipped: pressing one is a
        click on a control, not a grab handle, and a jittering mouse would
        otherwise shove the window across the desktop.
        """
        if isinstance(w, tk.Button):
            return
        w.bind("<ButtonPress-1>", self._press)
        w.bind("<B1-Motion>", self._move)
        for c in w.winfo_children():
            self._bind_drag(c)

    def _ultra_need(self):
        """Width the ultra line asks for: fixed cells plus the two buttons.

        Every GPU gets exactly ULTRA_SEG_W and never more, so this is pure
        arithmetic -- no measuring labels, and the line cannot creep sideways
        while the readings change.
        """
        n = len(self.cards)
        w = ULTRA_PAD + ULTRA_SEG_W * n + ULTRA_SEP_W * max(n - 1, 0)
        # — and the layout button sit at the right end of this very line, so
        # they count towards its width too.
        hide = self.ultra_btn_hide if self.ultra_btn_hide is not None \
            else self.btn_hide
        mode = self.ultra_btn_mode if self.ultra_btn_mode is not None \
            else self.btn_mode
        w += hide.winfo_reqwidth() + 6 + mode.winfo_reqwidth() + 2
        return w

    def cycle_mode(self):
        old = MODE_NAMES[self.mode]
        self.mode = MODE_NEXT[self.mode]
        save_mode(self.mode)
        log("mode: %s -> %s" % (old, MODE_NAMES[self.mode]))
        self.btn_mode.config(text=MODE_NAMES[MODE_NEXT[self.mode]])
        self.build_cards(max(self.ui_ngpu, 1))
        self.apply_geometry()

    def build_cards(self, n):
        for w in self.body.winfo_children():
            w.destroy()
        self.cards = []
        self.canvases = []
        self.pw_canvases = []
        self.ultra_row = None
        self.hist = [deque(maxlen=HIST) for _ in range(n)]
        self.mhist = [deque(maxlen=HIST) for _ in range(n)]
        self.phist = [deque(maxlen=HIST) for _ in range(n)]
        if self.mode == MODE_ULTRA:
            # One single line for the whole fleet: 0 12G 250W | 1 11G 220W ...
            # Only memory in use and power survive the shrink, both without
            # decimals and with no tag in front of them -- the G and the W say
            # which is which. GPUs are told apart by their index, each painted
            # in its own colour -- full-mode graphs all share one colour, so
            # this row is the only place where the cards are colour-coded.
            row = tk.Frame(self.body, bg=CARD, highlightbackground=LINE,
                           highlightthickness=1)
            row.configure(height=ULTRA_ROW_H)
            row.pack_propagate(False)
            row.pack(fill="x", padx=6, pady=1)
            self.ultra_row = row
            mono = ("Consolas", 9)
            bold = ("Consolas", 9, "bold")
            for i in range(n):
                if i:
                    tk.Frame(row, bg=ULTRA_SEP_COLOR, width=2).pack(
                        side="left", padx=2, fill="y")
                # One cell of its own, and its width is fixed. The whole point
                # is that a reading changing on GPU 1 (29W -> 184W) never moves
                # the numbers of GPU 2; each card owns its own slice of pixels.
                cell = tk.Frame(row, bg=CARD, width=ULTRA_SEG_W,
                                height=ULTRA_ROW_H)
                cell.pack_propagate(False)
                cell.pack(side="left", fill="y")
                idx = tk.Label(cell, text=str(i), bg=CARD,
                               fg=TEMP_COLORS[i % len(TEMP_COLORS)],
                               font=("Segoe UI", 9, "bold"))
                idx.pack(side="left", padx=(6, 4), fill="y")
                # Memory in use is the figure you actually scan here, so it
                # gets weight and the brightest ink on the line.
                mu = tk.Label(cell, text="--", bg=CARD, fg=FG, font=bold,
                              padx=0)
                mu.pack(side="left", fill="y")
                # Power sits next to it, quieter and in its own unit; that is
                # what tells the two numbers apart now that the tags are gone.
                mp = tk.Label(cell, text="--", bg=CARD, fg=TXT, font=mono)
                mp.pack(side="left", padx=(4, 4), fill="y")
                self.cards.append({"name": idx, "used": mu, "pw": mp,
                                   "cell": cell})
                self.canvases.append(None)
                self.pw_canvases.append(None)
            # This layout has no title bar at all: the title and the status
            # text vanish with it, and — plus the layout button are built
            # right into the line. Being its children, they are destroyed and
            # rebuilt together with it, so nothing ever has to be reparented
            # and a click lands exactly where it looks like it should.
            self.title_lbl.pack_forget()
            self.status.pack_forget()
            self.btn_hide.pack_forget()
            self.btn_mode.pack_forget()
            self.header.pack_forget()
            self.ultra_btn_hide = tk.Button(row, text="—", command=self.hide,
                                            bg=CARD, fg=DIM, relief="flat",
                                            bd=0, font=("Segoe UI", 10),
                                            activebackground=LINE,
                                            activeforeground=FG)
            self.ultra_btn_hide.pack(side="right", padx=(0, 6), pady=0)
            self.ultra_btn_mode = tk.Button(
                row, text=MODE_NAMES[MODE_NEXT[self.mode]],
                command=self.cycle_mode, bg=CARD, fg=DIM, relief="flat", bd=0,
                font=("Segoe UI", 9), activebackground=LINE,
                activeforeground=FG)
            self.ultra_btn_mode.pack(side="right", padx=(0, 2), pady=0)
            # No title bar to grab: the whole line moves the window.
            self._bind_drag(row)
            return
        # The other two layouts get the title bar back with its own pair of
        # buttons; the line's pair went away with the line.
        self.ultra_btn_hide = None
        self.ultra_btn_mode = None
        # before=self.body matters: the body is packed first, so a header
        # packed after it would end up at the bottom of the window.
        self.header.pack(fill="x", side="top", before=self.body)
        self.title_lbl.pack(side="left")
        self.status.pack(side="left")
        self.btn_hide.pack(side="right", padx=(0, 8), pady=2)
        self.btn_mode.pack(side="right", padx=(0, 2), pady=2)
        for i in range(n):
            if self.mode == MODE_COMPACT:
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
                self.pw_canvases.append(None)
                continue
            col = tk.Frame(self.body, bg=BG, width=FULL_CARD_W)
            col.configure(height=FULL_COL_H)
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

            # Full mode is the only layout that draws history graphs; compact
            # and ultra never get past their own branches above. Two charts
            # stacked in the space that used to hold one: watts on top, degrees
            # underneath. No legend frame any more -- the key is drawn inside
            # each chart, which is where the height for the second one comes
            # from. They sit on the same x mapping, so they read as one
            # timeline cut in two, not as two unrelated plots. The pack order
            # is the on-screen order, so the power canvas has to come first.
            pcv = tk.Canvas(col, bg=CARD, height=GRAPH_H_PW,
                            highlightthickness=0)
            pcv.pack(fill="x", pady=(6, 0))
            cv = tk.Canvas(col, bg=CARD, height=GRAPH_H_TEMP,
                           highlightthickness=0)
            cv.pack(fill="x", pady=(GRAPH_GAP, 0))
            self.canvases.append(cv)
            self.pw_canvases.append(pcv)

    # window drag / visibility
    def _press(self, e):
        # Window-relative offset from absolute numbers: a label inside the
        # ultra line reports its own e.x/e.y, which is not the window's origin.
        self._drag = (e.x_root - self.root.winfo_x(),
                      e.y_root - self.root.winfo_y())

    def _move(self, e):
        if self._drag:
            self.moved = True
            self.root.geometry("+%d+%d" % (e.x_root - self._drag[0],
                                           e.y_root - self._drag[1]))
            self._zorder_soon()

    def hide(self):
        self.visible = False
        self.root.withdraw()

    def show(self):
        self.visible = True
        self.root.deiconify()
        self.root.lift()
        if self.moved:
            # the screen may have changed while we were hidden
            self.ensure_on_screen()
        self.root.after_idle(self.keep_below_taskbar)

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
            self.apply_geometry()  # compact height tracks the GPU count

        with_data = 0
        healthy = 0
        for i in range(max(NVML_STATE["ngpu"], 1)):
            try:
                g = read_gpu(i)
            except Exception as e:
                # A stale NVML handle can raise OSError (access violation)
                # while the driver is torn down/reinstalled. Letting it escape
                # _tick skipped the fail/degraded counters below, so NVML was
                # never re-initialized and the app crashed once a second
                # forever. Treat it as a missing read and let recovery run.
                g = {}
                now = time.time()
                if now - self.read_err_log > 30:
                    self.read_err_log = now
                    log("read gpu%d failed: %r" % (i, e))
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
            # An ultra card has no temperature label at all: the temperature is
            # precisely what that layout throws away.
            if "temp" in card:
                if t is not None:
                    col = "#ff5555" if t >= 80 else "#ffb020" if t >= 68 else "#3ddc84"
                    card["temp"].config(text="%d°C" % t, fg=col)
                else:
                    card["temp"].config(text="--", fg=DIM)
            if self.mode == MODE_ULTRA:
                card["name"].config(text=str(i))
            elif self.mode == MODE_COMPACT:
                card["name"].config(text="GPU %d" % i)
            else:
                card["name"].config(
                    text="GPU %d · %s" % (i, pretty_name(g.get("name"))))
            mem = "%s / %s GB" % (g.get("mem_used", "?"), g.get("mem_total", "?"))
            pwtxt = ("%s / %s W" % (p, g.get("power_limit", "?"))
                     if p is not None else "--")
            mtxt = ("%d °C" % m) if m is not None else "--"
            if self.mode == MODE_ULTRA:
                card["used"].config(text=fmt_mem_used(g.get("mem_used")))
                card["pw"].config(text=fmt_power_w(p))
            elif self.mode == MODE_COMPACT:
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

        # memory-temp blindness: NVML cannot read HBM on this stack (probe
        # rc=2), the nvidia-smi stream is our only source. When it stops
        # feeding us, every other field still looks healthy and only 存温
        # goes "--" -- exactly the post-unlock report, so make it visible in
        # the log instead of invisible.
        if NVML_STATE["ok"] and nseen == 0:
            self.nsmi_streak += 1
            if self.nsmi_streak == 20 or \
                    (self.nsmi_streak > 20 and self.nsmi_streak % 60 == 0):
                log("no nsmi data for %ds: mtemp blind" % self.nsmi_streak)
        else:
            self.nsmi_streak = 0

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

        self._check_display()
        self.keep_below_taskbar()  # activation may have raised us again

    def _chart_frame(self, cv, lo, hi, unit, ink, divisions, pad_b, live):
        """Frame, horizontal grid and axis numbers for one chart.

        Returns (w, h, fn): the canvas size and a value -> y pixel mapping. The
        box is drawn even with nothing to plot -- the layout must not collapse
        into a hole while a driver is being reinstalled -- but a grid printed
        with invented numbers would lie, so the labels keep away in that case.
        """
        w = int(cv.winfo_width() or FULL_CARD_W)
        # Before the first map Tk reports a 1x1 canvas for this window, and a
        # 1px height would put the axis numbers off the canvas. The canvas
        # height is fixed by its own configure(), so the configured value is
        # the truth whenever the widget has not been laid out yet.
        h = int(cv.winfo_height() if cv.winfo_ismapped()
                else cv.cget("height"))
        cv.create_rectangle(0, 0, w - 1, h - 1, outline=LINE)
        if not live:
            return w, h, None
        top, bot = GRAPH_PAD_T, h - pad_b

        def y(v):
            return top + (bot - top) * (1 - (v - lo) / (hi - lo))

        for k in range(divisions + 1):
            yy = top + (bot - top) * k / divisions
            cv.create_line(GRAPH_PAD_L, yy, w - GRAPH_PAD_R, yy, fill=LINE)
            cv.create_text(4, yy, text="%d%s" % (
                round(hi - (hi - lo) * k / divisions), unit), fill=ink,
                font=("Segoe UI", 8), anchor="w")
        return w, h, y

    def draw_graph(self, i):
        # One canvas per GPU used to carry three series on two scales at once:
        # degrees read off the left edge, watts off the right, and both sets of
        # numbers printed on the very same grid lines -- so the two units were
        # only told apart by which side of the chart their numbers sat on. Core
        # temp and mem temp are degrees and share one scale, so they stay
        # together in one chart; watts have nothing in common with degrees and
        # get a chart plus a grid of their own. Power is drawn first and so
        # sits on top, temperatures underneath. Both charts use the same x
        # mapping, which is what makes them read as one timeline cut in two
        # rather than as two unrelated plots.
        cv_t, cv_p = self.canvases[i], self.pw_canvases[i]
        if cv_t is None:
            return  # compact / ultra mode: no graph at all
        cv_t.delete("all")
        cv_p.delete("all")

        tv = [v for v in self.hist[i] if v is not None]
        mv = [v for v in self.mhist[i] if v is not None]
        pv = [v for v in self.phist[i] if v is not None]
        temp_all = tv + mv

        tlo, thi = (min(temp_all) - 2, max(temp_all) + 2) if temp_all else (0, 10)
        if thi - tlo < 10:
            mid = (thi + tlo) / 2
            tlo, thi = mid - 5, mid + 5
        plo, phi = (min(pv) - 5, max(pv) + 5) if pv else (0, 100)
        if phi - plo < 20:
            mid = (phi + plo) / 2
            plo, phi = max(0, mid - 10), mid + 10

        # pad_b follows the position in the stack, not the unit: the chart that
        # ends the pair at the bottom pays for the time axis, the one above it
        # reads straight down to it.
        w_p, h_p, y_p = self._chart_frame(cv_p, plo, phi, "W", PW_COLOR,
                                          3, GRAPH_PAD_B_TOP, bool(pv))
        w_t, h_t, y_t = self._chart_frame(cv_t, tlo, thi, "°", GRAPH_TEMP,
                                          4, GRAPH_PAD_B, bool(temp_all))

        # The key used to be a legend frame hanging under the graph. It now
        # sits inside each chart's own top strip: two charts no longer need two
        # captions of their own, and the height that frees is exactly what the
        # second chart costs.
        cv_t.create_text(GRAPH_PAD_L, GRAPH_KEY_Y, text="— 核温",
                         fill=GRAPH_TEMP, font=("Segoe UI", 9), anchor="w")
        cv_t.create_text(GRAPH_PAD_L + GRAPH_KEY_GAP, GRAPH_KEY_Y,
                         text="— 存温", fill=GRAPH_MEM,
                         font=("Segoe UI", 9), anchor="w")
        cv_p.create_text(GRAPH_PAD_L, GRAPH_KEY_Y, text="┄ 功耗",
                         fill=PW_COLOR, font=("Segoe UI", 9), anchor="w")

        # One timeline, so the time axis is printed once, at the very bottom of
        # the pair -- which is now the temperature chart -- and the chart above
        # it simply reads down to it. The oldest sample is index 0 and the
        # newest is the last one appended, so the left end is three minutes
        # back and the right end is now.
        cv_t.create_text(GRAPH_PAD_L, h_t - 5, text="-3 min", fill=DIM,
                         font=("Segoe UI", 8), anchor="w")
        cv_t.create_text(w_t - GRAPH_PAD_R, h_t - 5, text="now", fill=DIM,
                         font=("Segoe UI", 8), anchor="e")

        # Close over the width rather than take it as an argument: a helper that
        # takes (index, width) is one swapped call away from a mapping that
        # silently smears the oldest samples left over the axis numbers and
        # squeezes the rest into less than the full width -- and the curves still
        # look plausible while it does. Both charts are the same width, so one
        # span serves them both.
        span = w_t - GRAPH_PAD_L - GRAPH_PAD_R

        def px(j):
            return GRAPH_PAD_L + span * j / (HIST - 1)

        if y_t is not None:
            pts_t = [(px(j), y_t(v)) for j, v in enumerate(self.hist[i])
                     if v is not None]
            if len(pts_t) >= 2:
                cv_t.create_line(pts_t, fill=GRAPH_TEMP, width=2, smooth=True)
            pts_m = [(px(j), y_t(v)) for j, v in enumerate(self.mhist[i])
                     if v is not None]
            if len(pts_m) >= 2:
                cv_t.create_line(pts_m, fill=GRAPH_MEM, width=2, smooth=True)
        if y_p is not None:
            pts_p = [(px(j), y_p(v)) for j, v in enumerate(self.phist[i])
                     if v is not None]
            if len(pts_p) >= 2:
                cv_p.create_line(pts_p, fill=PW_COLOR, width=1, dash=(4, 3),
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
        nsmi_child_kill()   # otherwise the stream child outlives us forever
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
        nsmi_child_kill()
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
