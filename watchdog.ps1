# GPU-Monitor watchdog: relaunch the exe if it disappears.
# Exits permanently when the user quits via the tray menu (flag file).
$ErrorActionPreference = 'SilentlyContinue'
$created = $false
$m = New-Object System.Threading.Mutex($true, 'GPUMonitorWatchdogMutex', [ref]$created)
if (-not $created) { exit }
$exe = Join-Path $PSScriptRoot 'GPU-Monitor.exe'
$flag = Join-Path $PSScriptRoot 'no-restart.flag'
while ($true) {
    Start-Sleep -Seconds 10
    if (Test-Path $flag) { break }
    if (-not (Get-Process GPU-Monitor -ErrorAction SilentlyContinue)) {
        Start-Process $exe
    }
}
