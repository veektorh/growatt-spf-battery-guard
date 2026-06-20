# Growatt SPF Battery Preservation Guard

This automates the rainy-season routine for a Growatt SPF 6000 ES on ShinePhone:

- `preserve-battery` runs before known outage windows.
- It reads battery SOC from Growatt/ShinePhone.
- If SOC is below `LOW_BATTERY_SOC`, it switches to Utility while estate power is available.
- `return-sbu` runs a few minutes before each outage and switches back to SBU.
- `health-check --notify` reports VPS/cron/Growatt readiness before the day starts.
- `battery-alert` sends a throttled Discord warning if SOC drops below `EMERGENCY_SOC`.

The script starts in `DRY_RUN=true` mode. In dry-run it logs in and prepares the command, but does not change the inverter.

## Setup

```powershell
cd C:\path\to\automation
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Fill in:

```text
GROWATT_USERNAME=...
GROWATT_PASSWORD=...
LOW_BATTERY_SOC=50
EMERGENCY_SOC=30
DRY_RUN=true
```

Then test reading data:

```powershell
python .\growatt_power_guard.py status
python .\growatt_power_guard.py probe
```

`probe` writes a redacted JSON file under `logs/`. Use this once so we can confirm the exact SPF setting command for your account/firmware before turning `DRY_RUN=false`.

For this inverter, the probe selected:

```text
GROWATT_PLANT_ID=your_plant_id
GROWATT_DEVICE_SN=your_device_sn
```

You can add those to `.env` so future runs do not rely on auto-selecting the first plant/device.

## Mode switching

Your SPF 6000 ES PLUS uses Growatt's SPF5000 storage setting command:

```text
GROWATT_MODE_DRIVER=spf5000
```

The script sends `storage_spf5000_ac_output_source` through Growatt's `storageSPF5000Set` action on `tcpSet.do`:

```text
0 = SBU priority
2 = Utility first
```

Keep `DRY_RUN=true` for the first `preserve-battery` and `return-sbu` manual test. After the dry-run output looks right, set `DRY_RUN=false`.

## Weather-Aware Threshold

Weather support is optional and uses [Open-Meteo](https://open-meteo.com/en/docs), which does not require an API key. The forecast checks hourly precipitation and cloud cover for the next few hours.

This setup keeps your rainy-season threshold at `50%`, then reduces utility use on better solar days:

```text
rainy/cloudy -> 50%
normal       -> 45%
sunny        -> 40%
```

Enable it in `.env`:

```text
WEATHER_ENABLED=true
WEATHER_LAT=your_latitude
WEATHER_LON=your_longitude
WEATHER_TIMEZONE=Africa/Lagos
LOW_BATTERY_SOC=50
LOW_BATTERY_SOC_NORMAL=45
LOW_BATTERY_SOC_SUNNY=40
```

Test the current dynamic threshold:

```bash
python growatt_power_guard.py weather-threshold
```

## Discord Notifications

Discord notifications are optional. Create a webhook in your Discord server:

```text
Server Settings -> Integrations -> Webhooks -> New Webhook -> Copy Webhook URL
```

Then add it to `.env`:

```text
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_NOTIFY_SUCCESS=true
DISCORD_NOTIFY_SKIP=false
DISCORD_NOTIFY_FAILURE=true
GROWATT_CLOUD_FAILURE_ALERT_THRESHOLD=3
```

Test it:

```bash
python growatt_power_guard.py test-discord
```

If the test returns `HTTP 403: Forbidden`, regenerate the webhook in Discord and paste the fresh full URL into `.env`. Make sure it starts with:

```text
https://discord.com/api/webhooks/
```

Notifications are sent when:

```text
preserve-battery switches to Utility first
return-sbu switches to SBU priority
watchdog-sbu repairs a missed SBU switch
daily-summary posts the end-of-day summary
health-check --notify posts readiness diagnostics
battery-alert detects or clears an emergency SOC episode
weekly-summary posts the weekly performance report
Growatt cloud failures alert after repeated consecutive failures
other command failures alert immediately, if DISCORD_NOTIFY_FAILURE=true
checks are skipped, only if DISCORD_NOTIFY_SKIP=true
```

## Current Light Schedule

Estate power is unavailable during these windows:

```text
Weekdays: 08:00-10:30 and 15:30-18:00
Weekends: 08:00-10:30
```

The automation should therefore:

```text
06:10 daily       post Discord health report
06:30 daily       preserve-battery if SOC is below 50%
07:55 daily       return to SBU before the 08:00 outage
08:01 daily       verify SBU and retry once if needed
14:30 weekdays    preserve-battery if SOC is below 50%
15:25 weekdays    return to SBU before the 15:30 outage
15:31 weekdays    verify SBU and retry once if needed
21:00 daily       post Discord daily summary
*/30 always       alert once if battery SOC drops below 30%
21:10 Sundays     post weekly performance summary
00:10 daily       rotate old generated logs/probes
```

The cloud cron installer reads these jobs from [schedule.json](schedule.json). Cron calls `run-scheduled <job-id>`, which applies date overrides before running the job. To change outage times, edit `schedule.json`, validate it, then reinstall cron:

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py validate-schedule
./install_cloud_cron.sh
```

## Run manually

```powershell
python .\growatt_power_guard.py preserve-battery
python .\growatt_power_guard.py return-sbu
python .\growatt_power_guard.py watchdog-sbu
python .\growatt_power_guard.py daily-summary
python .\growatt_power_guard.py weekly-summary
python .\growatt_power_guard.py rotate-logs
python .\growatt_power_guard.py weather-threshold
python .\growatt_power_guard.py validate-schedule
python .\growatt_power_guard.py health-check
python .\growatt_power_guard.py health-check --notify
python .\growatt_power_guard.py battery-alert
python .\growatt_power_guard.py dashboard
python .\growatt_power_guard.py dashboard-refresh --once
python .\growatt_power_guard.py dashboard-stale-alert
python .\growatt_power_guard.py serve-dashboard
python .\growatt_power_guard.py pause --hours 6 --reason "maintenance"
python .\growatt_power_guard.py pause-status
python .\growatt_power_guard.py resume
```

## Schedule on Windows

These commands create the current estate schedule:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_growatt_schedule.ps1
```

Or create them manually:

```powershell
schtasks /Create /F /TN "Growatt Morning Health Report" /SC DAILY /ST 06:10 /TR "cmd /c cd /d C:\path\to\automation && python growatt_power_guard.py run-scheduled morning-health"
schtasks /Create /F /TN "Growatt Utility Check Morning" /SC DAILY /ST 06:30 /TR "cmd /c cd /d C:\path\to\automation && python growatt_power_guard.py run-scheduled morning-preserve"
schtasks /Create /F /TN "Growatt SBU Before Morning Outage" /SC DAILY /ST 07:55 /TR "cmd /c cd /d C:\path\to\automation && python growatt_power_guard.py run-scheduled morning-return-sbu"
schtasks /Create /F /TN "Growatt SBU Watchdog Morning" /SC DAILY /ST 08:01 /TR "cmd /c cd /d C:\path\to\automation && python growatt_power_guard.py run-scheduled morning-watchdog"
schtasks /Create /F /TN "Growatt Utility Check Afternoon" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 14:30 /TR "cmd /c cd /d C:\path\to\automation && python growatt_power_guard.py run-scheduled afternoon-preserve"
schtasks /Create /F /TN "Growatt SBU Before Afternoon Outage" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 15:25 /TR "cmd /c cd /d C:\path\to\automation && python growatt_power_guard.py run-scheduled afternoon-return-sbu"
schtasks /Create /F /TN "Growatt SBU Watchdog Afternoon" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 15:31 /TR "cmd /c cd /d C:\path\to\automation && python growatt_power_guard.py run-scheduled afternoon-watchdog"
schtasks /Create /F /TN "Growatt Daily Summary" /SC DAILY /ST 21:00 /TR "cmd /c cd /d C:\path\to\automation && python growatt_power_guard.py run-scheduled daily-summary"
schtasks /Create /F /TN "Growatt Emergency Battery Alert" /SC MINUTE /MO 30 /TR "cmd /c cd /d C:\path\to\automation && python growatt_power_guard.py run-scheduled battery-alert"
schtasks /Create /F /TN "Growatt Weekly Summary" /SC WEEKLY /D SUN /ST 21:10 /TR "cmd /c cd /d C:\path\to\automation && python growatt_power_guard.py run-scheduled weekly-summary"
schtasks /Create /F /TN "Growatt Log Rotation" /SC DAILY /ST 00:10 /TR "cmd /c cd /d C:\path\to\automation && python growatt_power_guard.py run-scheduled rotate-logs"
```

Logs are written to:

```text
C:\path\to\automation\logs\growatt_power_guard.log
```

## Run On A Cloud VPS

Use this if you do not want your laptop to stay on. The VPS only needs internet access; it does not need to be on your home WiFi because the script talks to Growatt/ShinePhone cloud.

On a fresh Ubuntu VPS:

```bash
sudo timedatectl set-timezone Africa/Lagos
sudo apt update
sudo apt install -y python3 python3-venv git cron
sudo systemctl enable --now cron
```

Copy this project folder to the VPS, then run:

```bash
cd ~/automation
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
nano .env
```

Set these values in `.env`:

```text
GROWATT_USERNAME=...
GROWATT_PASSWORD=...
GROWATT_PLANT_ID=your_plant_id
GROWATT_DEVICE_SN=your_device_sn
LOW_BATTERY_SOC=50
EMERGENCY_SOC=30
EMERGENCY_SOC_RECOVERY=35
GROWATT_CLOUD_FAILURE_ALERT_THRESHOLD=3
DASHBOARD_STALE_MINUTES=30
DRY_RUN=true
GROWATT_MODE_DRIVER=spf5000
```

Test it:

```bash
.venv/bin/python growatt_power_guard.py status
.venv/bin/python growatt_power_guard.py test-discord
.venv/bin/python growatt_power_guard.py preserve-battery
.venv/bin/python growatt_power_guard.py return-sbu
.venv/bin/python growatt_power_guard.py watchdog-sbu
.venv/bin/python growatt_power_guard.py daily-summary
.venv/bin/python growatt_power_guard.py weekly-summary
.venv/bin/python growatt_power_guard.py rotate-logs
.venv/bin/python growatt_power_guard.py weather-threshold
.venv/bin/python growatt_power_guard.py validate-schedule
.venv/bin/python growatt_power_guard.py health-check
.venv/bin/python growatt_power_guard.py health-check --notify
.venv/bin/python growatt_power_guard.py battery-alert
.venv/bin/python growatt_power_guard.py dashboard
.venv/bin/python growatt_power_guard.py dashboard-refresh --once
```

## Pause Or Resume Automation

Pause only affects mode-changing commands: `preserve-battery`, `return-sbu`, and `watchdog-sbu`. Read-only commands such as `status`, `daily-summary`, `weekly-summary`, `weather-threshold`, `health-check`, `battery-alert`, and `dashboard` still run.

Mode-changing commands also use a local lock under `state/` so overlapping cron/manual runs do not issue conflicting Growatt mode commands. A stale lock clears automatically after 45 minutes.

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py pause --hours 6 --reason "inverter maintenance"
.venv/bin/python growatt_power_guard.py pause-status
.venv/bin/python growatt_power_guard.py resume
```

Pause state is stored locally under `state/`, which should not be committed.

After the dry-run output is correct, set `DRY_RUN=false`, then install the cloud cron schedule:

```bash
.venv/bin/python growatt_power_guard.py validate-schedule
./install_cloud_cron.sh
```

Verify the scheduled jobs:

```bash
crontab -l | grep growatt-power-guard
```

Cron logs go to:

```text
~/automation/logs/cron.log
~/automation/logs/growatt_power_guard.log
~/automation/logs/mode_decisions.csv
```

## Operations

Check current inverter state:

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py status
```

Watch recent automation logs:

```bash
tail -n 120 ~/automation/logs/growatt_power_guard.log
tail -n 120 ~/automation/logs/cron.log
tail -n 40 ~/automation/logs/mode_decisions.csv
```

Post a manual Discord daily summary:

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py daily-summary
```

Run a read-only health check:

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py health-check
.venv/bin/python growatt_power_guard.py health-check --notify
```

Run the emergency battery alert check manually:

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py battery-alert
```

Post a weekly performance summary manually:

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py weekly-summary
```

Generate the dashboard once:

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py dashboard
```

Install the safe dashboard services:

```bash
cd ~/automation
./install_dashboard_service.sh
```

This installs:

```text
growatt-dashboard-refresh.service  refreshes dashboard.html every 10 minutes
growatt-dashboard-server.service   serves the static file on 127.0.0.1:8080
growatt-dashboard-stale-alert.timer checks dashboard freshness every 10 minutes
```

Browser refreshes do not call Growatt. Only the refresh service calls Growatt, and only on the configured interval.
The dashboard includes a health badge that turns stale when `dashboard.html` is older than `DASHBOARD_STALE_MINUTES`.

To use a 30-minute refresh interval instead:

```bash
cd ~/automation
DASHBOARD_REFRESH_MINUTES=30 ./install_dashboard_service.sh
```

View the dashboard from your laptop through an SSH tunnel:

```bash
ssh -L 8080:localhost:8080 ubuntu@YOUR_VPS_IP
```

Then open:

```text
http://localhost:8080/dashboard.html
```

Check service status:

```bash
sudo systemctl status growatt-dashboard-refresh.service
sudo systemctl status growatt-dashboard-server.service
sudo systemctl status growatt-dashboard-stale-alert.timer
```

Stale dashboard alerts use Discord when `DISCORD_WEBHOOK_URL` is configured and `DISCORD_NOTIFY_FAILURE=true`:

```text
DASHBOARD_STALE_MINUTES=30
```

Manual freshness check:

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py dashboard-stale-alert
```

## Expose Dashboard On A Domain

Recommended public URL: a subdomain such as `dashboard.example.com` rather than a root domain, so it does not collide with any main website.

1. Create a DNS record:

```text
Type: A
Name: dashboard
Value: YOUR_VPS_PUBLIC_IP
```

2. Make sure ports `80` and `443` are open in your VPS firewall/security group.

3. Install the local dashboard services first:

```bash
cd ~/automation
./install_dashboard_service.sh
```

4. Install the HTTPS reverse proxy with basic auth:

```bash
cd ~/automation
DASHBOARD_DOMAIN=dashboard.example.com DASHBOARD_EMAIL=you@example.com ./install_dashboard_proxy.sh
```

It will prompt for a dashboard password. The public dashboard URL will be:

```text
https://dashboard.example.com/dashboard.html
```

The Python dashboard server still listens only on `127.0.0.1:8080`; Nginx is what exposes HTTPS publicly.

To expose the dashboard without basic auth:

```bash
cd ~/automation
DASHBOARD_AUTH_ENABLED=false DASHBOARD_DOMAIN=dashboard.example.com DASHBOARD_EMAIL=you@example.com ./install_dashboard_proxy.sh
```

## Growatt Cloud Flakiness Alerts

Transient Growatt/ShinePhone cloud failures are tracked as a streak so Discord does not alert on every one-off blip. The default is:

```text
GROWATT_CLOUD_FAILURE_ALERT_THRESHOLD=3
```

After 3 consecutive Growatt cloud login/status failures, Discord gets one alert. When a later Growatt read succeeds, the streak is cleared and Discord gets a recovery message.

## Schedule Overrides

Use date overrides for temporary estate schedule changes without editing `schedule.json`.

```bash
cd ~/automation
cp schedule_overrides.example.json schedule_overrides.json
nano schedule_overrides.json
.venv/bin/python growatt_power_guard.py validate-schedule
./install_cloud_cron.sh
```

Example: skip the afternoon outage automation on a specific date:

```json
{
  "dates": {
    "2026-06-26": {
      "note": "No afternoon outage today",
      "skip": [
        "afternoon-preserve",
        "afternoon-return-sbu",
        "afternoon-watchdog"
      ]
    }
  }
}
```

The local `schedule_overrides.json` file is ignored by Git so VPS-specific calendar changes stay private.

Change the battery preservation threshold:

```bash
nano ~/automation/.env
```

Then edit:

```text
LOW_BATTERY_SOC=50
EMERGENCY_SOC=30
EMERGENCY_SOC_RECOVERY=35
```

Update the VPS from GitHub:

```bash
cd ~/automation
./update_server.sh
```

Use `./update_server.sh --no-notify` if you want the health check printed only in the terminal.

Pause the cloud schedule:

```bash
crontab -l | grep -v growatt-power-guard | crontab -
```

Reinstall the current schedule:

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py validate-schedule
./install_cloud_cron.sh
```

## Public Repo Safety

This project can be public, but keep real secrets and device identifiers out of GitHub.

Safe to publish:

```text
growatt_power_guard.py
requirements.txt
README.md
RUNBOOK.md
.env.example
schedule.json
install_cloud_cron.sh
install_growatt_schedule.ps1
update_server.sh
install_dashboard_service.sh
install_dashboard_proxy.sh
schedule_overrides.example.json
tests/
.gitignore
```

Do not publish:

```text
.env
logs/
state/
growatt-probe-*.json
schedule_overrides.json
dashboard.html
```

Before pushing, check for private values:

```bash
grep -R "your_real_username\|your_real_device_serial\|your_real_plant_id" .
```
