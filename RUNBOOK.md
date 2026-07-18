# Growatt Automation Runbook

## Current Schedule

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
*/20 22-23,0-2   start night auto-topup only if needed
21:20 Sundays     post weekly operational review
*/10 22-23,0-6   complete an expired auto-topup and return to SBU
21:10 Sundays     post weekly performance summary
00:10 daily       prune old generated probe/temp files
00:20 monthly     prune audit CSV rows older than retention
```

## Key Commands

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py status
.venv/bin/python growatt_power_guard.py test-discord
.venv/bin/python growatt_power_guard.py preserve-battery
.venv/bin/python growatt_power_guard.py return-sbu
.venv/bin/python growatt_power_guard.py watchdog-sbu
.venv/bin/python growatt_power_guard.py daily-summary
.venv/bin/python growatt_power_guard.py weekly-summary
.venv/bin/python growatt_power_guard.py monthly-summary
.venv/bin/python growatt_power_guard.py rotate-logs
.venv/bin/python growatt_power_guard.py weather-threshold
.venv/bin/python growatt_power_guard.py validate-schedule
.venv/bin/python growatt_power_guard.py health-check
.venv/bin/python growatt_power_guard.py health-check --notify
.venv/bin/python growatt_power_guard.py ops-review --days 7
.venv/bin/python growatt_power_guard.py ops-review --days 7 --json
.venv/bin/python growatt_power_guard.py battery-alert
.venv/bin/python growatt_power_guard.py runtime-alert
.venv/bin/python growatt_power_guard.py auto-topup-check
.venv/bin/python growatt_power_guard.py topup-complete-check
.venv/bin/python growatt_power_guard.py estimate-charge-rate --wait-seconds 900
.venv/bin/python growatt_power_guard.py dashboard
.venv/bin/python growatt_power_guard.py pause --hours 6 --reason "maintenance"
.venv/bin/python growatt_power_guard.py pause-status
.venv/bin/python growatt_power_guard.py resume
.venv/bin/python growatt_power_guard.py clear-login-cooldown
.venv/bin/python growatt_power_guard.py schedule-preview
.venv/bin/python growatt_power_guard.py schedule-preview --days 14
.venv/bin/python growatt_power_guard.py run-scheduled morning-preserve --dry-plan
```

`weekly-summary` includes threshold tuning guidance based on the last 7 days of
audit rows, including lowest SOC, near-cutoff readings, and auto-topup behavior.
`ops-review --days 7` is a read-only operational review of the latest dashboard
snapshot, sunrise plan, mode audit rows, topup activity, estimated grid charge,
target-reached, expired, legacy, and unclosed topup closures, failures, current
automation state, last mode change, stale command locks, and trends versus the
previous equivalent window. Expired or unclosed top-ups and stale locks produce
WARN recommendations. Add `--notify` to post the review to Discord; delivery
failure exits with an error. The weekly Sunday schedule posts this review at 21:20.

## Pause Automation

Pause mode prevents scheduled mode changes while still allowing read-only checks, summaries, alerts, and dashboard generation:

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py pause --hours 6 --reason "maintenance"
.venv/bin/python growatt_power_guard.py pause-status
.venv/bin/python growatt_power_guard.py resume
```

Mode-changing commands use a local `state/mode_command.lock` file to avoid overlapping Growatt writes.

## Growatt Account Lockout (507)

Growatt locks an account for ~24h after too many logins in a short window. The
login response looks like:

```text
Growatt login failed: {'msg': '507', 'lockDuration': '24', 'success': False,
'error': 'Current account has been locked for 24 hours'}
```

The lock is a **rolling window** — every fresh login attempt can reset the 24h
timer, so continuing to hit the API keeps the account locked indefinitely.

**Automatic protection.** On a 507, `connect()` writes a cooldown file
(`state/growatt_login_cooldown.json`) for `lockDuration` + 15 min and then
*refuses to attempt any login* until it expires, so scheduled jobs stop hammering
the account. A successful login clears it. `health-check` reports an active
cooldown as a WARN ("backing off until X"). Discord is not spammed (the
cloud-failure `alerted` flag de-dups).

**If you see this alert:**

1. Deploy the latest code if the cooldown logic isn't running yet (see *Diagnostics → update_server.sh*). Once deployed, the next failed login arms the cooldown and the hammering stops on its own.
2. Leave the account alone — do not repeatedly open ShinePhone/web to "test" it; each manual login can also reset the timer.
3. The cooldown auto-expires; the next scheduled job then logs in normally.

**If you confirm via ShinePhone that the account unlocked early:**

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py clear-login-cooldown
```

**Prevent recurrence — enable session reuse** (see *Important Config* below). It
caches the logged-in session and skips the rate-limited login endpoint on most
runs, which is what tripped the lock.

## Change Schedule

Edit `schedule.json`, then validate and reinstall:

```bash
cd ~/automation
nano schedule.json
.venv/bin/python growatt_power_guard.py validate-schedule
./install_cloud_cron.sh --dry-run
./install_cloud_cron.sh
```

Optional for manual shells: `.venv/bin/python -m pip install -e .` installs the
`growatt-guard` console script. Cron and service examples continue to use
`growatt_power_guard.py` for backwards compatibility. Run `./verify_local.sh`
before pushing local changes; it performs compile, quiet tests, schedule
validation, shell syntax, whitespace checks, and the public hygiene check. You
can also run `.venv/bin/python growatt_power_guard.py public-hygiene` by itself
before publishing docs or examples.

For temporary date changes, copy and edit the ignored override file:

```bash
cd ~/automation
cp schedule_overrides.example.json schedule_overrides.json
nano schedule_overrides.json
.venv/bin/python growatt_power_guard.py validate-schedule
./install_cloud_cron.sh --dry-run
./install_cloud_cron.sh
```

## Verify Cron

```bash
crontab -l | grep growatt-power-guard
```

Expected jobs:

```text
10 6 * * *
30 6 * * *
55 7 * * *
1 8 * * *
30 14 * * 1-5
25 15 * * 1-5
31 15 * * 1-5
0 21 * * *
*/30 * * * *
*/20 22-23,0-2 * * *
*/10 22-23,0-6 * * *
10 21 * * 0
10 0 * * *
```

## Logs

```bash
tail -n 120 ~/automation/logs/growatt_power_guard.log
tail -n 120 ~/automation/logs/cron.log
tail -n 40 ~/automation/logs/mode_decisions.csv
```

growatt_power_guard.log rotates automatically by size. Install cron.log rotation on Linux with:

```bash
cd ~/automation
sudo ./install_logrotate.sh
```

Success response:

```text
{'msg': 'inv_set_success', 'success': True}
```

## Pause Or Reinstall

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

## Important Config

```text
GROWATT_PLANT_ID=your_plant_id
GROWATT_DEVICE_SN=your_device_sn
LOW_BATTERY_SOC=50
EMERGENCY_SOC=30
EMERGENCY_SOC_RECOVERY=35
GROWATT_CLOUD_FAILURE_ALERT_THRESHOLD=3
DASHBOARD_STALE_MINUTES=30
GROWATT_MODE_DRIVER=spf5000

# Preserve-battery retries for transient Growatt mode-write failures
PRESERVE_UTILITY_MAX_ATTEMPTS=2
PRESERVE_UTILITY_RETRY_DELAY_SECONDS=30

# Session reuse: cache the Growatt session and reuse it until shortly before the
# configured TTL (0 = disabled, log in every run). Reduces logins from ~250/day
# to a handful and is the main defence against the 24h account lock (507).
# Growatt sessions have been observed expiring around 60 minutes, so 60 refreshes
# proactively at about 55 minutes.
# Enable after the account is healthy, then confirm the log shows
# "Reusing cached Growatt session" between logins. Concurrent stale-cache refreshes
# are coordinated through a local refresh lock so only one process logs in fresh.
# Set back to 0 to disable.
GROWATT_SESSION_TTL_MINUTES=60
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
DISCORD_BOT_TOKEN=your_bot_token
DISCORD_CONTROL_CHANNEL_ID=your_private_channel_id
DISCORD_CONTROL_ALLOWED_USER_IDS=your_discord_user_id
DISCORD_CONTROL_GUILD_ID=your_server_id
WEATHER_ENABLED=true
WEATHER_LAT=your_latitude
WEATHER_LON=your_longitude

# Battery capacity (required for runtime estimates, topup, and alerts)
BATTERY_CAPACITY_WH=30000
BATTERY_BMS_CUTOFF_SOC=25
BATTERY_CHARGE_RATE_W=3000

# Charge ceiling: hold off SBU repair until SOC reaches this level (0 = disabled)
BATTERY_CHARGE_TARGET_SOC=0

# Auto-topup: charge at night when battery won't last until sunrise (requires weather)
AUTO_TOPUP_ENABLED=false
AUTO_TOPUP_MIN_HOURS_TO_SUNRISE=4    # skip topup if sunrise is less than N hours away (0 = disabled)
AUTO_TOPUP_MIN_MINUTES=0             # skip calculated topups shorter than N minutes (0 = disabled)
AUTO_TOPUP_TARGET_SOC=0              # optional reserve SOC at sunrise
AUTO_TOPUP_SOLAR_SKIP_KWH_M2=0       # sunny forecast skip threshold (0 = disabled)
AUTO_TOPUP_SOLAR_SKIP_MIN_MARGIN_MINUTES=60

# Low runtime alert: Discord alert when estimated runtime drops below this (0 = disabled)
RUNTIME_ALERT_MINUTES=0
RUNTIME_ALERT_CLEAR_MINUTES=0
```

If SOC is at or below `BATTERY_BMS_CUTOFF_SOC` and Growatt does not report grid bypass or AC charging, `battery-alert` escalates to "Low battery and utility not detected". Treat that as an immediate utility-input/breaker/charger check, because the expected low-SOC transfer/charging did not appear.

## Diagnostics

Run a read-only health check:

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py health-check
.venv/bin/python growatt_power_guard.py health-check --notify
.venv/bin/python growatt_power_guard.py ops-review --days 7
.venv/bin/python growatt_power_guard.py battery-alert
.venv/bin/python growatt_power_guard.py weekly-summary
.venv/bin/python growatt_power_guard.py dashboard
.venv/bin/python growatt_power_guard.py dashboard-refresh --once
.venv/bin/python growatt_power_guard.py observability-refresh
.venv/bin/python growatt_power_guard.py dashboard-stale-alert
.venv/bin/python growatt_power_guard.py service-status
.venv/bin/python growatt_power_guard.py service-status --json
.venv/bin/python growatt_power_guard.py deployment-preflight
.venv/bin/python growatt_power_guard.py deployment-preflight --json
.venv/bin/python growatt_power_guard.py diagnostic-bundle
.venv/bin/python growatt_power_guard.py diagnostic-bundle --include-cloud
.venv/bin/python growatt_power_guard.py pv-metric-probe
.venv/bin/python growatt_power_guard.py schedule-preview --days 14 --json
.venv/bin/python growatt_power_guard.py schedule-calendar --days 14 --output growatt-schedule.ics
```

`health-check` reports readiness plus the next scheduled jobs. `service-status` is read-only and includes schedule lint, cron checks, dashboard
and PVOutput freshness, local pause/topup/lock state, and systemd active/enabled
restart detail where systemd is available.

Update the VPS from GitHub, reinstall cron, and run health check:

```bash
cd ~/automation
./update_server.sh
```

The checkout controls immutable packaged runtimes under `.deploy/releases/`.
Each update installs `requirements.lock`, verifies the source, builds and
validates a wheel, then atomically switches `.deploy/current`. Operational data
remains in the checkout and the three newest releases are retained. Validation
or post-activation failures restore the previous checkout and runtime. If
rollback itself fails, stop and recover the repository manually before
restarting services.

`update_server.sh` runs `deployment-preflight` and prints a preflight summary before pulling code. If an
auto-topup or Utility hold is active, the script refuses to continue until the
hold completes or is cancelled, to avoid interrupting return-to-SBU automation
during deploys.

To wait up to two hours for the hold to clear naturally, without cancelling it
or issuing any inverter command, run:

```bash
./update_server.sh --wait-for-clear 120
```

If Discord reports `Schedule job ... has unsupported command` after an update, the VPS is running a stale Python process or mismatched files. Run:

```bash
cd ~/automation
git pull --ff-only
.venv/bin/python growatt_power_guard.py --help | grep auto-topup-check
.venv/bin/python growatt_power_guard.py validate-schedule
sudo systemctl restart growatt-dashboard-refresh.service growatt-dashboard-server.service growatt-dashboard-stale-alert.timer
./install_cloud_cron.sh
```

If the failure message still says `dashboard-refresh`, check for an old background loop and stop it:

```bash
pgrep -af "growatt_power_guard.py"
pkill -f "growatt_power_guard.py dashboard-refresh"
sudo systemctl restart growatt-dashboard-refresh.service
```

Install safe dashboard services:

```bash
cd ~/automation
./install_dashboard_service.sh
```

View from your laptop:

```bash
ssh -L 8080:localhost:8080 ubuntu@YOUR_VPS_IP
```

Open:

```text
http://localhost:8080/dashboard.html
```

The server serves a static file. Growatt is only called by the refresh service every 15 minutes by default (override with `DASHBOARD_REFRESH_MINUTES`).
That refresh service uses one Growatt read for both `dashboard.html` and PVOutput uploads when PVOutput is enabled.
The dashboard page shows a freshness badge, and `growatt-dashboard-stale-alert.timer` sends Discord alerts when `dashboard.html` is older than `DASHBOARD_STALE_MINUTES`.
Each refresh also writes `dashboard.json` with live metrics, metric source paths,
schedule summary, next automation action, PVOutput state, data-quality status,
`quality.data.reconciliation` energy-balance status, same-time daily insights,
and tonight risk planner data
for monitors or future apps. The built-in dashboard server serves it at
`/dashboard.json` without making another Growatt API call. The public read-only
contract is the top-level `generated_at`, `metrics`, `sources`, `planner`,
`schedule`, `automation`, `pvoutput`, `quality`, `history`, `assistant`, and
`freshness` objects. `freshness.last_successful_growatt_read_at` is the
latest dashboard source read, and `freshness.last_successful_pvoutput_upload_at`
is copied from the last successful PVOutput upload state when available.

```text
DASHBOARD_STALE_MINUTES=30
```

If a separate 10-minute `pvoutput-upload` cron job exists, remove it after installing the dashboard service. `observability-refresh` replaces that duplicate poller.

## Discord Control Bot

The control bot is optional and separate from the send-only Discord webhook. It should only be invited to a private control channel and allowlisted to your Discord user ID.

Available slash commands: `/growatt_status`, `/growatt_health`, `/growatt_dashboard`, `/growatt_refresh`, `/growatt_pause`, `/growatt_resume`, `/growatt_sbu`, `/growatt_utility`, `/growatt_preserve`, `/growatt_topup`, `/growatt_topup_cancel`.

`/growatt_dashboard` shows live SOC, output mode, battery power, load, and PVOutput at a glance without running a full status command in the channel.

Install or restart it:

```bash
cd ~/automation
.venv/bin/python -m pip install -r requirements.txt
./install_discord_bot_service.sh
```

Check status and logs:

```bash
sudo systemctl status growatt-discord-control.service
journalctl -u growatt-discord-control.service -n 80 --no-pager
```

Emergency stop for all Discord write controls:

```bash
sudo systemctl stop growatt-discord-control.service
```

Expose on a dashboard subdomain:

```bash
cd ~/automation
./install_dashboard_service.sh
DASHBOARD_DOMAIN=dashboard.example.com DASHBOARD_EMAIL=you@example.com ./install_dashboard_proxy.sh
```

Keep basic auth enabled for normal internet-facing deployments. The dashboard is read-only, but it exposes SOC, output mode, load/grid/PV power, schedule timing, freshness timestamps, PVOutput state, and recent local history through both `dashboard.html` and `/dashboard.json`. Disable auth only for a deliberate public status page.

To expose it without basic auth:

```bash
DASHBOARD_AUTH_ENABLED=false DASHBOARD_DOMAIN=dashboard.example.com DASHBOARD_EMAIL=you@example.com ./install_dashboard_proxy.sh
```

Before running the proxy installer, create an `A` record for the dashboard subdomain pointing to the VPS public IP and open ports `80` and `443`.

Growatt cloud flakiness alerts:

```text
GROWATT_CLOUD_FAILURE_ALERT_THRESHOLD=3
```

Discord alerts after 3 consecutive Growatt cloud login/status failures, then sends a recovery message when cloud reads work again.

## Weather Thresholds

Weather-aware thresholds are conservative:

```text
rainy/cloudy -> 50%
normal       -> 45%
sunny        -> 40%
```

Check the current threshold:

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py weather-threshold
```

## Discord Alerts

The automation can post to Discord on successful mode switches, health reports, emergency battery alerts, daily/weekly/monthly summaries, repeated Growatt cloud failures, recoveries, and other failures. All notifications use rich embeds with colour-coded severity.

```text
DISCORD_NOTIFY_SUCCESS=true
DISCORD_NOTIFY_SKIP=false
DISCORD_NOTIFY_FAILURE=true
```

Test after changing the webhook:

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py test-discord
```

If Discord returns `HTTP 403: Forbidden`, regenerate the webhook and replace `DISCORD_WEBHOOK_URL` in `.env` with the fresh full URL.
