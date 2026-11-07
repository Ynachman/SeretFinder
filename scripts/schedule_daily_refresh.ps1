<#
  Registers a Windows scheduled task that runs the SeretFinder refresh daily.

  Usage (from the project root, in PowerShell):
      .\scripts\schedule_daily_refresh.ps1              # daily at 04:00
      .\scripts\schedule_daily_refresh.ps1 -At 06:30
      .\scripts\schedule_daily_refresh.ps1 -Remove      # delete the task

  Runs as the current user (no admin needed) and only while the PC is on;
  StartWhenAvailable makes it run at next login if it missed its time.
  Output is appended to logs\refresh.log.
#>
param(
    [string]$At = "04:00",
    [switch]$Remove
)

$TaskName = "SeretFinder daily refresh"
if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed '$TaskName'."
    return
}

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Missing $python - create the venv first (see README)." }
New-Item -ItemType Directory -Force (Join-Path $root "logs") | Out-Null

$cmd = "& '$python' -m scraper.refresh *>> 'logs\refresh.log'"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -Command `"$cmd`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Scheduled '$TaskName' daily at $At. Test it now with:  Start-ScheduledTask -TaskName '$TaskName'"
