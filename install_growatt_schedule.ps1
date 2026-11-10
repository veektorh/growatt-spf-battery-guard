$ErrorActionPreference = "Stop"

# These tasks mirror the estate-current profile. They pass --profile so they
# no-op when another profile is active; multi-profile switching is installed by
# install_cloud_cron.sh on the VPS.

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
Remove-StaleGrowattTask -Name "Utility Check Afternoon"

Register-GrowattTask `
    -Name "Morning Health Report" `
    -ScheduleArgs @("/SC", "DAILY", "/ST", "06:10") `
    -Command "run-scheduled morning-health"

Register-GrowattTask `
    -Name "Utility Check Morning" `
    -ScheduleArgs @("/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", "06:30") `
    -Command "run-scheduled --profile estate-current morning-preserve"

Register-GrowattTask `
    -Name "SBU Before Morning Outage" `
    -ScheduleArgs @("/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", "07:55") `
    -Command "run-scheduled --profile estate-current morning-return-sbu"

Register-GrowattTask `
    -Name "SBU Watchdog Morning" `
    -ScheduleArgs @("/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", "08:01") `
    -Command "run-scheduled --profile estate-current morning-watchdog"

Register-GrowattTask `
    -Name "Utility Check Noon" `
    -ScheduleArgs @("/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", "12:30") `
    -Command "run-scheduled --profile estate-current afternoon-preserve-noon"

Register-GrowattTask `
    -Name "Utility Check Afternoon Early" `
    -ScheduleArgs @("/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", "13:30") `
    -Command "run-scheduled --profile estate-current afternoon-preserve-early"

Register-GrowattTask `
    -Name "Utility Check Afternoon Retry" `
    -ScheduleArgs @("/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", "14:00") `
    -Command "run-scheduled --profile estate-current afternoon-preserve-retry"

Register-GrowattTask `
    -Name "Utility Check Afternoon Final" `
    -ScheduleArgs @("/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", "14:30") `
    -Command "run-scheduled --profile estate-current afternoon-preserve"

Register-GrowattTask `
    -Name "SBU Before Afternoon Outage" `
    -ScheduleArgs @("/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", "15:25") `
    -Command "run-scheduled --profile estate-current afternoon-return-sbu"

Register-GrowattTask `
    -Name "SBU Watchdog Afternoon" `
    -ScheduleArgs @("/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", "15:31") `
    -Command "run-scheduled --profile estate-current afternoon-watchdog"

Register-GrowattTask `
    -Name "Daily Summary" `
    -ScheduleArgs @("/SC", "DAILY", "/ST", "21:00") `
    -Command "run-scheduled daily-summary"

Register-GrowattTask `
    -Name "Emergency Battery Alert" `
    -ScheduleArgs @("/SC", "MINUTE", "/MO", "30") `
    -Command "run-scheduled battery-alert"

Register-GrowattTask `
    -Name "Weekly Summary" `
    -ScheduleArgs @("/SC", "WEEKLY", "/D", "SUN", "/ST", "21:10") `
    -Command "run-scheduled weekly-summary"

Register-GrowattTask `
    -Name "Log Rotation" `
    -ScheduleArgs @("/SC", "DAILY", "/ST", "00:10") `
    -Command "run-scheduled rotate-logs"

Write-Host "Done."
