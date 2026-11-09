@echo off
rem AirPods two-way bridge in a console window, for debugging.
rem Usage: airpods_bridge.cmd usb:<dongle path> <AirPods Bluetooth address> [options]
rem If the bridge runs at logon, stop it first (setup\autostart.ps1 -Stop): only one process can own the dongle.
title AirPods bridge
if "%~2"=="" (
  echo Usage: airpods_bridge.cmd usb:^<dongle path^> ^<AirPods Bluetooth address^> [options]
  pause
  exit /b 1
)
"%USERPROFILE%\airpod-venv\Scripts\python.exe" "%~dp0tools\airpods_bridge.py" %*
pause
