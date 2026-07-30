# Growatt Guard Roadmap

Last reviewed: 2026-07-30

This is the working backlog for Growatt Guard. Priorities favor inverter safety,
low Growatt API pressure, trustworthy decisions, and clear operations before
convenience or larger product ideas.

## Current Baseline

The following capabilities are implemented and should be preserved:

- Cloud-safe morning and afternoon preservation, verified SBU returns,
  watchdog checks, health reporting, and conditional night auto-topup.
- Sunrise planning using learned weekday/weekend load, observed charge rates,
  weather calibration, minimum topup duration, and SOC safety floors.
- Guard-owned Utility holds with restart-safe completion, orphan recovery,
  command locking, deployment safety gates, and audited low-SOC overrides.
- Growatt session reuse, refresh locking, login cooldown handling, API-pressure
  linting, and cloud failure/recovery alerts.
- Confirmed mode writes that re-read inverter state after Utility/SBU changes.
- Discord embeds and a private allowlisted control bot with canonical topup
  status shared across CLI, Discord, dashboard, health, and service diagnostics.
- A mobile-oriented dashboard with live power flow, daily energy, metric source
  paths, freshness, charts, schedule visibility, tonight-risk planning,
  reconciliation, same-time insights, and schema-versioned JSON output.
- One observability refresh path that updates the dashboard and PVOutput from a
  shared Growatt read.
- PVOutput live uploads, extended-field fallback, and complete weekly-history
  parsing.
- Forecast-versus-actual calibration with evidence thresholds before tuning
  recommendations.
- Schedule overrides, outage profiles, dry-plan preview, calendar export, and
  schedule lint for duplicate, fast, or tightly spaced jobs.
- Selective backup/restore with strict validation for active Utility holds.
- Read-only service status, deployment preflight, ops review, schedule preview,
  and diagnostic bundle commands with JSON output.
- Atomic packaged production releases with pinned verification, health checks,
  rollback, and wait-for-clear deployment support.
- Public-safe fixtures, probe redaction, secret scanning, public environment
  validation, and repository hygiene checks.
- A backwards-compatible `growatt_power_guard.py` shim, packaged
  `growatt-guard` entry point, split modules, and the `verify_local.sh` gate.

## Priority Queue

Work from the top unless live operational evidence justifies reordering.

### P1. Top-up Decision Quality And Reporting Trust

Goal: make every reserve decision and weekly recommendation explainable before
adding more inverter writes.

- Classify topup closures as target reached, near-target expiry, insufficient
  Utility/charge, load overrun, or unknown.
- Compare planned duration and target against observed SOC gain, learned charge
  rate, load, and actual completion.
- Separate expected safety holds from inefficient or avoidable grid charging in
  weekly reporting.
- Recommend changes to `AUTO_TOPUP_TARGET_SOC`, charge rate, reserve margin, or
  thresholds only after enough comparable observations.
- Make weekly solar and topup comparisons fail closed when their data windows
  are incomplete.
- Add regression fixtures whenever a production report exposes a new data-shape
  or interpretation bug.

### P2. Notification Quiet Hours And Digest

Goal: keep critical Discord alerts prominent without losing routine evidence.

- Add configurable quiet hours for success and skip notifications.
- Always allow emergency battery, cloud lock, command failure, and watchdog
  failure alerts.
- Add a morning digest for routine overnight topup starts, completions, skips,
  and recoveries.
- Add per-event toggles for noisy routine events.
- Keep health and summary embeds compact enough for mobile.

### P3. Discord Write Audit And Safe Planning

Goal: make every Discord-triggered inverter write attributable and reviewable.

- Record a dedicated audit row with a hashed user ID, command, reason, requested
  duration or target, result, and final ownership state.
- Add optional reason text to `/growatt_utility`, `/growatt_sbu`, and
  `/growatt_topup`.
- Add a read-only `/growatt_plan` command showing the next automation decision
  without writing.
- Require a second confirmation only for unusually long or risky manual Utility
  holds.

### P4. Dashboard History And Mobile Polish

Goal: improve investigation and phone use without turning the public dashboard
into a control surface.

- Add download/export for local metric history.
- Make ambiguous metric source paths easier to inspect from their cards.
- Keep the first viewport focused on SOC, mode, PV/load/grid, runtime, freshness,
  tonight risk, and active hold state.
- Add compact schedule and safe copy-command panels.
- Extend reconciliation only when reliable new energy counters are available.
- Keep live write controls out until authentication, CSRF protection, and audit
  behavior are designed.

### P5. Outage Planning Assistance

Goal: reduce manual schedule work while retaining human confirmation.

- Parse estate outage notices from text or images into proposed schedule
  overrides.
- Show the exact effective jobs and mode changes before applying a proposal.
- Require explicit confirmation before persisting any proposed override.
- Extend calendar export only when new schedule semantics require it.

### P6. Maintenance And Extensibility

Goal: preserve reliability as payloads, dependencies, and integrations evolve.

- Add public-safe fixtures when new Growatt response shapes are observed.
- Add stricter audit detail for skipped or already-satisfied mode writes.
- Keep package metadata, lock files, the compatibility shim, and
  `verify_local.sh` aligned.
- Add a webhook rotation checklist/helper.
- Introduce a notification-channel abstraction only after Discord quiet hours,
  digesting, and auditing are stable.

## Ongoing Guardrails

These are continuous constraints, not standalone backlog projects:

- Keep tests offline and all examples public-safe.
- Keep mode writes idempotent, verified, auditable, and protected by ownership.
- Do not increase Growatt polling frequency without schedule/API accounting.
- Prefer read-only explanation and evidence collection before new automation.
- Keep production changes on the verified PR and atomic deployment path.
- Treat fixture expansion, documentation accuracy, dependency maintenance, and
  secret hygiene as part of every relevant change.

## Later Ideas

These remain intentionally below the active queue:

- Authenticated web controls after dashboard auth, CSRF, and audit design.
- Multi-inverter support with per-device schedules and thresholds.
- Local-first inverter integration if a reliable supported path is proven.
- A deeper PVOutput comparison view for forecast versus actual production.

## Good First Issues

1. Add quiet-hours configuration and tests for non-critical notifications.
2. Add a semicolon-delimited multi-day PVOutput fixture to the public-safe
   fixture library.
3. Add metric-history download/export with offline tests.
