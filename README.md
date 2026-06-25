# Growatt SPF Battery Preservation Guard

[![CI](https://github.com/veektorh/growatt-spf-battery-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/veektorh/growatt-spf-battery-guard/actions/workflows/ci.yml)

This automates battery-preservation mode switching for a Growatt SPF 6000 ES on ShinePhone. It runs year-round with season-aware thresholds:

- `preserve-battery` runs before known outage windows and switches to Utility when SOC is below threshold.
- `return-sbu` runs a few minutes before each outage and switches back to SBU priority.
- `health-check --notify` reports VPS/cron/Growatt readiness before the day starts.
- `battery-alert` sends a throttled Discord warning if SOC drops below `EMERGENCY_SOC`.
- `runtime-alert` sends a Discord warning when estimated battery runtime drops below a configured threshold.
- `watchdog-sbu` repairs a missed SBU switch; with `BATTERY_CHARGE_TARGET_SOC` set it waits until charging is complete before repairing.
- `auto-topup-check` fires a timed Utility top-up at night when the battery won't survive until sunrise; `topup-complete-check` resumes automation once it finishes and reports the implied AC charge rate so you can tune `BATTERY_CHARGE_RATE_W` over time.
- Weather-aware thresholds reduce utility use on good solar days.
- Season profiles automatically lower thresholds in the dry season (November–March) when solar is stronger.

The script starts in `DRY_RUN=true` mode. In dry-run it logs in and prepares the command, but does not change the inverter.

## Developer Docs

- [AGENTS.md](AGENTS.md) gives coding agents the safety rules, module map, and verification checklist.
- [ARCHITECTURE.md](ARCHITECTURE.md) explains the command flow and module boundaries.
- [ROADMAP.md](ROADMAP.md) lists improvement and enhancement ideas for future work.
- [RUNBOOK.md](RUNBOOK.md) covers VPS operations and recovery steps.
- [PUBLIC_RELEASE_CHECKLIST.md](PUBLIC_RELEASE_CHECKLIST.md) lists public-repo hygiene checks.

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

### Season Profiles

Enable season profiles to automatically lower thresholds during the dry season (November–March for Lagos), when solar irradiance is higher and the battery tops up faster:

```text
SEASON_PROFILES_ENABLED=true
```

When enabled, the dry-season thresholds are:

```text
rainy/cloudy -> 45%
normal       -> 40%
sunny        -> 35%
```

The rainy season (April–October) uses the `LOW_BATTERY_SOC` / `LOW_BATTERY_SOC_NORMAL` / `LOW_BATTERY_SOC_SUNNY` values from `.env` unchanged. Season adjustment is applied on top of the weather-aware threshold at run time, so no schedule changes are needed.

Test the current dynamic threshold:

```bash
python growatt_power_guard.py weather-threshold
```

## Battery Capacity & Runtime

Set your battery specs so the automation can estimate runtime, time topups accurately, and send low-runtime alerts:

```text
BATTERY_CAPACITY_WH=30000
BATTERY_BMS_CUTOFF_SOC=25
BATTERY_CHARGE_RATE_W=3000
```

`BATTERY_CAPACITY_WH` is the total nameplate capacity (e.g. 2 × 15 kWh = 30 000 Wh).
`BATTERY_BMS_CUTOFF_SOC` is the SOC at which the BMS cuts off — runtime and topup estimates use this as the floor.
`BATTERY_CHARGE_RATE_W` is the AC charger output. Required for topup duration estimates and auto-topup.

To measure the actual charge rate from your inverter:

```bash
.venv/bin/python growatt_power_guard.py estimate-charge-rate --wait-seconds 900
```

Run this while on Utility (charging). The command reads SOC before and after the wait and prints an estimate.

You can also let auto-topup measure it passively: each time `topup-complete-check` completes a topup it compares starting and ending SOC to back-calculate the implied rate and prints it. If the implied rate differs from `BATTERY_CHARGE_RATE_W` by 10% or more, the output suggests an updated value and a Discord embed is sent.

### Charge Ceiling

To stop `watchdog-sbu` from returning to SBU while the battery is still charging toward a useful level, set a target SOC:

```text
BATTERY_CHARGE_TARGET_SOC=75
```

`watchdog-sbu` will hold on Utility until SOC reaches 75%, then repair to SBU normally. Set to `0` to disable.

### Auto-Topup at Night

Enable auto-topup to automatically charge from Utility at night when the battery won't survive until sunrise:

```text
AUTO_TOPUP_ENABLED=true
AUTO_TOPUP_MIN_HOURS_TO_SUNRISE=4
AUTO_TOPUP_TARGET_SOC=0
AUTO_TOPUP_SOLAR_SKIP_KWH_M2=0
AUTO_TOPUP_SOLAR_SKIP_MIN_MARGIN_MINUTES=60
```

Requires `BATTERY_CAPACITY_WH`, `BATTERY_CHARGE_RATE_W`, `WEATHER_LAT`, and `WEATHER_LON`.

`AUTO_TOPUP_MIN_HOURS_TO_SUNRISE` prevents late-night fires: if sunrise is less than 4 hours away, `auto-topup-check` exits immediately. With a 06:30 sunrise that means no new topups after ~02:30. Set to `0` to disable the cutoff.

`AUTO_TOPUP_TARGET_SOC` is an optional reserve target for sunrise. `AUTO_TOPUP_SOLAR_SKIP_KWH_M2` may skip only optional reserve topups on sunny forecasts; it will not skip topups needed to reach sunrise plus `AUTO_TOPUP_SOLAR_SKIP_MIN_MARGIN_MINUTES`.

The bundled schedule uses 20-minute start checks and 10-minute completion checks:

```text
*/20 22-23,0-2 * * *  auto-topup-check      # starts a topup if needed, exits immediately
*/10 22-23,0-6 * * *  topup-complete-check  # resumes automation once the topup window expires
```

`auto-topup-check` is non-blocking: it evaluates whether a topup is needed, starts one if so (pausing automation, switching to Utility, writing state), and exits in seconds. `topup-complete-check` detects when the window has elapsed, resumes automation, and calls `return-sbu`.

The Discord control bot also accepts `/growatt_topup_cancel` to abort a running topup early.

### Low Runtime Alert

Send a Discord alert when estimated battery runtime drops below a threshold:

```text
RUNTIME_ALERT_MINUTES=90
RUNTIME_ALERT_CLEAR_MINUTES=120
```

`RUNTIME_ALERT_MINUTES` triggers the alert. `RUNTIME_ALERT_CLEAR_MINUTES` clears it when runtime recovers (defaults to 1.5× the alert threshold if unset). State is tracked so the alert fires once and clears once, with no repeat spam.

Add a cron job (e.g. every 15 min):

```text
*/15 * * * *   runtime-alert
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
auto-topup-check starts a night topup when battery won't reach sunrise
topup-complete-check resumes automation after topup, with SOC delta and implied charge rate
daily-summary posts the end-of-day summary
weekly-summary posts the weekly performance report
monthly-summary posts the 30-day performance summary
health-check --notify posts readiness diagnostics
battery-alert detects or clears an emergency SOC episode
runtime-alert sends and clears a low-runtime warning
Growatt cloud failures alert after repeated consecutive failures
other command failures alert immediately, if DISCORD_NOTIFY_FAILURE=true
checks are skipped, only if DISCORD_NOTIFY_SKIP=true
```

All Discord notifications are sent as rich embeds with colour-coded severity.

## Discord Control Bot

The webhook above is send-only. To trigger safe write actions from Discord, run the optional private control bot.

Create a Discord application/bot, invite it to your server, then put it in a private control channel. In Discord, enable Developer Mode and copy:

```text
your Discord user ID
the private channel ID
the server/guild ID
the bot token
```

Add these to `.env`:

```text
DISCORD_BOT_TOKEN=your_bot_token
DISCORD_CONTROL_CHANNEL_ID=your_private_channel_id
DISCORD_CONTROL_ALLOWED_USER_IDS=your_discord_user_id
DISCORD_CONTROL_GUILD_ID=your_server_id
DISCORD_TOPUP_MAX_MINUTES=180
```

Install the bot service on the VPS:

```bash
cd ~/automation
.venv/bin/python -m pip install -r requirements.txt
./install_discord_bot_service.sh
```

Available slash commands:

```text
/growatt_status      — run the status command and show key metrics
/growatt_health      — run the health check and show results
/growatt_dashboard   — show live SOC, output mode, battery power, load, PVOutput at a glance
/growatt_refresh     — force an immediate dashboard refresh
/growatt_pause       — pause scheduled mode-changing automation
/growatt_resume      — resume automation after a pause
/growatt_sbu         — manually switch to SBU priority
/growatt_utility     — manually switch to Utility first
/growatt_preserve    — run preserve-battery immediately
/growatt_topup        — charge from grid for N minutes (or to a target SOC), then return to SBU
/growatt_topup_cancel — abort a running topup early and return to SBU
```

`/growatt_topup minutes:60` (or `target_soc:80`) pauses scheduled mode-changing automation, switches to Utility, waits, resumes automation, then returns to SBU.

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
python .\growatt_power_guard.py force-utility --reason "manual top-up"
python .\growatt_power_guard.py return-sbu
python .\growatt_power_guard.py watchdog-sbu
python .\growatt_power_guard.py daily-summary
python .\growatt_power_guard.py weekly-summary
python .\growatt_power_guard.py monthly-summary
python .\growatt_power_guard.py rotate-logs
python .\growatt_power_guard.py weather-threshold
python .\growatt_power_guard.py validate-schedule
python .\growatt_power_guard.py health-check
python .\growatt_power_guard.py health-check --notify
python .\growatt_power_guard.py battery-alert
python .\growatt_power_guard.py runtime-alert
python .\growatt_power_guard.py auto-topup-check
python .\growatt_power_guard.py topup-complete-check
python .\growatt_power_guard.py estimate-charge-rate --wait-seconds 900
python .\growatt_power_guard.py dashboard
python .\growatt_power_guard.py dashboard-refresh --once
python .\growatt_power_guard.py observability-refresh
python .\growatt_power_guard.py dashboard-stale-alert
python .\growatt_power_guard.py serve-dashboard
python .\growatt_power_guard.py serve-discord-bot
python .\growatt_power_guard.py pause --hours 6 --reason "maintenance"
python .\growatt_power_guard.py pause-status
python .\growatt_power_guard.py resume
python .\growatt_power_guard.py schedule-preview
python .\growatt_power_guard.py schedule-preview --days 14
python .\growatt_power_guard.py run-scheduled morning-preserve --dry-plan
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
.venv/bin/python growatt_power_guard.py force-utility --reason "manual top-up"
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
.venv/bin/python growatt_power_guard.py runtime-alert
.venv/bin/python growatt_power_guard.py auto-topup-check
.venv/bin/python growatt_power_guard.py topup-complete-check
.venv/bin/python growatt_power_guard.py estimate-charge-rate --wait-seconds 900
.venv/bin/python growatt_power_guard.py dashboard
.venv/bin/python growatt_power_guard.py dashboard-refresh --once
.venv/bin/python growatt_power_guard.py observability-refresh
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

The weekly summary includes a threshold tuning block with the observed SOC range,
near-cutoff count, auto-topup start SOC, and a conservative recommendation on
whether to hold or trial a slightly lower `LOW_BATTERY_SOC`.

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
growatt-dashboard-refresh.service  refreshes dashboard.html and PVOutput every 10 minutes
growatt-dashboard-server.service   serves the static file on 127.0.0.1:8080
growatt-dashboard-stale-alert.timer checks dashboard freshness every 10 minutes
```

Browser refreshes do not call Growatt. Only the refresh service calls Growatt, and only on the configured interval.
The refresh service uses one Growatt status read for both the dashboard and PVOutput uploads, if PVOutput is enabled.
The dashboard includes a health badge that turns stale when `dashboard.html` is older than `DASHBOARD_STALE_MINUTES`.
Each refresh also appends a compact local snapshot to `logs/dashboard_metrics.jsonl`.
The dashboard uses that local history for PV/load/grid/SOC charts, so chart views do
not add extra Growatt API calls.
Each refresh also writes `dashboard.json` beside `dashboard.html`. The JSON file
contains the same live metrics, metric source paths, freshness metadata, schedule
summary, PVOutput state, and tonight risk planner data used by the dashboard.

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
.venv/bin/python growatt_power_guard.py service-status
sudo systemctl status growatt-dashboard-refresh.service
sudo systemctl status growatt-dashboard-server.service
sudo systemctl status growatt-dashboard-stale-alert.timer
```

For support/debugging without exposing secrets:

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py diagnostic-bundle
```

`diagnostic-bundle` is local/read-only and does not call Growatt. Use
`health-check` when you specifically want a live cloud connectivity check.

Stale dashboard alerts use Discord when `DISCORD_WEBHOOK_URL` is configured and `DISCORD_NOTIFY_FAILURE=true`:

```text
DASHBOARD_STALE_MINUTES=30
```

Manual freshness check:

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py dashboard-stale-alert
```

## PVOutput Upload

Enable PVOutput in `.env`:

```text
PVOUTPUT_ENABLED=true
PVOUTPUT_API_KEY=your_pvoutput_api_key
PVOUTPUT_SYSTEM_ID=your_pvoutput_system_id
```

Manual upload test:

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py pvoutput-upload
```

If the dashboard service is installed, do not run a separate `pvoutput-upload` cron job. The service runs `observability-refresh`, which reads Growatt once and uses that same status for both `dashboard.html` and PVOutput.

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
growatt_guard/
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
install_discord_bot_service.sh
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
