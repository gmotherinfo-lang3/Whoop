# Register whoop-bridge as a Scheduled Task that starts at logon and restarts
# if it stops. Run in an elevated PowerShell from the repo root:
#   .\windows\install-task.ps1
# Remove later with:  Unregister-ScheduledTask -TaskName "WhoopBridge"

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$exe  = "$root\.venv\Scripts\whoop-bridge.exe"
$name = "WhoopBridge"

if (-not (Test-Path $exe)) { throw "$exe not found. Run .\windows\setup.ps1 first." }
if (-not (Test-Path "$root\config.toml")) { throw "config.toml not found. Run setup.ps1 and edit it." }

$action = New-ScheduledTaskAction -Execute $exe -Argument "run -c `"$root\config.toml`"" -WorkingDirectory $root

# At logon, plus a retry loop so a laptop that sleeps/wakes recovers on its own.
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = "PT30S"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 999 `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Registered scheduled task '$name'." -ForegroundColor Green
Write-Host "Start it now with:  Start-ScheduledTask -TaskName $name"
Write-Host "Watch the log with: Get-Content whoop-bridge.log -Wait"
