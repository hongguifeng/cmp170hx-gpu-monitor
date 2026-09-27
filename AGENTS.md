# AGENTS.md

本文件面向在此项目上工作的 AI 编码代理（Agents），定义工作准则与项目背景知识。

## 项目简介

GPU Monitor：针对 CMP 170HX × 2（MCDM 模式）的显卡监控小工具。基于 NVML 读取显卡指标，
HBM 存温通过常驻 `nvidia-smi -l 1` 流式进程解析获得。主要文件：

- `gpu-monitor-tray.py` —— 托盘 + 悬浮窗（主要交付物）
- `gpu-monitor.py` —— 网页仪表盘（可选）
- `watchdog.ps1` —— 看门狗，进程意外死亡自动拉起
- `monitor.log` / `compact.flag` / `no-restart.flag` —— 运行时产物

## 工作准则

### 1. 改动必须验证：编译 + 运行 + UI 截图确认

- 任何涉及代码改动的任务，完成后**必须编译/打包并实际运行**，确认功能正常，不能只改完代码就交付。
- 涉及界面（悬浮窗、托盘图标、网页仪表盘等）的改动，必须**通过截图确认 UI 显示符合预期**后再交付。
- 验证过程发现问题，先修复再重新验证，直至通过。

### 2. UI 改动完成后同步更新 docs 截图

- 涉及 UI 改动的任务，完成后必须**同步更新 `docs/img/` 中的截图文件**（如 `window-compact.png`、`window-full.png`、`tray-icons.png`、`web-dashboard.png`），使其与新的界面状态一致（README.md 引用了这些截图）。

### 3. 知识沉淀到本文件

- 开发过程中遇到的问题及解决办法、或发现的有用知识，若确认**对本项目后续开发有用**，追加到下方「项目知识库」章节。
- 若知识仅对当前任务有用（一次性、局部），则**不要**添加进来。
- 知识条目建议简短、可操作；注明来源文件/命令以便日后查证。

## 项目知识库

（在此追加对本项目后续开发有用的经验与知识。示例格式：）

### 替换根目录 GPU-Monitor.exe 必须先停看门狗

- 现象：`cp dist/GPU-Monitor.exe .` 报 `Device or resource busy`；杀掉主进程后它又被自动拉起。
- 原因：`watchdog.ps1` 每 10 秒查活并 `Start-Process` 重启；运行中的 exe 文件被占用无法覆盖。
- 做法：先按 CommandLine 匹配 `*watchdog.ps1*` 找到并杀掉 powershell 看门狗进程 → `Stop-Process -Name GPU-Monitor` → 再覆盖 exe → `Start-Process` 启动新 exe（它会自己重新拉起看门狗）。
- 坑：git-bash 会把 `taskkill /IM`、`/PID` 参数当成路径转换，导致参数失效；改用 powershell 的 `Stop-Process`。

### UI 验证截图方法（无 GUI 自动化工具时）

- 抓屏：powershell `Add-Type System.Windows.Forms,System.Drawing` + `Graphics.CopyFromScreen` 存 PNG，再用 PIL 裁剪。
- 悬浮窗默认位置在右下角：`x = 屏宽 - w - 16`，`y = 屏高 - h - 86`；精简模式 w=440、h=32+30*卡数+6；完全模式 524x492。
- 托盘图标不必去任务栏找（可能被收纳在溢出区）：用 `importlib.util.spec_from_file_location` 加载 `gpu-monitor-tray.py`，直接调 `make_tray_image()` / `tray_image_for()` 渲染——这正是程序实际使用的同一代码路径，渲染结果即真实效果。

### docs/img 截图没有自动生成脚本

- `docs/img/tray-icons.png` 是手工拼合的 672x240 画布（背景 RGB(20,22,26)），三个 185px 图标粘贴在 x=23 / 239 / 455，y=23。
- 只改配色时不必重画整图：把原图对应图标按抗锯齿线性反解 `t = (p.b - 38) / (255 - 38)`，再取 `新色 = (28,31,38) + t * (目标色 - (28,31,38))` 重绘，可与原图风格完全一致。

<!--
### 某某问题
- 现象：...
- 原因：...
- 解决办法：...
-->
