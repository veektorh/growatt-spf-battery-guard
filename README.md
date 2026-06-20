# Growatt SPF Battery Preservation Guard

This automates the rainy-season routine for a Growatt SPF 6000 ES on ShinePhone:

- `preserve-battery` runs before known outage windows.
- It reads battery SOC from Growatt/ShinePhone.
- If SOC is below `LOW_BATTERY_SOC`, it switches to Utility while estate power is available.
- `return-sbu` runs a few minutes before each outage and switches back to SBU.

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
any command fails, if DISCORD_NOTIFY_FAILURE=true
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
06:30 daily       preserve-battery if SOC is below 50%
07:55 daily       return to SBU before the 08:00 outage
14:30 weekdays    preserve-battery if SOC is below 50%
15:25 weekdays    return to SBU before the 15:30 outage
```

## Run manually

```powershell
python .\growatt_power_guard.py preserve-battery
python .\growatt_power_guard.py return-sbu
```

## Schedule on Windows

These commands create the current estate schedule:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_growatt_schedule.ps1
```

Or create them manually:

```powershell
schtasks /Create /F /TN "Growatt Utility Check Morning" /SC DAILY /ST 06:30 /TR "cmd /c cd /d C:\path\to\automation && python growatt_power_guard.py preserve-battery"
schtasks /Create /F /TN "Growatt SBU Before Morning Outage" /SC DAILY /ST 07:55 /TR "cmd /c cd /d C:\path\to\automation && python growatt_power_guard.py return-sbu"
schtasks /Create /F /TN "Growatt Utility Check Afternoon" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 14:30 /TR "cmd /c cd /d C:\path\to\automation && python growatt_power_guard.py preserve-battery"
schtasks /Create /F /TN "Growatt SBU Before Afternoon Outage" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 15:25 /TR "cmd /c cd /d C:\path\to\automation && python growatt_power_guard.py return-sbu"
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
DRY_RUN=true
GROWATT_MODE_DRIVER=spf5000
```

Test it:

```bash
.venv/bin/python growatt_power_guard.py status
.venv/bin/python growatt_power_guard.py test-discord
.venv/bin/python growatt_power_guard.py preserve-battery
.venv/bin/python growatt_power_guard.py return-sbu
```

After the dry-run output is correct, set `DRY_RUN=false`, then install the cloud cron schedule:

```bash
chmod +x install_cloud_cron.sh
./install_cloud_cron.sh
```

Verify the four scheduled jobs:

```bash
crontab -l | grep growatt-power-guard
```

Cron logs go to:

```text
~/automation/logs/cron.log
~/automation/logs/growatt_power_guard.log
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
```

Change the battery preservation threshold:

```bash
nano ~/automation/.env
```

Then edit:

```text
LOW_BATTERY_SOC=50
```

Pause the cloud schedule:

```bash
crontab -l | grep -v growatt-power-guard | crontab -
```

Reinstall the current schedule:

```bash
cd ~/automation
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
install_cloud_cron.sh
install_growatt_schedule.ps1
tests/
.gitignore
```

Do not publish:

```text
.env
logs/
growatt-probe-*.json
```

Before pushing, check for private values:

```bash
grep -R "your_real_username\|your_real_device_serial\|your_real_plant_id" .
```
