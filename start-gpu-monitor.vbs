' GPU mini monitor launcher (hidden, no console window)
' Server: http://127.0.0.1:8765
Set sh = CreateObject("WScript.Shell")
sh.Run """C:\Users\hong\.workbuddy\binaries\python\envs\default\Scripts\pythonw.exe"" ""D:\Portable Program\gpu-monitor\gpu-monitor.py""", 0, False
