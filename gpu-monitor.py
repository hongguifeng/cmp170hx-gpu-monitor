# -*- coding: utf-8 -*-
"""GPU mini monitor: NVML (nvidia-smi channel) -> local web dashboard.

Works in MCDM mode where NVAPI-based tools (GPU-Z / LibreHardwareMonitor)
cannot see the GPUs. Serves http://127.0.0.1:8765
"""
import ctypes
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
nv = ctypes.WinDLL("nvml.dll")

NVML_TEMPERATURE_GPU = 0
NVML_CLOCK_GRAPHICS = 0
NVML_CLOCK_MEM = 2


class NvmlMemory(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong),
                ("free", ctypes.c_ulonglong),
                ("used", ctypes.c_ulonglong)]


class NvmlUtil(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


def _init():
    r = nv.nvmlInit_v2()
    if r != 0:
        raise RuntimeError("nvmlInit_v2 rc=%d" % r)
    n = ctypes.c_uint()
    nv.nvmlDeviceGetCount_v2(ctypes.byref(n))
    return n.value


def read_gpu(idx):
    h = ctypes.c_void_p()
    nv.nvmlDeviceGetHandleByIndex_v2(ctypes.c_uint(idx), ctypes.byref(h))
    d = {"index": idx}

    name = ctypes.create_string_buffer(96)
    if nv.nvmlDeviceGetName(h, name, 96) == 0:
        d["name"] = name.value.decode("utf-8", "replace")

    uuid = ctypes.create_string_buffer(96)
    if nv.nvmlDeviceGetUUID(h, uuid, 96) == 0:
        d["uuid"] = uuid.value.decode("ascii", "replace")[-8:]

    t = ctypes.c_uint()
    if nv.nvmlDeviceGetTemperature(h, NVML_TEMPERATURE_GPU,
                                   ctypes.byref(t)) == 0:
        d["temp"] = t.value

    clocks = {}
    for label, ctype in (("gpu", NVML_CLOCK_GRAPHICS), ("mem", NVML_CLOCK_MEM)):
        c = ctypes.c_uint()
        if nv.nvmlDeviceGetClockInfo(h, ctypes.c_uint(ctype),
                                     ctypes.byref(c)) == 0:
            clocks[label] = c.value
    d["clocks"] = clocks

    mem = NvmlMemory()
    if nv.nvmlDeviceGetMemoryInfo(h, ctypes.byref(mem)) == 0:
        d["mem_total"] = round(mem.total / (1 << 30), 1)
        d["mem_used"] = round(mem.used / (1 << 30), 1)
        d["mem_pct"] = round(100.0 * mem.used / mem.total, 1) if mem.total else 0

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
    return d


PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GPU 监控 · NVML 通道</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
    background: #14161a; color: #e8eaf0; min-height: 100vh;
    display: flex; flex-direction: column; align-items: center;
    padding: 28px 16px;
  }
  h1 { font-size: 18px; font-weight: 600; color: #9aa3b2; margin-bottom: 20px; }
  h1 b { color: #e8eaf0; }
  .row { display: flex; gap: 20px; flex-wrap: wrap; justify-content: center; }
  .card {
    width: 340px; background: #1c1f26; border: 1px solid #2a2f3a;
    border-radius: 14px; padding: 20px 22px;
  }
  .card h2 { font-size: 15px; font-weight: 600; margin-bottom: 2px; }
  .card .sub { font-size: 12px; color: #7d8595; margin-bottom: 14px; }
  .temp { font-size: 44px; font-weight: 700; line-height: 1; }
  .temp small { font-size: 18px; font-weight: 400; color: #7d8595; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 14px;
          margin-top: 14px; font-size: 13px; }
  .grid .k { color: #7d8595; }
  .grid .v { text-align: right; font-variant-numeric: tabular-nums; }
  .bar { height: 8px; background: #2a2f3a; border-radius: 4px;
         margin-top: 14px; overflow: hidden; }
  .bar > div { height: 100%; background: #4f8cff; border-radius: 4px;
               transition: width .5s; }
  .bar-label { display: flex; justify-content: space-between; font-size: 12px;
               color: #7d8595; margin-top: 6px; }
  .stale { opacity: .45; }
  #status { margin-top: 22px; font-size: 12px; color: #7d8595; }
</style>
</head>
<body>
<h1>GPU 监控 <b>· NVML 通道</b>(MCDM 兼容)</h1>
<div class="row" id="cards"></div>
<div id="status">连接中…</div>
<script>
const tcolor = t => t >= 80 ? "#ff5555" : t >= 68 ? "#ffb020" : "#3ddc84";
async function tick() {
  try {
    const r = await fetch("/api");
    const gpus = await r.json();
    const box = document.getElementById("cards");
    box.innerHTML = gpus.map((g, i) => {
      const t = g.temp ?? "--";
      const mem = (g.mem_used != null)
        ? g.mem_used + " / " + g.mem_total + " GB" : "--";
      return `<div class="card">
        <h2>GPU ${g.index} · ${g.name || "NVIDIA GPU"}</h2>
        <div class="sub">${g.uuid ? "UUID 尾号 " + g.uuid : ""}</div>
        <div class="temp" style="color:${typeof t == "number" ? tcolor(t) : "#7d8595"}">${t}<small> °C</small></div>
        <div class="grid">
          <span class="k">GPU 频率</span><span class="v">${g.clocks?.gpu ?? "--"} MHz</span>
          <span class="k">显存频率</span><span class="v">${g.clocks?.mem ?? "--"} MHz</span>
          <span class="k">利用率</span><span class="v">${g.util ?? "--"} %</span>
          <span class="k">风扇</span><span class="v">${g.fan ?? "--"} %</span>
          <span class="k">功耗</span><span class="v">${g.power != null ? g.power + " W" : "--"}${g.power_limit ? " / " + g.power_limit + " W" : ""}</span>
        </div>
        <div class="bar"><div style="width:${g.mem_pct ?? 0}%"></div></div>
        <div class="bar-label"><span>显存占用</span><span>${mem}</span></div>
      </div>`;
    }).join("");
    document.getElementById("status").textContent =
      "更新于 " + new Date().toLocaleTimeString() + " · 每秒自动刷新";
  } catch (e) {
    document.getElementById("status").textContent = "连接断开,重试中…";
  }
}
tick();
setInterval(tick, 1000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api":
            try:
                data = [read_gpu(i) for i in range(COUNT)]
                body = json.dumps(data).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body)
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode("utf-8")
                self._send(500, "application/json", body)
        elif self.path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        else:
            self._send(404, "text/plain", b"not found")


COUNT = _init()
print("NVML ok, %d GPU(s); serving http://127.0.0.1:%d" % (COUNT, PORT), flush=True)
print("FINISHED_INIT", flush=True)
ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
