# AGENTS.md

本文件面向在此项目上工作的 AI 编码代理（Agents），定义工作准则与项目背景知识。

## 项目简介

GPU Monitor：针对 CMP 170HX × 2（MCDM 模式）的显卡监控小工具（也支持同机混插的 GeForce 卡）。
基于 NVML 读取显卡指标；CMP 的 HBM 存温由常驻 `nvidia-smi -l 1` 流式进程解析，
GDDR6X（如 RTX 3090）的存温走 NVAPI。主要文件：

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
- 坑：本机 `[System.Windows.Forms.Screen]::PrimaryBounds` 返回 null（Bitmap 构造随之失败），改用 `[System.Windows.Forms.SystemInformation]::VirtualScreen`（物理分辨率 3840x2160）。
- 悬浮窗默认位置在右下角：`x = 屏宽 - w - 16`，`y = 屏高 - h - 86`；精简模式 w=440、h=32+33*卡数+6；完全模式 w=262*卡数、h=492。
- 托盘图标不必去任务栏找（可能被收纳在溢出区）：用 `importlib.util.spec_from_file_location` 加载 `gpu-monitor-tray.py`，直接调 `make_tray_image()` / `tray_image_for()` 渲染——这正是程序实际使用的同一代码路径，渲染结果即真实效果。

### docs/img 截图没有自动生成脚本

- `docs/img/tray-icons.png` 是手工拼合的 672x240 画布（背景 RGB(20,22,26)），三个 185px 图标粘贴在 x=23 / 239 / 455，y=23。
- 只改配色时不必重画整图：把原图对应图标按抗锯齿线性反解 `t = (p.b - 38) / (255 - 38)`，再取 `新色 = (28,31,38) + t * (目标色 - (28,31,38))` 重绘，可与原图风格完全一致。

### GitHub CI 打包产物可直接下载验证

- CI：`.github/workflows/build-exe.yml`（push master / PR / 手动触发 → `windows-latest` + `pip install -r requirements.txt` + `pyinstaller GPU-Monitor.spec`，artifact 名 `GPU-Monitor-windows`）。
- CI 同时把 exe 发成 GitHub Release：推 `master` → 滚动预发布 `ci-latest`（每次都 delete `--cleanup-tag` 后 `create --target <sha>` 重建，否则 `ci-latest` tag 会停在首次构建的 commit）；推 tag `v*` → 以 tag 命名的正式版。用 runner 自带的 `gh` + `GH_TOKEN: secrets.GITHUB_TOKEN`（需 `permissions: contents: write`），公开免登录下载。
- 查运行状态：仓库公开，无需 token —— `curl https://api.github.com/repos/hongguifeng/cmp170hx-gpu-monitor/actions/runs/<run_id>` 与 `/artifacts`、`/jobs`。
- 下载 artifact 需要 token：`printf "protocol=https\nhost=github.com\n\n" | git credential fill`（wincred 中存的是 `gho_` PAT）→ `curl -L -H "Authorization: Bearer $TOK" <archive_download_url>`。
- 现象：CI 产物（约 19.5MB）比本地产物（约 18.7MB）大 —— GitHub runner 上没有 UPX，spec 里的 `upx=True` 会被静默跳过。
- 坑：本机没装 `gh` CLI；`Get-Process GPU-Monitor` 会看到 **2 个**同路径进程，那是 PyInstaller onefile 的父/子进程，不是重复启动。

### 改显卡数量后必须重新应用窗口尺寸（“只显示一个 GPU”的真凶）

- 现象：重启后走解锁流程（驱动卸载重装），日志里 `ngpu 1 -> 2, rebuild UI` 明明执行了，状态栏也写「NVML · 2 卡」，但悬浮窗只有一行 GPU。
- 原因：精简模式的窗高由卡数推导（`32 + 33*n + 6`），而 `apply_geometry()` 只在 `__init__` / 切模式时调用。若程序是在驱动只认出 1 张卡（甚至 0 张，`reinit: init rc=6`）时被看门狗拉起的，窗高就锁死在 1 行；之后 `build_cards(2)` 建出第二张卡的行，但它落在窗口可视区之外（Tk 里该行 `winfo_ismapped()==0`），看起来就是“只有一张卡”。
- 修复：`_tick` 里卡数变化重建卡片后补一次 `apply_geometry()`；同时用 `self.moved` 记录用户是否拖动过窗口，拖过就只 resize 不改位置（否则用户摆好的位置会被弹回右下角）。
- 排查手法：`python test-geometry.py`（强制 `NVML_STATE["ngpu"]=1` 再走真实 `_tick` 恢复），断言 `32 + body.winfo_reqheight() <= win_h` 且每行 `winfo_ismapped()`。只改 `NVML_STATE` 不改真实 NVML 即可复现。
- 截取过渡态截图前要 `for aid in root.tk.call("after", "info"): root.after_cancel(aid)`，否则 `MonitorApp.__init__` 注册的 `after(300, tick)` 会提前把状态推进到 2 卡。

### 验证脚本不要向控制台打印非 ASCII

- 现象：`python -c "print(...)"` 打印中文/emoji 时报 `UnicodeEncodeError: 'gbk' codec can't encode character`，整条 `cmd && cmd` 链因此中断。
- 原因：本机控制台默认 GBK 编码，而文件内容本身是 UTF-8（`open(..., encoding='utf-8')` 读入没问题）。
- 做法：验证脚本只打印 ASCII，或先设 `PYTHONIOENCODING=utf-8`；检查文件内容用 `file` / `grep` 而不是 `print`。

### 存温一直 "--" 的根因：nvidia-smi 流进程卡死（已修）

- 现象：开机自启后紧接着跑解锁，NVML 重连成功、其它字段全恢复，唯独存温一直 `--`；只有退出并重启工具才恢复。
- 原因：存温只来自常驻 `nvidia-smi -l 1` 子进程。设备被禁用 / 驱动重装的窗口里该子进程**不退出**，只是不再吐有效数据；
  旧 reader 卡在 `readline()`，而 60s 重启检查只在“读到一行之后”才执行（解析失败还先 `continue` 跳过它）→ 流永不重启 → `NSMI` 永远为空。
- 铁证：解锁前 1 秒 spawn 的那个 nvidia-smi 进程，5.5 小时后仍在运行且父进程已消失 → 它在整场解锁期间都没退出过。
- 修法：读输出挤到 pump 线程 + `Queue.get(timeout=)`；无输出 20s / 无有效数据 15s / 每 60s 都重启并 kill；
  `[N/A]` 只丢存温不丢核温；退出（托盘退出、关窗）时 `nsmi_child_kill()` 不再留孤儿。日志关键字：`nsmi stream silent` / `fed no data` / `mtemp blind`。
- 验证手法：`NSMI_CMD` 可整体换成假的 nvidia-smi 脚本，分别模拟“吐 `No devices were found` 但永不退出”和“彻底不出声”，
  新旧代码各跑一遍对比（旧版 1 次 spawn 后永久失明；新版 15~20s 内自愈并留下日志）。另：旧版的 `NSMI_CMD` 没人用，
  要测旧代码得 monkeypatch `mod.subprocess.Popen` 把 `nvidia-smi` 换掉，否则会默默跑成真的 nvidia-smi。

### 截图验证优先用 PIL ImageGrab（本机 Add-Type 不可用）

- 本机 `Add-Type System.Windows.Forms,System.Drawing` 报 TypeNotFound（Bitmap / Graphics 都拿不到），powershell 截图路线不可靠。
- 可用：`from PIL import ImageGrab; ImageGrab.grab(all_screens=True)`（3840x2160），再用 `win32gui.EnumWindows` +
  `win32process.GetWindowThreadProcessId` 找标题为 `GPU Monitor` 的窗口，按 `GetWindowRect` 裁剪即为完整悬浮窗。
- 窗体尺寸可反推模式与卡数：精简 440 x (32+33*n+6)，完全 262*n x 492。

### git-bash 里跑 PowerShell 脚本的坑

- `-File /tmp/x.ps1` 要先 `cygpath -w` 转成 Windows 路径，否则脚本根本不会执行（表现为“没有任何输出 + rc=4294967295”，但可能已部分执行）。
- 用 `sed` 改写 ps1 里的 Windows 路径时，替换串中的 `\U` 会被当成“转大写”修饰符，路径会变乱码；直接用 write 工具写脚本文件。
- `whoami /groups` 会被 coreutils 的 whoami 抢先，要用 `/c/Windows/System32/whoami.exe`；判断是否管理员用 `net session`（拒绝访问 = 非管理员，所以无法用禁用设备来复现解锁场景）。
- 清理遗留的 nvidia-smi 孤儿流：先用 `Get-Process -Id <ppid>` 确认父进程已消失再 `Stop-Process`，否则会误杀现役实例的流。

### 切换分辨率后悬浮窗跑到桌面外（已修）

- 现象：4K → 2K 之后小控件“不见了”；它仍停在为 4K 算出的右下角坐标（x=3384 > 2560），永远在屏幕外。
- 原因：`winfo_screenwidth/height` 是 Tk 在解释器启动时缓存的，换屏后不会变；而锚点只在 `__init__` / 切模式时算一次，换屏事件根本没人处理。
- 做法：`_tick` 每秒用 `user32.GetSystemMetrics` 取实时桌面尺寸（SM_CXSCREEN=0、SM_CYSCREEN=1；虚拟桌面 SM_XVIRTUALSCREEN=76、SM_YVIRTUALSCREEN=77、SM_CXVIRTUALSCREEN=78、SM_CYVIRTUALSCREEN=79），变了就重新 `apply_geometry()`：没拖动过的重新锚到新右下角，拖动过的用 `ensure_on_screen()` 夹回桌面内（`show()` 里也补一次，防止切屏时窗口是隐藏的）。日志关键字：`display change` / `window off desktop`。
- 注意：Windows 里主屏左上角恒为 (0,0)，所以 Tk 的 `+x+y` 就是绝对屏幕坐标；副屏坐标可为负，夹取范围要用虚拟桌面而不是主屏。

### Tk 窗口坐标与尺寸的读取坑

- 已经映射过的窗口，即使 `withdraw()` 之后 `winfo_x/y/width/height` 仍然准确（实测与 `GetWindowRect(GetAncestor(winfo_id(), GA_ROOT))` 一致）；从未映射过的新窗口则报 (0,0) 和 1x1，必须先 `root.update()` 跑一轮事件才会落位。
- `geometry("%dx%d")` 这类只改尺寸的调用不会改位置，但 `winfo_width/height` 要 `update_idletasks()` 才反映新值。
- 本机系统 DPI=96（100% 缩放）、进程 DPI awareness=0，因此 GetSystemMetrics 与 Tk 坐标同为物理像素，可直接混用。

### 验证脚本看不到 traceback（被项目 excepthook 吞掉）

- 现象：测试脚本报错时 stderr 全空、只剩 rc=1，栈信息默默写进了 monitor.log。
- 原因：`gpu-monitor-tray.py` 在 import 时就设置 `sys.excepthook = _excepthook`，用 importlib 加载它之后对宿主脚本也生效。
- 做法：加载模块后立刻 `sys.excepthook = sys.__excepthook__`。另：Windows 版 python 认不出 git-bash 的 `/tmp/...`，脚本路径要先 `cygpath -w`。

### 本机没法真的切分辨率来复现换屏问题

- 显示设备名是通用驱动 `CDD`（CMP 170HX 没有视频输出），`ChangeDisplaySettingsExW(..., CDS_TEST)` 对 1920x1080、2560x1440 等模式一律返回 -4（DISP_CHANGE_BADFLAGS），虽然这些模式都能枚举到。
- 结论：验证“换屏后重定位”只能 monkeypatch `desktop_metrics` 返回假桌面尺寸来驱动真实代码路径（真实 Tk 窗口确实会被移动，可截图确认），不要指望程序化换分辨率。

### 改窗口尺寸必须以右下角为锚点（项目约定）

- 约定：用户拖动过的悬浮窗（`self.moved`）变尺寸时（精简 ⇄ 完全、显卡数变化）必须保持右下角不动，而不是左上角。
- 原因：以左上角为锚点时 104 → 492 的高度跳变会把窗口推到任务栏以下甚至屏幕外，492 → 104 则会在原位置留个空洞。
- 做法：`apply_geometry()` 的 moved 分支先 `update_idletasks()` 拿当前 `winfo_width/height`，再 `x += ow - w; y += oh - h`，最后 `geometry("WxH+X+Y")` + `ensure_on_screen()`；回归断言在 `test-geometry.py` 的第 [4][5] 步。

### 验证脚本会误改用户的 compact.flag

- 现象：跑过包含 `toggle_compact()` 的验证脚本后，`compact.flag` 消失，`test-geometry.py` 第 [1] 步变成完全模式（win_h=492），第 [2] 步 `h2 > h1` 断言假失败。
- 原因：`toggle_compact()` 会 `save_compact()` —— 文件存在与否就是用户的跨重启偏好，测试一旦切换就把它改写了。
- 做法：测试里直接设 `app.compact` + `build_cards()` + `apply_geometry()`，不走按钮命令；若确实要模拟点按钮，先记录 `os.path.exists(MODE_FLAG)`，结束后再 `touch compact.flag` 恢复。（跑 test-geometry.py 前先确认 compact.flag 存在。）

### 真点击 exe 按钮做端到端 UI 验证（无需 GUI 自动化框架）

- 用 `user32.SetCursorPos` + `mouse_event(LEFTDOWN/LEFTUP)` 直接点屏幕绝对坐标；先 `GetCursorPos` 存下原光位置，结束后 `SetCursorPos` 回去。
- 标题栏上的「精简/完全」按钮约在 `rect.r - 58, rect.t + 16`（`—` 最靠右）；`rect.r - 40` 可能落在两个按钮之间的空隙，必要时在 `r-45..r-65` 间扫点，每点一次用 `GetWindowRect` 确认宽度是否切换。
- 验证方法：前后各用 `win32gui.EnumWindows` 找标题 `GPU Monitor` 的窗口，比较 `GetWindowRect` 的尺寸与右下角，再截图。分层窗口建议用 PrintWindow，见下条。

### 默认落位要贴边：用 SPI_GETWORKAREA，不要硬编码边距

- 旧做法 `x = sw - w - 16, y = sh - h - 86` 把任务栏当成 48px，结果上边缘离任务栏还空 38px、右边缘离屏幕空 16px，换机器 / 换任务栏高度就错位。
- 做法：`user32.SystemParametersInfoW(SPI_GETWORKAREA=0x0030, 0, byref(RECT), 0)` 拿**实时**工作区（已扣除任务栏与托盘），默认锚点就取它的右下角：`x = wa.right - w, y = wa.bottom - h`。任务栏变高、改成自动隐藏或停靠到其它边，下一 tick 就会重新贴边（前提：窗口没被用户拖动过）。
- 坑：`_check_display` 的比较元组必须把 `work_area()` 一并放进去（`(desktop_metrics(), work_area())`），否则任务栏变化不会触发重新落位；而 `ensure_on_screen` 的夹取仍按整个虚拟桌面算 —— 被夹回的窗口可能压在任务栏上，但悬浮窗是 `-topmost`，会盖在任务栏之上而不是被藏掉。

### nvidia-smi 在 Windows 不报 RTX 3090 显存温度，改走 NVAPI

- 现象：`nvidia-smi --query-gpu=temperature.memory` 对 RTX 3090 返回 `N/A`，精简模式里 3090 的「存温」永远是 `--`；两张 CMP 的 HBM 存温正常。
- 澄清：NVML 的 `nvmlTemperatureSensors_t` 只有 `NVML_TEMPERATURE_GPU=0`，不存在 memory 枚举；旧注释里「`TEMPERATURE_MEMORY` 返回 NOT_SUPPORTED」不对，实际 rc=2 是 `INVALID_ARGUMENT`。
- 做法：用 `nvapi64.dll` 的 `nvapi_QueryInterface(0x65FE3AAD)` 取 `NvAPI_GPU_GetThermalSensors`。结构为 `version, mask, reserved[8], temperatures[32]`（`_pack_=8`，version=`0x000200A8`），mask 是按位选择的传感器集合，温度是 8.8 定点数（除以 256）。Ampere（3090）存温在 `temperatures[9]`；40 系用 7、50 系用 2（与 LibreHardwareMonitor 0.9.6 的 `NvidiaGpu` 表一致）。
- 映射：`NvAPI_GPU_GetBusId` 得到 PCI bus（3090=4），NVML 的 `nvmlDeviceGetPciInfo_v3` 的 `bus` 字段同值；不要按枚举顺序硬匹配（NVAPI 在 MCDM 下只看得见 3090，CMP 不可见）。
- 参考：本机 RTX 3090（驱动 610.88）实测 mask=`1<<9` 返回 54°C；LHM 0.9.6 的 `LibreHardwareMonitorLib.dll` IL 里可查到该函数 ID 与槽位。

### 完全模式窗口宽度必须随卡数变化

- 现象：加第三张卡后精简模式有 3 行，但完全模式只看得到 2 张；`NVML · 3 卡` 状态正常。
- 原因：`apply_geometry()` 里完全模式宽度写死 `524`（= 2×250 + 2×12 padding），第三列被 pack 到窗口外；`winfo_ismapped()` 仍可能为 1，所以不能只靠 mapped 判断。
- 做法：`w = (FULL_CARD_W + FULL_CARD_PAD) * n`（每卡 262px），3 卡 = 786px；`test-geometry.py` 第 [6] 步断言 `body.winfo_reqwidth() <= win_w` 且最右卡片 `rootx+width <=` 窗口右沿。
- 坑：用户拖动过的窗口扩宽时会走 `ensure_on_screen()` 夹取；回归测试要把窗口放在靠右位置（如 `+3000+1500`），否则扩宽后左移出屏会被夹回，右下角断言失败。

### read_gpu 异常必须就地捕获，否则自愈逻辑永远不跑

- 现象：3 卡驱动切换期间 `monitor.log` 每秒一条 `OSError: exception: access violation reading 0x...`（`read_gpu` 里的 `nvmlDeviceGetTemperature`），持续 20+ 分钟；NVML 从不重建，直到手动退出。
- 原因：`_tick` 里 `g = read_gpu(i)` 直接抛异常会跳出整个 `_tick`，后面的 `fail_streak` / `degraded_streak` / `nvml_reinit(force=True)` 全部被跳过，形成「每秒崩溃但不自愈」的死循环。
- 做法：每张卡 `try/except`，失败按 `g={}` 处理并限频写日志；这样 `with_data` / `healthy` 统计照常累计，5s / 15s 后能触发强制重建。

### 悬浮窗要「普通窗口之上、任务栏之下」

- 需求：`-topmost` 会把窗口放进 topmost 带，默认排在 `Shell_TrayWnd` 之上；拖到任务栏上会盖住开始菜单 / 托盘 / 时间。
- 做法：先确保窗口在 topmost 带（`SetWindowPos(hwnd, HWND_TOPMOST, ..., SWP_NOSIZE|SWP_NOMOVE|SWP_NOACTIVATE|SWP_NOOWNERZORDER)`），然后**仅当 taskbar 自身也带 `WS_EX_TOPMOST` 时**才用 `SetWindowPos(hwnd, taskbar, ...)` 把它插到任务栏之后（z-order 下方）。taskbar 用 `FindWindowW("Shell_TrayWnd")`；多屏先用 `MonitorFromWindow` 匹配 `Shell_SecondaryTrayWnd`，没有再用主任务栏。taskbar 不是 topmost 时保持纯置顶，绝不为了“在任务栏下面”而掉出 topmost 带。
- **绝不允许被任务栏整个吞掉**（否则点不到、只能拖出来才能恢复）：插到任务栏之前先量 `covered_share(window_rect(hwnd), window_rect(tray))` 与 `visible_strip(...)`；盖过 `TASKBAR_COVER_LIMIT=0.6`，或者露在外面的边不够 `TASKBAR_KEEP_VISIBLE_PX=12`（根本瞄不准），就改成 `SetWindowPos(hwnd, HWND_TOPMOST, ...)` 爬到任务栏之上保持可点击，并记 `("above", tray)` 状态；拖出来后下一秒条件不成立，又自动回到下面。日志关键字：`keeping it above` / `below taskbar`。
- 自动隐藏的任务栏不用特殊处理：滑出时它的 rect 会真盖到窗口上 → 同一套判断自动把窗口抬上去；缩回去后自动回下面。
- 验证“会不会被吞”可以完全不点鼠标：用外部脚本 `win32gui.SetWindowPos(app, 0, x, tray.top+10, 0, 0, SWP_NOSIZE|SWP_NOZORDER)` 把窗口按进任务栏（程序的 `_tick` 用 `SWP_NOMOVE`，不会把它拖回来），等 3 秒后用 `GetTopWindow` + `GW_HWNDNEXT` 枚举 z-order，比 app 与 taskbar 的下标（app 下标更小 = 在任务栏上面），再挪到 `tray.top-8`（盖 71%、露 8px，仍应在上面）与 `tray.top-20`（盖 29%、露 20px，应回到下面）。
- 维护：点击 / `root.lift()` / `show()` 会把窗口抬回任务栏之上，所以要在 `<ButtonRelease-1>`、`<FocusIn>`、拖动 `_move`、`show()` 和每秒 `_tick` 里重新校正。校正前先沿 `GW_HWNDPREV` 向上找 taskbar：找到说明已经在下面，直接返回，避免无谓重排。
- 验证：用 `GetTopWindow` + `GetWindow(GW_HWNDNEXT)` 枚举 z-order，taskbar 为 topmost 时 app 的 index 应大于 taskbar（如 app=6、tray=5），且两者都带 `WS_EX_TOPMOST`；用 `SetWindowPos(tray, HWND_NOTOPMOST, ...)` 模拟任务栏失去 topmost（分辨率切换 / Explorer 重启后会出现），此时 app 必须仍是 `WS_EX_TOPMOST`、index 小于所有普通窗口，日志写 `keeping widget topmost`；恢复 taskbar topmost 后再自动变回在任务栏下面。
- 坑：`SetWindowPos` 的 `hWndInsertAfter` 必须是 topmost 的 taskbar；若传非 topmost 的 taskbar，目标窗口会被改成非 topmost —— 这正是“有时候不置顶”的根因：任务栏在分辨率切换 / Explorer 重启 / 全屏工具之后可能没有 `WS_EX_TOPMOST`，旧代码仍然按它插队，于是悬浮窗被降级到普通窗口层，下一秒 tick 又继续把它按在下面。
- 坑：不要在窗口映射前缓存 `GetAncestor(winfo_id(), GA_ROOT)` 的 HWND。Tk 映射后顶层 HWND 会变，缓存的旧句柄让后续 `SetWindowPos` 静默无效（日志还显示已设置成功）。每次现取；实测 `after(50)` 时拿到的 HWND 与最终可见窗口不同。

### 分层窗口截图会有残影，改用 PrintWindow

- 现象：`ImageGrab.grab(bbox=GetWindowRect(...))` 在精简→完全改尺寸后，截图左上角出现旧 header 的半透明残影（窗口本身内容正常，`PrintWindow` 干净）。
- 原因：Tk 窗口带 `-alpha 0.97` 且 `overrideredirect`，改尺寸/移动后 DWM 的分层表面在屏幕截图中会残留旧帧。
- 做法：`ctypes.WinDLL("user32").PrintWindow(hwnd, hdc, 2)` 把窗口画到内存 DC，再用 PIL 从 bitmap 取图；`win32gui` 没有导出 `PrintWindow`，必须用 ctypes。docs 截图用此法可避免残影。

### 三布局（完全 / 精简 / 极简）与 compact.flag 的内容语义

- 常量 `MODE_FULL/MODE_COMPACT/MODE_ULTRA` + `MODE_NEXT` 循环，标题栏只有一个按钮，标签永远写**下一个**布局的名字（`MODE_NAMES[MODE_NEXT[self.mode]]`）。
- `compact.flag` 的**内容**现在有意义：文件不存在 = 完全，空文件 = 精简，内容 `ultra` = 极简；因此旧的 flag 文件照旧可用（别去改写用户的内容做测试）。
- 极简模式**没有标题栏**：`—` 和布局按钮是 `ultra_row` 的子控件，随行一起销毁重建；换回其它布局时必须 `header.pack(before=self.body)`，否则标题栏会被 pack 到 body 下面。
- 拖动：`_bind_drag` 递归绑定行内控件，但**跳过 `tk.Button`**（点按钮不是抓手柄，鼠标抖一下就会把窗口推走）。`_press` 必须用 `x_root/y_root` 绝对坐标 —— 行内控件的 `e.x/e.y` 是控件本地坐标，拿它当窗口原点会让窗口跳变。
- 宽度：每张卡一个 `width=ULTRA_SEG_W` + `pack_propagate(False)` 的**定宽格子**（90px），于是某张卡的数字变长变短不会把别的卡的数字推位；`apply_geometry` 与 `_ultra_need()` 都是纯算术（`ULTRA_PAD + ULTRA_SEG_W*n + ULTRA_SEP_W*(n-1) + ULTRA_BTN_W`），不再需要“只增不减”的宽度补偿。（`pack_propagate(False)` 的 Frame 里 `winfo_reqwidth()` 只会报自己配置的宽度，量不到子控件 —— 想按内容算宽就得逐标签累加。）
- 定宽按最坏情况算刚好，格子内容超宽会被 Frame 裁掉：序号 `13+10`=23、`99G` 25、`999W` `34+8`=42 → 90。Tk Label 即使 `padx=0` 仍带约 4px 内部留白，算宽时别按纯字宽算。
- 显存读数单独一个粗体标签（`Consolas 9 bold`、`padx=0` 与前后标签齐平，否则默认 padx 会在数字两边各挤出约 1px 的缝）；Consolas 是等宽字体，粗体的 reqwidth 与常规相同，所以粗体不影响宽度估算。
- 分隔线用 `ULTRA_SEP_COLOR=#4a5262` 的 **2px** Frame（比边框 `LINE=#2a2f3a` 亮不少，否则 1px 的分隔线几乎看不见，两张卡看起来连成一片）。行高 `ULTRA_ROW_H=26` 必须够 `—` 按钮坐得下：Segoe UI 11 时按钮 reqheight=31，会被定高行裁掉，现在 `—` 用 Segoe UI 10（reqheight=26）刚好；窗口高 = `ULTRA_ROW_H + 2*pady` = 28。
- 实测：序号 `13+10`、`12G` 21、`250W` 34+8；定宽格子里数字**左对齐**，所以位数变化时数字的左边缘不动、只有单位 `G`/`W` 在跑。
- 验证“数字没有移位”的办法：同一窗口连拍两张 PrintWindow 截图，找出整列都等于分隔线色 `#4a5262`（现在每条分隔线占 2 列，如 `[99,100]`、`[195,196]`）或边框色 `#2a2f3a`（如 `[6,349]`）的列，两张的分隔线/边框列号应完全一致；再逐列比对差异像素，应只落在数字区域。

### 验证 UI 交互不必依赖真实鼠标（用 invoke + event_generate）

- 现象：`SetCursorPos` + `mouse_event` 的自动点击经常失效 —— 用户在动真鼠标时光标立刻被抢走，`SetCursorPos` 白做，拖动和点击全部落空。
- 做法：在进程内直接 `btn.invoke()` 触发命令；拖动用 `w.event_generate("<ButtonPress-1>", rootx=..., rooty=..., x=..., y=...)`，选项名是 `rootx/rooty`（写成 `x_root` 会报 `bad option`）。
- 好处：不依赖光标位置，断言精确（窗口位移 == 指针位移）；`cget("font")` 返回的是 Tcl 列表字符串（如 `Consolas 9 bold`），判断粗体用 `"bold" in ...` 而不是 `endswith("bold")`。
- 坑：`MODE_NAMES` 的值是中文，断言里别拿英文 `"full"/"compact"` 去比；`cget("text")` 的标签断言必须在**每次切换之后立刻**取，放在循环外面会全部跟最后一个模式比。

### 按像素验证配色：PrintWindow 首拍可能没画、颜色距离会误伤

- 现象：图里明明看得到第 3 张卡的曲线，按画布区域取像素却一个彩色都没有；在同一个 root 里切到极简模式后 `PrintWindow` 整窗拍到一条纯背景空白（连卡片色 `#1c1f26` 都没有）。
- 原因：Tk 懒绘制 —— `PrintWindow` 拍到的可能是还没刷新的画布，或刚换布局尚未重绘的表面；`winfo_*` 坐标是对的，是图没画上去。
- 做法：连拍两三次取最后一次（每拍之间 `update()` 一下）；换布局的验证另起一个 `tk.Tk()` 从头建窗口，不要指望同一个 root 切换后立刻拍干净。
- 坑：查“旧配色是否残留”要用**精确** hex 相等而不是距离阈值 —— 功耗琥珀 `#e8a23c` 与旧的第 3 卡橙 `#ffb020` 只差 39，容差 40 会把功耗线误报成残留。
- 反过来，“新颜色在不在”用容差 60 的模糊匹配没问题（曲线有抗锯齿）；但**小字号**（极简模式 9pt 序号）几乎没纯前景像素，要改成“取非背景像素、按最接近的候选色投票”（实测每格 ink≈20-35，颜色对时占比 ≥0.94）。
- docs 截图只能用**真实运行的实例**重截（曲线得有真数据，启动后等 1 分钟左右），外部脚本 `EnumWindows` 找标题 `GPU Monitor` + `PrintWindow`；合成数据的截图不要顶上去。

<!--
### 某某问题
- 现象：...
- 原因：...
- 解决办法：...
-->
