# Agent Guide

This file is for coding agents working on this repository. Read it before editing.

## Mission

This project automates battery-preservation mode switching for a Growatt SPF inverter through the Growatt/ShinePhone cloud API. It is safety-sensitive because some commands can change inverter output-source mode.

## Safety Rules

- Never commit `.env`, credentials, Discord webhook URLs, real Growatt plant/device IDs, generated probe data, logs, state files, `dashboard.html`, or local override files.
- Do not add personal domains, real IP addresses, exact weather coordinates, usernames, passwords, serial numbers, or webhook tokens to docs, tests, fixtures, or examples.
- Do not run live mode-changing commands such as `preserve-battery`, `return-sbu`, `watchdog-sbu`, or scheduled jobs unless the user explicitly asks and the current `.env`/`DRY_RUN` state is understood.
- Prefer read-only checks while developing: `validate-schedule`, `health-check`, parser tests, unit tests, and dry-run command paths.
- Keep schedule edits conservative. After changing `schedule.json` or schedule validation logic, run `python growatt_power_guard.py validate-schedule`.
- Keep dashboard refresh intervals at or above the built-in minimum unless using `--once`; avoid changes that increase Growatt API polling frequency.

## Repo Map

- `growatt_power_guard.py`: thin shim that imports and re-exports all public symbols from `growatt_guard/`. Keep it a pure re-export; add no logic here.
- `growatt_guard/cli.py`: argparse parser, command dispatch, and `main()`.
- `growatt_guard/config.py`: `.env` loading and `Config`.
- `growatt_guard/exceptions.py`: `GrowattGuardError`; imported by helpers to avoid circular imports with the shim.
- `growatt_guard/growatt_api.py`: Growatt session/device coordination, status probing, mode writes, and verification.
- `growatt_guard/growatt_telemetry.py`: pure recursive telemetry extraction, normalization, runtime estimates, and bypass detection.
- `growatt_guard/schedule.py`: cron parsing, schedule validation/lint, and the canonical effective-job model.
- `growatt_guard/schedule_views.py`: calendar and preview JSON/terminal presentation.
- `growatt_guard/schedule_overrides.py`: override persistence/mutation commands and outage profiles.
- `growatt_guard/modes.py`: core inverter mode commands (`preserve-battery`, `force-utility`, `return-sbu`, `watchdog-sbu`), scheduled dispatch, charge-rate estimation, and `test-discord`.
- `growatt_guard/topup.py`: auto-topup planning, canonical hold persistence, completion policies, and Utility adoption.
- `growatt_guard/alerts.py`: battery, bypass, runtime, and avoidable-Utility-waste alert commands.
- `growatt_guard/reports.py`: daily/weekly/monthly summaries, log rotation, audit pruning, and weather-threshold reporting.
- `growatt_guard/pause.py`: pause/resume state checks and the mode-command lock (`ensure_not_paused`, `run_with_command_lock`).
- `growatt_guard/health.py`: `health-check` command and health report formatting.
- `growatt_guard/dashboard.py`: dashboard HTML renderer and compatibility exports.
- `growatt_guard/dashboard_metrics.py`: normalized typed metrics and local metric history.
- `growatt_guard/dashboard_insights.py` / `dashboard_planning.py`: dashboard risk, reconciliation, recommendations, and energy planning.
- `growatt_guard/dashboard_viewmodel.py`: the canonical JSON/view-model assembly boundary.
- `growatt_guard/dashboard_render_components.py`: reusable HTML presentation components.
- `growatt_guard/dashboard_service.py`: refresh orchestration, stale alerts, and static serving.
- `growatt_guard/assets/`: packaged dashboard CSS and JavaScript sources.
- `growatt_guard/pvoutput.py`: PVOutput field extraction, upload, retry without extended fields, and upload state.
- `growatt_guard/discord_control.py`: optional private Discord control bot; keep commands allowlisted and route writes through existing CLI commands. `/growatt_dashboard` renders a live embed from `status` subprocess output without posting raw text to the channel.
- `growatt_guard/audit.py`: mode decision CSV audit trail, daily summary, weekly summary, and log counters.
- `growatt_guard/notifications.py`: Discord embed delivery and Growatt cloud failure streak tracking. All notifications use `send_discord_embed`; `send_discord_message` is for plain-text test messages only.
- `growatt_guard/state.py`: local state files, pause state, alert state, command locks, and timestamps.
- `growatt_guard/weather.py`: Open-Meteo forecast fetch, weather-aware threshold decisions, and season profiles (rainy April–October / dry November–March for Lagos). Enable with `SEASON_PROFILES_ENABLED=true`.
- `tests/`: unittest coverage split across modules — `test_growatt_power_guard.py` (integration/command behavior), `test_notifications.py` (embed builders), `test_growatt_api.py` (API extraction helpers), `test_schedule.py` (schedule logic), `test_dashboard.py` (dashboard generation), `test_cli.py` (CLI parsing), and others.
- `schedule.json`: source of truth for cloud cron jobs.
- `schedule_overrides.example.json`: public-safe template for temporary local date overrides.
- `RUNBOOK.md`: operations guide for the VPS.
- `ROADMAP.md`: backlog of improvement and enhancement ideas.
- `PUBLIC_RELEASE_CHECKLIST.md`: public-repo hygiene checks.

## Local Verification

Run these before committing code changes:

```bash
python -m py_compile growatt_power_guard.py growatt_guard/*.py
python -m unittest discover -s tests
python growatt_power_guard.py validate-schedule
git diff --check
```

For docs-only changes, at minimum run:

```bash
git diff --check
```

Before pushing public docs or examples, also search for secrets or personal values:

```bash
rg -n "GROWATT_USERNAME|GROWATT_PASSWORD|GROWATT_PLANT_ID|GROWATT_DEVICE_SN|discord.com/api/webhooks|WEATHER_LAT|WEATHER_LON" .
```

Expected matches should be placeholders in `.env.example`, README/RUNBOOK examples, or public release checklist text. Do not commit real values.

## Deployment Notes

The VPS normally updates from GitHub:

```bash
cd ~/automation
git pull
.venv/bin/python growatt_power_guard.py validate-schedule
.venv/bin/python growatt_power_guard.py health-check
```

After changing `schedule.json`, reinstall cron on the VPS:

```bash
cd ~/automation
.venv/bin/python growatt_power_guard.py validate-schedule
./install_cloud_cron.sh
```

After changing dashboard service/proxy scripts, review `RUNBOOK.md` and the installer scripts together.

## Design Expectations

- Keep modules focused. Put new logic beside the owner module listed above instead of growing `growatt_power_guard.py`.
- Preserve command names and public imports from `growatt_power_guard.py` when reasonable; tests and user scripts may import from it.
- Use structured JSON parsing for schedules, state, and API responses.
- Keep tests offline. Tests must not call Growatt, Discord, Open-Meteo, cron, systemd, or external networks.
- Use placeholders in docs and examples.
- Keep shell installer scripts idempotent and readable.
- Avoid broad refactors when a narrow change is enough.
