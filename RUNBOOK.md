# Growatt Automation Runbook

## Current Schedule

```text
06:30 daily       preserve-battery if SOC is below 50%
07:55 daily       return to SBU before the 08:00 outage
08:01 daily       verify SBU and retry once if needed
14:30 weekdays    preserve-battery if SOC is below 50%
15:25 weekdays    return to SBU before the 15:30 outage
15:31 weekdays    verify SBU and retry once if needed
21:00 daily       post Discord daily summary
00:10 daily       rotate old generated logs/probes
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
.venv/bin/python growatt_power_guard.py rotate-logs
.venv/bin/python growatt_power_guard.py weather-threshold
.venv/bin/python growatt_power_guard.py validate-schedule
.venv/bin/python growatt_power_guard.py pause --hours 6 --reason "maintenance"
.venv/bin/python growatt_power_guard.py pause-status
.venv/bin/python growatt_power_guard.py resume
```

## Pause Automation

Pause mode prevents scheduled mode changes while still allowing read-only checks and summaries:

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py pause --hours 6 --reason "maintenance"
.venv/bin/python growatt_power_guard.py pause-status
.venv/bin/python growatt_power_guard.py resume
```

## Change Schedule

Edit `schedule.json`, then validate and reinstall:

```bash
cd ~/automation
nano schedule.json
.venv/bin/python growatt_power_guard.py validate-schedule
./install_cloud_cron.sh
```

## Verify Cron

```bash
crontab -l | grep growatt-power-guard
```

Expected jobs:

```text
30 6 * * *
55 7 * * *
1 8 * * *
30 14 * * 1-5
25 15 * * 1-5
31 15 * * 1-5
0 21 * * *
10 0 * * *
```

## Logs

```bash
tail -n 120 ~/automation/logs/growatt_power_guard.log
tail -n 120 ~/automation/logs/cron.log
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
GROWATT_MODE_DRIVER=spf5000
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
WEATHER_ENABLED=true
WEATHER_LAT=your_latitude
WEATHER_LON=your_longitude
```

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

The automation can post to Discord on successful mode switches and failures.

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
