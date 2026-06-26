# Roadmap And Enhancement Ideas

This is the working backlog for Growatt Guard. The order below favors safety,
low Growatt API pressure, clear operations, and useful visibility before bigger
product ideas.

## Current Baseline

The project already has the main automation shape in place:

- Cloud-safe schedule for morning/afternoon preserve windows, watchdog SBU
  repairs, health checks, summaries, and night auto-topup.
- Session reuse and Growatt login cooldown handling to reduce account lock risk.
- Confirmed mode writes: after a Utility/SBU write, the tool re-reads status and
  warns if the inverter does not verify the expected mode.
- Auto-topup with sunrise estimates, load history, minimum topup floor, sunny
  forecast handling, and orphaned topup repair after deploys or restarts.
- Discord rich embeds for mode changes, failures, cloud recovery, health,
  summaries, emergency battery, runtime, and topup events.
- Private Discord control bot with allowlisted slash commands.
- Dashboard with live flow, daily energy totals, local metric history, charts,
  freshness badge, stale alerts, PVOutput status, schedule visibility, tonight
  risk planning, same-time energy insights, JSON export, metric source paths,
  and system/automation cards.
- Shared observability refresh: one Growatt read updates the dashboard and
  PVOutput, avoiding duplicate pollers.
- PVOutput upload with fallback when extended fields are rejected.
- Schedule overrides, named outage profiles, dry-plan preview, and safer update
  script gates.
- Unit tests split by module and a public-repo hygiene guide.
- Read-only `service-status` and `diagnostic-bundle` commands for VPS support.
- JSON output for service status, diagnostic bundles, and schedule previews.
- Redacted PV metric probing for Growatt field-shape debugging.
- Schedule lint warnings for duplicate PVOutput pollers, fast polling, and
  tightly spaced mode-changing jobs.
- Public-safe SPF fixtures for parser regression tests.
- Health checks include next-step remediation hints.

## Next Best Things

### 1. Dashboard Data Contract And JSON Export

Why: dashboard values now matter operationally, so the metric extraction layer
should be explicit and reusable.

- Expand `dashboard.json` into a stable external contract with semantic version
  notes and documented fields.
- Add dashboard source-path tooltips for ambiguous values.
- Add reconciliation checks when PV/grid/load/battery totals do not add up.

### 2. Service Status And Diagnostic Bundle

Why: when something looks wrong on the VPS, the next step should be one command.

- Add more systemd detail: enabled/disabled and recent restart count.

### 3. Public-Safe Fixture Library

Why: Growatt returns duplicate and inconsistent fields. Fixtures make parser
fixes faster and safer.

- Add more fixture variants:
  - missing grid import live power
  - unavailable SOC/output source paths
- Cover PVOutput extraction, status summary, and Discord dashboard embed parsing
  with the fixtures.
- Use `redact-probe` when turning raw probe JSON into public-safe fixtures.

### 4. Health Check Remediation Mode

Why: `health-check` should not only say WARN/FAIL; it should tell you exactly
what to do next.

- Include next scheduled runs and cron job count.
- Include `DRY_RUN`, pause/topup state, dashboard freshness, PVOutput freshness,
  and service states.
- For each failed check, include one suggested command.
- Add `--discord`/`--notify` output that stays compact enough for mobile.

### 5. Forecast And Load Planner V2

Why: auto-topup is now useful, but the most valuable next step is explaining and
improving decisions, not adding more writes.

- Use solar-relevant weather fields where available: direct radiation, sunshine
  duration, cloud cover, and precipitation.
- Compare forecast with recent PVOutput/Growatt production to learn whether a
  "sunny" forecast actually produced useful energy.
- Add a dashboard card: "Tonight's risk" with expected sunrise SOC and reason.
- Add weekly recommendation text for `AUTO_TOPUP_TARGET_SOC`,
  `BATTERY_CHARGE_RATE_W`, and the topup margin.

### 6. Notification Quiet Hours And Digesting

Why: Discord is useful, but too many routine messages make the important ones
easier to miss.

- Add quiet hours for success/skip messages.
- Always allow critical failure, emergency battery, cloud lock, and watchdog
  failure alerts.
- Optionally batch routine overnight auto-topup events into a morning digest.
- Add per-event toggles for noisy events.

### 7. Safer Discord Control Audit Trail

Why: Discord can write to the inverter, so every write should be easy to trace.

- Add a dedicated audit row for every Discord-triggered command with user ID
  hash, command, duration, reason, and result.
- Add optional reason text to `/growatt_utility`, `/growatt_sbu`, and
  `/growatt_topup`.
- Add a read-only `/growatt_plan` command that shows what automation would do
  next without writing.
- Add a second confirmation path only for longer or riskier manual Utility
  holds.

### 8. Dashboard Mobile Polish

Why: the dashboard is likely checked from a phone first.

- Make the first viewport prioritize SOC, mode, live PV/load/grid, runtime, and
  dashboard freshness.
- Add compact cards for today's schedule and active topup/pause state.
- Add a manual action copy panel with safe copy-paste commands.
- Keep live write buttons out of the public dashboard until auth/CSRF and audit
  are designed properly.

### 9. Packaging And Developer Experience

Why: the repo is now large enough to benefit from standard Python project shape.

- Add `pyproject.toml` and dependency metadata.
- Add console script entry point, for example `growatt-guard`.
- Keep `growatt_power_guard.py` as a backwards-compatible shim.
- Add optional pre-commit checks for whitespace, Python compile, secret-looking
  values, and schedule JSON validation.

### 10. Backup, Restore, And Calendar Export

Why: local state now matters and outage schedules change.

- Add backup/restore for pause state, alert state, topup state, schedule
  overrides, dashboard metrics, and audit logs.
- Keep generated backups out of Git.
- Generate `.ics` calendar events for outage windows and mode-changing jobs.
- Add schedule lint warnings for jobs that are too close together.

## Area Backlog

### Safety And Reliability

- Add local API call accounting and warnings when a schedule would increase
  Growatt polling too much.
- Add stricter idempotency audit rows for skipped mode writes.
- Add emergency SBU-return guard below a configurable SOC, with an explicit
  override for outage realities.
- Add state migration/versioning for local JSON state files.

### Dashboard And Observability

- Add `dashboard.json`.
- Add metric source/source-path display for ambiguous values.
- Add daily import/export/load/PV reconciliation warnings when numbers do not
  add up.
- Add "last successful Growatt read" and "last successful PVOutput upload"
  timestamps to the top of the dashboard.
- Add export/download for local dashboard metric history.

### Forecasting And Optimization

- Learn typical overnight load by weekday/weekend.
- Learn effective charge rate from completed topups and suggest config updates.
- Compare forecasted solar against actual PV generation.
- Suggest threshold adjustments only after several days of evidence.

### Scheduling

- Add `.ics` calendar export.
- Add dry-run cron installer output showing exact crontab changes.
- Add schedule lint warnings for overlapping jobs and too-frequent writes.
- Add outage notice parser that proposes schedule overrides but requires manual
  confirmation.

### Notifications

- Add quiet hours.
- Add morning digest mode.
- Add notification channel abstraction after Discord behavior is stable.
- Add webhook rotation helper/checklist.

### Security And Public Repo Hygiene

- Add local secret scanning command before release/push.
- Keep docs free of domains, IPs, coordinates, usernames, serials, and webhooks.
- Add public-safe sample `.env` validation.
- Document dashboard exposure tradeoffs clearly.

## Later / Bigger Ideas

- Authenticated web app for controls, after dashboard JSON, audit trails, and
  auth design are in place.
- Multi-inverter support with per-device schedules and thresholds.
- Local-first inverter integration if the WiFi dongle or another local path can
  be proven reliable.
- PVOutput comparison dashboard for forecast vs actual production.
- Import estate outage notices from text/images into proposed overrides.

## Good First Issues

1. Add `dashboard.json` generation from the existing dashboard payload.
2. Add public-safe fixture files and one parser test per fixture.
3. Add `service-status` read-only command.
4. Add config-loading tests for `.env` defaults and invalid values.
5. Add quiet-hours config and tests for non-critical Discord messages.
6. Add schedule lint warnings for overlapping jobs.
7. Add `pyproject.toml` while preserving the existing script entry point.
