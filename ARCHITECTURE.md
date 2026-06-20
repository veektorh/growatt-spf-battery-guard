# Architecture

This repository is a small Python automation service with one command-line entry point and focused helper modules.

## Runtime Flow

```text
cron/systemd/manual shell
  -> growatt_power_guard.py
  -> growatt_guard.cli
  -> command implementation in growatt_power_guard.py
  -> focused helper modules under growatt_guard/
```

`growatt_power_guard.py` remains the public script users run. It imports and re-exports many helpers for compatibility, but most implementation detail now lives in `growatt_guard/`.

## Module Boundaries

```text
growatt_guard.config
  Loads .env and returns Config.

growatt_guard.cli
  Owns argparse, scheduled command token parsing, dispatch, and main().

growatt_guard.growatt_api
  Talks to Growatt/ShinePhone cloud, reads status, extracts SOC/output source,
  and sends output-source mode commands.

growatt_guard.schedule
  Validates schedule.json and schedule_overrides.json, computes scheduled jobs,
  and checks installed cron entries.

growatt_guard.state
  Owns local JSON state and lock files under state/.

growatt_guard.notifications
  Owns Discord delivery and Growatt cloud failure streak notifications.

growatt_guard.audit
  Owns logs/mode_decisions.csv, daily summaries, weekly summaries, and log counters.

growatt_guard.weather
  Owns optional Open-Meteo forecast reads and weather-aware threshold selection.

growatt_guard.dashboard
  Owns dashboard.html rendering, refresh loop, static server, and stale dashboard alerts.
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
dashboard-stale-alert
serve-dashboard
validate-schedule
pause-status
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

```text
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
