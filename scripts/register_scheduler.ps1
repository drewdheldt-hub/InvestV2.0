# Register the one Task Scheduler entry investTree 2 needs: start the scheduler at logon and keep it running.
# Every job lives in config/schedule.yaml; this entry only makes sure the scheduler process is up.
#
#   powershell -ExecutionPolicy Bypass -File scripts\register_scheduler.ps1
#
# Run it once, after `uv sync`. Remove with: Unregister-ScheduledTask -TaskName "investTree2 scheduler"
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\pythonw.exe"      # pythonw: no console window
if (-not (Test-Path $python)) { throw "no project environment at $python; run 'uv sync' first" }

$action = New-ScheduledTaskAction -Execute $python -Argument "-m investtree scheduler run" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName "investTree2 scheduler" -Action $action -Trigger $trigger -Settings $settings `
    -Description "investTree 2: runs config/schedule.yaml. Status: investtree status" | Out-Null
"registered 'investTree2 scheduler' (starts at logon; restarts up to 3 times if it stops)"
