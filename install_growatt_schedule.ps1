$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Register-GrowattTask {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Name,

        [Parameter(Mandatory = $true)]
        [string[]] $ScheduleArgs,

        [Parameter(Mandatory = $true)]
        [string] $Command
    )

    $taskName = "Growatt $Name"
    $taskRun = "cmd /c cd /d `"$Root`" && python growatt_power_guard.py $Command"
    $args = @("/Create", "/F", "/TN", $taskName) + $ScheduleArgs + @("/TR", $taskRun)

    Write-Host "Creating scheduled task: $taskName"
    & schtasks @args
}

function Remove-StaleGrowattTask {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Name
    )

    $taskName = "Growatt $Name"
    & schtasks /Query /TN $taskName *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Removing stale scheduled task: $taskName"
        & schtasks /Delete /F /TN $taskName
    }
}

Remove-StaleGrowattTask -Name "Utility Check Midday"

Register-GrowattTask `
    -Name "Utility Check Morning" `
    -ScheduleArgs @("/SC", "DAILY", "/ST", "06:30") `
    -Command "preserve-battery"

Register-GrowattTask `
    -Name "SBU Before Morning Outage" `
    -ScheduleArgs @("/SC", "DAILY", "/ST", "07:55") `
    -Command "return-sbu"

Register-GrowattTask `
    -Name "SBU Watchdog Morning" `
    -ScheduleArgs @("/SC", "DAILY", "/ST", "08:01") `
    -Command "watchdog-sbu"

Register-GrowattTask `
    -Name "Utility Check Afternoon" `
    -ScheduleArgs @("/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", "14:30") `
    -Command "preserve-battery"

Register-GrowattTask `
    -Name "SBU Before Afternoon Outage" `
    -ScheduleArgs @("/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", "15:25") `
    -Command "return-sbu"

Register-GrowattTask `
    -Name "SBU Watchdog Afternoon" `
    -ScheduleArgs @("/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", "15:31") `
    -Command "watchdog-sbu"

Register-GrowattTask `
    -Name "Daily Summary" `
    -ScheduleArgs @("/SC", "DAILY", "/ST", "21:00") `
    -Command "daily-summary"

Register-GrowattTask `
    -Name "Log Rotation" `
    -ScheduleArgs @("/SC", "DAILY", "/ST", "00:10") `
    -Command "rotate-logs"

Write-Host "Done."
