# Unattended morning run for the PromptProofreading sales agent.
#
# Runs `pp-agent daily` (sync Gmail -> follow-ups -> audit -> draft) and appends
# the output to data\logs\daily_<date>.log. It never sends email; drafts wait in
# the review queue (http://127.0.0.1:8765/queue) for you.
#
# To schedule it for 7:30 every weekday, run this ONCE in PowerShell yourself:
#   $a = New-ScheduledTaskAction -Execute "powershell.exe" `
#        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\run_daily.ps1`""
#   $t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 7:30am
#   Register-ScheduledTask -TaskName "PP Sales Agent daily" -Action $a -Trigger $t
# The computer must be on and you must be logged in (the Claude CLI login is per-user).

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:PYTHONUTF8 = "1"

$logDir = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ("daily_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

"==== $(Get-Date -Format s) ====" | Out-File -FilePath $log -Append -Encoding utf8
& $python -m app.cli daily *>&1 | Out-File -FilePath $log -Append -Encoding utf8
"exit code: $LASTEXITCODE" | Out-File -FilePath $log -Append -Encoding utf8
