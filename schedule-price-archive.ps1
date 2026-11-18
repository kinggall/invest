param([Parameter(Mandatory=$true)][string]$DominoHome)
$ErrorActionPreference = 'Stop'
if ((Get-TimeZone).Id -ne 'Korea Standard Time') { throw 'Set the Windows timezone to Korea Standard Time for ETF archive scheduling.' }
$homePath = (Resolve-Path -LiteralPath $DominoHome).Path
$runner = Join-Path $PSScriptRoot 'price_archive.py'
$state = Join-Path $PSScriptRoot 'runtime\orders.sqlite3'
$python = [string](& python -c 'import sys; print(sys.executable)')
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python.Trim())) { throw 'Python executable not found.' }
$python = $python.Trim()
$name = 'InvestETFMinuteArchive'
$description = 'Invest actual ETF minute archive v1 (read-only KIS GET)'
$arguments = '"{0}" --domino-home "{1}" --state "{2}"' -f $runner, $homePath, $state
$existing = @(Get-ScheduledTask -TaskPath '\' | Where-Object { $_.TaskName -eq $name })
if ($existing.Count -and ($existing[0].Description -ne $description -or @($existing[0].Actions).Count -ne 1 -or $existing[0].Actions[0].Arguments -ne $arguments)) {
    throw 'An unrelated task already uses InvestETFMinuteArchive. It was not changed.'
}
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $PSScriptRoot
# Explicit daily triggers avoid the null Repetition object on Windows PS 5.1.
# 26 intraday + 3 closing + 1 logon triggers stay below Task Scheduler's limit.
$triggers = @(for ($slot = 0; $slot -lt 26; $slot++) {
    $at = (Get-Date).Date.AddHours(9).AddMinutes(5 + 15 * $slot)
    New-ScheduledTaskTrigger -Daily -At $at
})
$triggers += @((New-ScheduledTaskTrigger -Daily -At '15:35'), (New-ScheduledTaskTrigger -Daily -At '15:50'), (New-ScheduledTaskTrigger -Daily -At '16:10'), (New-ScheduledTaskTrigger -AtLogOn -User $userId))
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName $name -TaskPath '\' -Description $description -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $name -TaskPath '\'
Write-Host 'ETF minute archive scheduled: every 15 min intraday, full checks at 15:35/15:50/16:10 and logon.'
