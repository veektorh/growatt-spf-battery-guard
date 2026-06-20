# Public Release Checklist

Use this before pushing the repository to a public GitHub repo.

## Keep Private

Do not commit these files or folders:

```text
.env
logs/
growatt-probe-*.json
state/
schedule_overrides.json
dashboard.html
.venv/
__pycache__/
```

## Check For Device Identifiers

Search for real Growatt values before publishing:

```bash
grep -R "GROWATT_USERNAME=\|GROWATT_PASSWORD=\|GROWATT_PLANT_ID=\|GROWATT_DEVICE_SN=" .
```

If weather support is enabled locally, also avoid committing exact coordinates:

```bash
grep -R "WEATHER_LAT=\|WEATHER_LON=" .
```

Also check that no real Discord webhook URL is committed:

```bash
grep -R "discord.com/api/webhooks/[0-9]" .
```

The public repo should only contain placeholders such as:

```text
GROWATT_USERNAME=your_shinephone_username
GROWATT_PASSWORD=your_shinephone_password
GROWATT_PLANT_ID=your_plant_id
GROWATT_DEVICE_SN=your_device_sn
```

## Safe Public Files

These are safe to publish after the checks above:

```text
growatt_power_guard.py
requirements.txt
README.md
RUNBOOK.md
PUBLIC_RELEASE_CHECKLIST.md
.env.example
.gitignore
schedule.json
schedule_overrides.example.json
install_cloud_cron.sh
install_growatt_schedule.ps1
update_server.sh
tests/
```
