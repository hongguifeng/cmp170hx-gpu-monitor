# GPU Monitor

[![Build Windows EXE](https://github.com/hongguifeng/cmp170hx-gpu-monitor/actions/workflows/build-exe.yml/badge.svg)](https://github.com/hongguifeng/cmp170hx-gpu-monitor/actions/workflows/build-exe.yml)

A small GPU monitoring utility for **CMP 170HX × 2 in MCDM mode**.

📖 Chinese documentation: [README.md](README.md) · this file is the English version.

In MCDM mode, NVAPI-based tools (GPU-Z, LibreHardwareMonitor, ...) **cannot see the card at all**.
This tool talks over the **NVML channel** (the same source `nvidia-smi` uses), so it reliably reads
core temperature, HBM memory temperature, power draw and VRAM usage — and it reconnects by itself
after the NVIDIA driver is uninstalled and reinstalled (the unlock procedure keeps swapping the driver).

## UI preview

System tray icon — always shows the temperature of the **hottest GPU** and changes colour with it
(green &lt; 68°C · orange ≥ 68°C · red ≥ 80°C):

![Tray icons](docs/img/tray-icons.png)

Always-on-top floating window · compact mode (one row per GPU, stays out of the way):

![Compact mode](docs/img/window-compact.png)

Always-on-top floating window · full mode (one card per GPU + 3-minute temp / memory-temp / power graphs):

![Full mode](docs/img/window-full.png)

Web dashboard (`gpu-monitor.py`, optional, see below):

![Web dashboard](docs/img/web-dashboard.png)

## Features

- **Tray-resident** — the icon shows the current maximum temperature in real time; a single click on the
  tray icon shows / hides the floating window
- **Always-on-top floating window** — drag the title bar to move it; `—` hides it; two layouts
  (compact / full) can be switched, and the preference persists across restarts (`compact.flag`)
- **Per-GPU readings** (NVML) — name, core temperature, GPU / memory clocks, utilisation, fan,
  VRAM usage, live power draw and power limit
- **HBM memory temperature** — NVML's `TEMPERATURE_MEMORY` returns `NOT_SUPPORTED` on this platform,
  so the value is obtained by a long-lived `nvidia-smi -l 1` streaming process that parses
  `temperature.memory` (entries expire after 5 seconds, which rules out "frozen" stale temperatures)
- **Survives driver removal** — three self-healing paths: all readings dead, individual GPUs degraded,
  or the NVML device list gone stale (cross-checked against a fresh nvidia-smi enumeration); each of
  them forces NVML to be rebuilt, so monitoring comes back after a driver reinstall
- **Start on boot** — a tray menu checkbox writes the `HKCU\...\Run` key; it survives driver reconnects
- **Watchdog** — `watchdog.ps1` checks liveness every 10 seconds and restarts the process if it dies
  unexpectedly; once the tray menu "Quit" writes `no-restart.flag`, the watchdog honours that intent
- **Crash black box** — `faulthandler` plus a global `excepthook` send hard crashes and uncaught
  exceptions to `monitor.log`
- **Single instance** — a named mutex stops two copies from interfering with each other

## Usage

### Tray app (recommended)

Double-click `GPU-Monitor.exe` — no installation and no Python required.
On first run it is a good idea to tick "Start on boot" in the tray menu.

### Run from source

```bash
pip install pystray pillow
python gpu-monitor-tray.py
```

### Web dashboard (optional second form)

```bash
python gpu-monitor.py
# or start it without a visible console: double-click start-gpu-monitor.vbs
```

Open <http://127.0.0.1:8765> in a browser — it refreshes every second and shows temperature, clocks,
utilisation, fan, power / power limit, VRAM usage bars and the last characters of each GPU UUID.

## Files

| File | Purpose |
| --- | --- |
| `gpu-monitor-tray.py` | Main program: tray + floating window + NVML / nvidia-smi self-healing logic |
| `gpu-monitor.py` | Lightweight variant: NVML → local web dashboard (port 8765) |
| `watchdog.ps1` | Watchdog: restarts the process when it dies |
| `start-gpu-monitor.vbs` | Hidden launcher for the web dashboard |
| `GPU-Monitor.spec` | PyInstaller build configuration |
| `monitor.ico` | Application icon |
| `GPU-Monitor.exe` | Build output (not tracked in git) |

## Troubleshooting

All runtime diagnostics (NVML reconnects, GPU count changes, why a forced rebuild happened, crash
stack traces) are appended to `monitor.log` — that file is the first thing to look at when something
misbehaves.

## Rebuilding

```bash
pip install pyinstaller pystray pillow
pyinstaller GPU-Monitor.spec
```

The result is `dist/GPU-Monitor.exe`, which can replace the exe in the repository root.
To replace the running exe, stop the watchdog first (see [AGENTS.md](AGENTS.md)).

## Building in GitHub CI

The repository ships `.github/workflows/build-exe.yml`: pushing to `master`, opening a pull request,
or running the workflow manually (Actions → "Build Windows EXE" → Run workflow) builds the exe on a
`windows-latest` runner with PyInstaller using `GPU-Monitor.spec`, then uploads
`dist/GPU-Monitor.exe` as an artifact named **GPU-Monitor-windows**, kept for 30 days. You can download
that exe straight from the Artifacts section of the workflow run page — no local toolchain required.

## Known limitations

- Graphs cover only the last **3 minutes** (180 one-second samples)
- `Fan %` is unavailable on some BIOS / driver combinations (shown as `-- %`)
- The floating window is borderless (`overrideredirect`), so it can only be dragged by its title bar
