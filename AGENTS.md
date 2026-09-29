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
- 坑：本机 `[System.Windows.Forms.Screen]::PrimaryBounds` 返回 null（Bitmap 构造随之失败），改用 `[System.Windows.Forms.SystemInformation]::VirtualScreen`（物理分辨率 3840x2160）。
- 悬浮窗默认位置在右下角：`x = 屏宽 - w - 16`，`y = 屏高 - h - 86`；精简模式 w=440、h=32+30*卡数+6；完全模式 524x492。
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
- 窗体尺寸可反推模式与卡数：精简 440 x (32+33*n+6)，完全 524x492。

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
- 标题栏上的「精简/完全」按钮大致在 `rect.r - 40, rect.t + 16`（`—` 最靠右，它左边 2px 就是这个按钮），两种模式下位置一致。
- 验证方法：前后各用 `win32gui.EnumWindows` 找标题 `GPU Monitor` 的窗口，比较 `GetWindowRect` 的尺寸与右下角，再 `ImageGrab` 裁剪存图。

### 默认落位要贴边：用 SPI_GETWORKAREA，不要硬编码边距

- 旧做法 `x = sw - w - 16, y = sh - h - 86` 把任务栏当成 48px，结果上边缘离任务栏还空 38px、右边缘离屏幕空 16px，换机器 / 换任务栏高度就错位。
- 做法：`user32.SystemParametersInfoW(SPI_GETWORKAREA=0x0030, 0, byref(RECT), 0)` 拿**实时**工作区（已扣除任务栏与托盘），默认锚点就取它的右下角：`x = wa.right - w, y = wa.bottom - h`。任务栏变高、改成自动隐藏或停靠到其它边，下一 tick 就会重新贴边（前提：窗口没被用户拖动过）。
- 坑：`_check_display` 的比较元组必须把 `work_area()` 一并放进去（`(desktop_metrics(), work_area())`），否则任务栏变化不会触发重新落位；而 `ensure_on_screen` 的夹取仍按整个虚拟桌面算 —— 被夹回的窗口可能压在任务栏上，但悬浮窗是 `-topmost`，会盖在任务栏之上而不是被藏掉。

<!--
### 某某问题
- 现象：...
- 原因：...
- 解决办法：...
-->
