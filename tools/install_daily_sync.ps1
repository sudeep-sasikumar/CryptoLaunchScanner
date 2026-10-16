# Registers the daily Windows task that copies the VPS's research data to this PC and writes the
# traits report (tools\sync_research.py). Runs every day at 09:30, or as soon as the PC is on if it
# was off; under your own account, only while you are logged in; stores no password.
#
#   powershell -ExecutionPolicy Bypass -File tools\install_daily_sync.ps1
#   remove:  Unregister-ScheduledTask -TaskName "Launch Radar research sync" -Confirm:$false
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $py)) { throw "Python environment not found at $py - set up .venv first (README)." }
$action = New-ScheduledTaskAction -Execute $py -Argument ('"' + (Join-Path $root "tools\sync_research.py") + '"') -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At 09:30
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "Launch Radar research sync" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Copies Launch Radar's research data from the VPS and writes the traits report" -Force | Out-Null
Write-Host "Registered 'Launch Radar research sync' (daily 09:30). Run it now with:"
Write-Host '  Start-ScheduledTask -TaskName "Launch Radar research sync"'
