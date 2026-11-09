<#
  AirPods bridge: run hidden at logon (no admin needed).

    autostart.ps1 -Install     copy the app to %LOCALAPPDATA%\airpodlink\app and register the logon task
    autostart.ps1 -Update      re-copy the app (after code changes) and restart the task
    autostart.ps1 -Start | -Stop | -Status
    autostart.ps1 -Uninstall   remove the task and the copied app (keeps logs and pairing keys)

  The task runs %USERPROFILE%\airpod-venv\Scripts\pythonw.exe (no console window) with
  tools\airpods_bridge.py --service. Logs: %LOCALAPPDATA%\airpodlink\bridge.log
  The app is copied off the WSL share so logon doesn't depend on WSL being up.
#>
param([switch]$Install, [switch]$Update, [switch]$Uninstall, [switch]$Start, [switch]$Stop, [switch]$Status,
      [string]$Source = (Split-Path -Parent $PSScriptRoot),
      [string]$Transport, [string]$AirPods)
$ErrorActionPreference = 'Stop'
if (($Install -or $Update) -and (-not $Transport -or -not $AirPods)) { throw 'Pass -Transport usb:<path> and -AirPods <address>. See README.md.' }
$TaskName = 'AirPods bridge'
$Home_ = Join-Path $env:LOCALAPPDATA 'airpodlink'
$App = Join-Path $Home_ 'app'
$Pythonw = Join-Path $env:USERPROFILE 'airpod-venv\Scripts\pythonw.exe'

function Stop-Bridge {
  # Disable first: the task's "restart on failure" otherwise counts the kill below as a failure
  # and relaunches the (possibly old) copy a minute later (seen 2026-10-08).
  Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  $procs = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*airpods_bridge.py*--service*' -and $_.Name -like 'python*' })
  foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
  # Wait until they're really gone: a dying instance still holds the dongle (USBErrorAccess)
  # and the Ctrl+Alt+M hotkey for a moment.
  foreach ($p in $procs) { Wait-Process -Id $p.ProcessId -Timeout 10 -ErrorAction SilentlyContinue }
  Start-Sleep -Milliseconds 500
}

function Enable-Bridge { Enable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null }

function Copy-App {
  New-Item -ItemType Directory -Force -Path $App | Out-Null
  foreach ($d in 'airpodlink', 'tools', 'config', 'vendor') {
    $dst = Join-Path $App $d
    if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
    Copy-Item -Recurse -Path (Join-Path $Source $d) -Destination $dst
    Get-ChildItem -Recurse -Directory -Filter '__pycache__' $dst | Remove-Item -Recurse -Force
  }
  Write-Host "app copied: $Source -> $App"
}

if ($Install -or $Update) {
  if (-not (Test-Path $Pythonw)) { throw "pythonw not found: $Pythonw" }
  Stop-Bridge
  Copy-App
  $script = Join-Path $App 'tools\airpods_bridge.py'
  $action = New-ScheduledTaskAction -Execute $Pythonw -Argument "`"$script`" $Transport $AirPods --service" -WorkingDirectory $App
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
              -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
  $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'AirPods A2DP + hi-res mic bridge (Bumble on the WinUSB dongle). See README.' -Force | Out-Null
  Enable-Bridge
  Start-ScheduledTask -TaskName $TaskName
  Write-Host "task '$TaskName' registered and started (runs at every logon)"
}
elseif ($Uninstall) {
  Stop-Bridge
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  if (Test-Path $App) { Remove-Item -Recurse -Force $App }
  Write-Host "task removed and $App deleted (logs in $Home_ kept)"
}
elseif ($Start) { Enable-Bridge; Start-ScheduledTask -TaskName $TaskName; Write-Host 'started' }
elseif ($Stop) { Stop-Bridge; Write-Host 'stopped (task disabled until -Start / -Update; it still runs at next logon only after -Start)' }
else {
  $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if (-not $t) { Write-Host "task '$TaskName' not installed"; return }
  $info = Get-ScheduledTaskInfo -TaskName $TaskName
  $procs = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*airpods_bridge.py*--service*' })
  Write-Host "task: $($t.State), last run $($info.LastRunTime), last result 0x$('{0:X}' -f $info.LastTaskResult), bridge processes: $($procs.Count)"
  $log = Join-Path $Home_ 'bridge.log'
  if (Test-Path $log) { Write-Host '--- last log lines ---'; Get-Content $log -Tail 8 }
}
