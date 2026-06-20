# Architecture

This repository is a small Python automation service with one command-line entry point and focused helper modules.

## Runtime Flow

```text
cron/systemd/manual shell
  -> growatt_power_guard.py  (thin shim; re-exports all public symbols)
  -> growatt_guard.cli       (argparse, dispatch, main)
  -> growatt_guard.modes / growatt_guard.health / growatt_guard.pause
  -> focused helper modules under growatt_guard/
```

`growatt_power_guard.py` is the public script users run. It is a thin import-and-re-export shim; all implementation lives in `growatt_guard/`.

## Module Boundaries

```text
growatt_guard.exceptions
  Defines GrowattGuardError so helper modules can raise it without importing the shim.

growatt_guard.config
  Loads .env and returns Config.

growatt_guard.cli
  Owns argparse, scheduled command token parsing, dispatch, and main().

growatt_guard.growatt_api
  Talks to Growatt/ShinePhone cloud, reads status, extracts SOC/output source,
  and sends output-source mode commands.

growatt_guard.schedule
  Validates schedule.json and schedule_overrides.json, computes scheduled jobs,
  checks installed cron entries, and implements schedule-preview.

growatt_guard.state
  Owns local JSON state and lock files under state/.

growatt_guard.notifications
  Owns Discord delivery and Growatt cloud failure streak notifications.

growatt_guard.audit
  Owns logs/mode_decisions.csv, daily summaries, weekly summaries, and log counters.

growatt_guard.weather
  Owns optional Open-Meteo forecast reads and weather-aware threshold selection.

growatt_guard.dashboard
  Owns dashboard.html rendering, observability refresh loop, static server, and stale dashboard alerts.

growatt_guard.pvoutput
  Owns PVOutput field extraction, upload, extended-field fallback, and upload state.

growatt_guard.pause
  Owns pause/resume state checks and the mode-command lock.

growatt_guard.health
  Owns the health-check command and health report formatting.

growatt_guard.modes
  Owns all remaining command implementations: preserve-battery, return-sbu,
  watchdog-sbu, run-scheduled (including --dry-plan), battery-alert, summaries, etc.
```

## Command Categories

Mode-changing commands:

```text
preserve-battery
utility-check
morning-check
return-sbu
watchdog-sbu
```

These commands use the mode-command lock in `state/mode_command.lock` to avoid overlapping Growatt writes. They also respect the pause state.

Read-only or reporting commands:

```text
status
probe
health-check
daily-summary
weekly-summary
weather-threshold
battery-alert
dashboard
dashboard-refresh
observability-refresh
dashboard-stale-alert
serve-dashboard
validate-schedule
pause-status
schedule-preview
run-scheduled --dry-plan
```

Pause/resume commands:

```text
pause
resume
```

## Important Files

```text
.env                         local secrets and runtime config; never commit
.env.example                 public-safe config template
schedule.json                source of truth for cron schedule
schedule_overrides.json      local temporary date overrides; never commit
schedule_overrides.example.json
logs/growatt_power_guard.log runtime log; never commit
logs/cron.log                cron output; never commit
logs/mode_decisions.csv      audit trail; never commit
state/*.json                 local automation state; never commit
dashboard.html               generated dashboard; never commit
```

## Schedule Flow

Cloud cron should call:

```bash
.venv/bin/python growatt_power_guard.py run-scheduled <job-id>
```

`run-scheduled` does this:

```text
validate schedule.json
load schedule_overrides.json if present
skip or replace today's job if an override applies
parse the target command tokens through the normal CLI parser
dispatch the command
```

This keeps scheduled jobs and manual command behavior consistent.

## Dashboard Flow

The dashboard is static HTML. The refresh service periodically calls Growatt and writes `dashboard.html`; the server only serves that file.
The preferred refresh path is `observability-refresh`, which reuses the same Growatt status read for dashboard generation and PVOutput upload.

```text
observability-refresh
  -> load Growatt status once
  -> validate schedule and overrides
  -> choose preserve threshold
  -> read audit/state summaries
  -> write dashboard.html
  -> upload PVOutput if enabled

dashboard-refresh
  -> load Growatt status
  -> validate schedule and overrides
  -> choose preserve threshold
  -> read audit/state summaries
  -> write dashboard.html

serve-dashboard
  -> serve the existing dashboard.html
```

`dashboard-stale-alert` checks the generated file age and sends Discord alerts when refreshes stop.

## Testing Strategy

Tests use `unittest` and mock external boundaries. They should not require `.env`, network access, cron, systemd, Growatt, Discord, or Open-Meteo.

Core verification:

```bash
python -m py_compile growatt_power_guard.py growatt_guard/*.py
python -m unittest discover -s tests
python growatt_power_guard.py validate-schedule
```

GitHub Actions runs the same checks on pushes and pull requests to `main`.
