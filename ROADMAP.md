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
  risk planning, same-time energy insights, documented JSON export, metric
  source paths, and system/automation cards.
- Shared observability refresh: one Growatt read updates the dashboard and
  PVOutput, avoiding duplicate pollers.
- PVOutput upload with fallback when extended fields are rejected.
- Schedule overrides, named outage profiles, dry-plan preview, and safer update
  script gates.
- Unit tests split by module, including config-loading coverage, and a public-repo hygiene guide.
- Read-only `service-status`, `deployment-preflight`, and `diagnostic-bundle`
  commands for VPS support, including local state and systemd detail.
- JSON output for service status, deployment preflight, diagnostic bundles, ops
  review, and schedule previews.
- Redacted PV metric probing for Growatt field-shape debugging.
- Schedule lint warnings for duplicate PVOutput pollers, duplicate read cron,
  fast polling, interval mode-changing jobs, and tightly spaced mode-changing
  jobs.
- Public-safe SPF fixtures cover parser, PVOutput, status-summary, and
  Discord dashboard embed regression tests.
- Package metadata exposes an optional `growatt-guard` console script while
  preserving `growatt_power_guard.py` as the compatibility shim.
- `verify_local.sh` runs compile, quiet tests, schedule validation, whitespace
  checks, and the public hygiene check before commits or pushes.
- Health checks include next-step remediation hints.

## Next Best Things

### 1. Dashboard Data Contract

Why: dashboard values now matter operationally, so the metric extraction layer
should be explicit and reusable.

- Add dashboard source-path tooltips for ambiguous values.
- Keep daily PV/grid/load/battery reconciliation warnings visible in
  `quality.data.reconciliation`.
- Add a schema-version field to `dashboard.json` if an external consumer needs
  strict migration handling.

### 2. Public-Safe Fixture Library

Why: Growatt returns duplicate and inconsistent fields. Fixtures make parser
fixes faster and safer.

- Add more fixture variants when new Growatt payload shapes are observed.
- Use `redact-probe` when turning raw probe JSON into public-safe fixtures.
- Keep parser, PVOutput, status-summary, and Discord dashboard embed coverage
  tied to the fixture set.

### 3. Health Check Remediation Mode

Why: `health-check` should not only say WARN/FAIL; it should tell you exactly
what to do next.

- Keep next scheduled runs and cron job count visible in health/service checks.
- Include `DRY_RUN`, pause/topup state, dashboard freshness, PVOutput freshness,
  and service states.
- For each failed check, include one suggested command.
- Add `--discord`/`--notify` output that stays compact enough for mobile.

### 4. Forecast And Load Planner V2

Why: auto-topup is now useful, but the most valuable next step is explaining and
improving decisions, not adding more writes.

- Use solar-relevant weather fields where available: direct radiation, sunshine
  duration, cloud cover, and precipitation.
- Compare forecast with recent PVOutput/Growatt production to learn whether a
  "sunny" forecast actually produced useful energy.
- Add a dashboard card: "Tonight's risk" with expected sunrise SOC and reason.
- Add weekly recommendation text for `AUTO_TOPUP_TARGET_SOC`,
  `BATTERY_CHARGE_RATE_W`, and the topup margin.

### 5. Notification Quiet Hours And Digesting

Why: Discord is useful, but too many routine messages make the important ones
easier to miss.

- Add quiet hours for success/skip messages.
- Always allow critical failure, emergency battery, cloud lock, and watchdog
  failure alerts.
- Optionally batch routine overnight auto-topup events into a morning digest.
- Add per-event toggles for noisy events.

### 6. Safer Discord Control Audit Trail

Why: Discord can write to the inverter, so every write should be easy to trace.

- Add a dedicated audit row for every Discord-triggered command with user ID
  hash, command, duration, reason, and result.
- Add optional reason text to `/growatt_utility`, `/growatt_sbu`, and
  `/growatt_topup`.
- Add a read-only `/growatt_plan` command that shows what automation would do
  next without writing.
- Add a second confirmation path only for longer or riskier manual Utility
  holds.

### 7. Dashboard Mobile Polish

Why: the dashboard is likely checked from a phone first.

- Make the first viewport prioritize SOC, mode, live PV/load/grid, runtime, and
  dashboard freshness.
- Add compact cards for today's schedule and active topup/pause state.
- Add a manual action copy panel with safe copy-paste commands.
- Keep live write buttons out of the public dashboard until auth/CSRF and audit
  are designed properly.

### 8. Packaging And Developer Experience

Why: the repo is now large enough to benefit from standard Python project shape.

- Keep `pyproject.toml`, `requirements.txt`, and the `growatt-guard` console
  script aligned.
- Keep `growatt_power_guard.py` as a backwards-compatible shim.
- Extend `verify_local.sh` if new mandatory local checks are added.

### 9. Backup, Restore, And Calendar Export

Why: local state now matters and outage schedules change.

- Add backup/restore for pause state, alert state, topup state, schedule
  overrides, dashboard metrics, and audit logs.
- [x] Keep generated backups out of Git.
- Keep `schedule-calendar` useful for outage windows and mode-changing jobs.
- [x] Add schedule lint warnings for jobs that are too close together.

## Area Backlog

### Safety And Reliability

- [x] Add local API call accounting and warnings when a schedule would increase
  Growatt polling too much.
- Add stricter idempotency audit rows for skipped mode writes.
- Add emergency SBU-return guard below a configurable SOC, with an explicit
  override for outage realities.
- [x] Add state migration/versioning for local JSON state files.

### Dashboard And Observability

- Add metric source/source-path display for ambiguous values.
- Extend daily reconciliation if new energy counters become available.
- Keep explicit Growatt read and PVOutput upload timestamps available in
  dashboard `freshness`.
- Add export/download for local dashboard metric history.

### Forecasting And Optimization

- Learn typical overnight load by weekday/weekend.
- Learn effective charge rate from completed topups and suggest config updates.
- Compare forecasted solar against actual PV generation.
- Suggest threshold adjustments only after several days of evidence.

### Scheduling

- Extend `.ics` calendar export if new schedule semantics are added.
- [x] Add dry-run cron installer output showing exact crontab changes.
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
- [x] Add public-safe sample `.env` validation.
- [x] Document dashboard exposure tradeoffs clearly.

## Later / Bigger Ideas

- Authenticated web app for controls, after dashboard JSON, audit trails, and
  auth design are in place.
- Multi-inverter support with per-device schedules and thresholds.
- Local-first inverter integration if the WiFi dongle or another local path can
  be proven reliable.
- PVOutput comparison dashboard for forecast vs actual production.
- Import estate outage notices from text/images into proposed overrides.

## Good First Issues

1. Add quiet-hours config and tests for non-critical Discord messages.
2. Add public hygiene checks for new sensitive config keys.
3. Add public-safe fixture files when new Growatt field shapes are observed.
