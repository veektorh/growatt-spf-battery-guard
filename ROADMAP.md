# Growatt Guard Roadmap

Last reviewed: 2026-11-10

This is the working backlog for Growatt Guard. Priorities favor inverter safety,
low Growatt API pressure, trustworthy decisions, and clear operations before
convenience or larger product ideas.

## Next Up

This is the handoff queue: concrete tasks any agent can pick up without the
conversation that produced them. `AGENTS.md` explains how to use it. Each task
carries its evidence, the steps, and what "done" means. Remove a task when it
is finished and record anything it uncovered as a new task. Keep entries
public-safe: no IDs, serials, domains, or credentials.

### N1. Check the first nights on the learned charge rate

- Status: ready from the morning of 2026-11-11. Read-only.
- Why: since 2026-11-10 `auto-topup-check` sizes top-ups with the learned
  charge rate (about 1500 W against 1950 W configured) and a top-up that ends
  within one SOC point of target is recorded as `topup-target-reached`. Neither
  has been observed in production yet.
- Steps, on the VPS runtime checkout:
  - In `logs/mode_decisions.csv`, read the night's `auto-topup-started` and
    `topup-complete-check` rows. The start note should show
    `planned_charge_rate_w` near the learned rate, not 1950.
  - In `logs/growatt_power_guard.log`, confirm the line
    `Planning with charge rate ... (learned from N top-ups)`.
  - Count top-ups per night and compare with the baseline below.
- Baseline: 7 of the 14 nights before the change had more than one auto top-up
  (one had four). Evening top-ups gained about 1200 W; late-night ones about
  1800 W.
- Progress, 2026-11-11 (1 of 3 nights reviewed, night of 2026-11-10):
  - Learned rate is in use. The log shows `Planning with charge rate 1513 W
    (learned from 10 top-ups); configured 1950 W`, and the three starts audited
    `planned_charge_rate_w` of 1512.7, 1556.5 and 1545.3 instead of 1950.
  - The near-target rule is not exercised yet. Both completions ended at or
    above target (61 against 60.64, 50 against 49.46), which the old check
    accepted too. The new rule only changes an expiry, and the one expiry ended
    6 points short (58 against 63.97). Keep watching for an expiry that lands
    within one point below target.
  - Repeat top-ups did not fall yet. The night had three starts (18:00 for
    210 min, 21:40 for 31 min, 02:40 for 52 min) against three on 2026-11-09,
    and planned Utility time rose from 127 to 293 minutes.
  - The first repeat is a window problem, not a rate problem: the 18:00
    reserve plan wanted about 274 minutes but was clamped to 210, while the
    recorded 63.97% completion target is unreachable in 210 minutes at any
    plausible rate (53.7% at the planned 1512.7 W, 58.8% even at the configured
    1950 W, 58% at the realized 1877 W). See N8.
  - Count only `auto-topup-started` rows. A morning preserve Utility hold also
    closes through `topup-complete-check` and appears as a `topup-target-reached`
    row (07:36 today, `ownership=owned`, target 45%); it is not a top-up start.
  - Baseline correction: "7 of 14 nights with more than one top-up" reproduces
    only for 2026-10-21 to 2026-11-03. The 14 nights ending 2026-11-09 have 4,
    and the two nights before the change had 3 each. Judge the new nights
    against the 2026-11-09 window.
- Remaining: two more nights, then write the verdict and close this task.
- Done when: three nights are reviewed and the result is written here as either
  "fewer repeat top-ups" or a follow-up task with the numbers.

### N2. Plan the night with an evening load and an overnight load

- Status: needs a short design before code. Changes inverter mode timing.
- Why: the planner uses one flat learned load (about 1450 W). On 2026-11-09
  every live reading was higher: about 2400 to 2700 W at 19:00 to 20:30, about
  1800 W at 21:00 to 22:30, and 1500 to 1650 W after midnight. The shortfall
  therefore regrows by about two minutes per 20-minute check, and a new top-up
  starts each time it crosses `AUTO_TOPUP_MIN_MINUTES`.
- Steps: decide how discharge history is stored per time band (it is one
  nightly aggregate today, see `append_discharge_rate_reading` and
  `select_overnight_load`), then make `command_auto_topup_check` project the
  drain as evening hours times evening load plus overnight hours times
  overnight load.
- Done when: a replay of the 2026-11-09 night in a test plans one evening
  top-up instead of two, and N1-style checks confirm it in production.

### N3. Use one sunrise target through the night

- Status: blocked on N1 evidence (see the three-comparable-outcome gate in P1).
- Why: with `AUTO_TOPUP_TARGET_SOC` unset the reserve plan aims to reach
  sunrise at the BMS cutoff (20%). Inside the last four hours the late-safety
  plan raises that to `AUTO_TOPUP_SUNRISE_FLOOR_SOC` (35%), which starts a 60 to
  80 minute top-up at about 02:40. At 06:30 the morning preserve then wants its
  threshold (45% on a rainy day) for the 08:00 outage and only reached 43% on
  2026-11-10.
- Options: set `AUTO_TOPUP_TARGET_SOC=35` (config only; moves charging earlier
  into the evening, when the measured charge rate is lower), or derive the
  target from the next morning's preserve threshold on outage days.
- Done when: one option is chosen with the owner, trialled, and reviewed.

### N4. Recheck the rainy-day PV forecast factor

- Status: ready. Read-only analysis first.
- Why: the learned rainy factor is 84%, but recent rainy days came in far lower
  (for example 13.4 kWh actual against 31.8 forecast, and 20.6 against 27.9).
  The forecast feeds the "tomorrow can run leaner" recommendation and the sunny
  top-up skip.
- Steps: in `growatt_guard/forecast_calibration.py`, compare the all-history
  ratio with a recent-window ratio (last 7 to 14 completed rainy days) and
  report both before changing the factor.
- Done when: the comparison is documented and either the weighting changes with
  tests or the task is closed with the reason.

### N5. Confirm the preserve-window deferral in production

- Status: waiting for a natural occurrence. Read-only.
- Why: PR #54 keeps the Utility hold when a time-boxed top-up ends inside a
  preserve window with SOC under the threshold. No such top-up has ended in
  production since it deployed on 2026-11-10.
- Done when: an audit row with action `topup-complete-deferred` is seen and the
  following scheduled `return-sbu` returned to SBU as planned.

### N6. Finish the dashboard clean-up

- Status: ready. No inverter writes.
- Why: the four-tab dashboard shipped on 2026-11-10 but `build_dashboard_html`
  is still one function of about 950 lines that reads the audit log and state
  files itself, so `dashboard.json` lacks automation history, recent decisions,
  upcoming overrides, and runtime.
- Steps, each a separate small PR:
  - Move those reads into `dashboard_viewmodel.py` so the HTML renders only
    from the payload.
  - Unify the two top-up estimates: the "Topup to Sunrise" row can show a dash
    while the Top-up Tonight tile shows minutes.
  - Fix the grid label: "Solar covering entire load" appears whenever grid
    import is under 20 W, including at night on battery.
  - With live data, look once at the Automation tab history chart and decisions
    table, and at an alert row on the Now tab when one applies.

### N7. Small operational hygiene

- Stray dry-run audit rows: pairs of `return-sbu` and `watchdog-sbu` rows with
  `dry_run=true` and SOC 70 appear in the production `mode_decisions.csv`, one
  pair at a deploy time. Find what writes them and keep it out of the
  production audit log.
- Logs print the plant ID and device serial at INFO. Mask them.
- The Discord bot logs "voice will NOT be supported" warnings on every start.
- GitHub has announced that `ubuntu-latest` moves to Ubuntu 26; pin or test.

### N8. Do not clamp a reserve top-up below its survival window

- Status: ready. Changes inverter mode timing.
- Why: found while reviewing N1. `command_auto_topup_check` sizes the window
  from the horizon maths and then clamps it with
  `topup_min = min(topup_min, config.discord_topup_max_minutes)`. The completion
  target is then derived from the clamped window as the SOC that still preserves
  the reserve across the remaining hours, but nothing checks that the clamped
  window can deliver that much charge. On the night of 2026-11-10 the survival
  maths wanted about 274 minutes (load 1455 W, learned charge rate 1512.7 W,
  12.6 hours to sunrise), it ran 210 minutes, and it recorded a 63.97%
  completion target. That target needs about 8400 Wh of battery gain, or 333
  minutes at the planned rate; 210 minutes cannot deliver it at the planned
  rate (53.7%), the configured 1950 W (58.8%) or the realized 1877 W (58%). It
  expired 6 points short and the next check started a second window, so the
  repeat is structural.
- Options: give the scheduled planner its own bound and keep the Discord cap
  for manual `/topup`; or, when the cap binds, record a reachable target and
  plan the remainder as a second window; or split the requirement into two
  planned windows from the start.
- Done when: a replay of the 2026-11-10 evening either plans a window that
  reaches its recorded target or records a target it can reach, and N1 shows
  one fewer repeat top-up.

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
- PVOutput live uploads, extended-field fallback, complete weekly-history
  parsing, quota-reserved one-time backfill, and coalesced incident/recovery
  alerts.
- An append-only daily-generation ledger seeded from verified Growatt history
  and advanced from completed late-evening observability days.
- Forecast-versus-actual calibration with evidence thresholds before tuning
  recommendations.
- Schedule overrides, outage profiles, dry-plan preview, calendar export, and
  schedule lint for duplicate, fast, or tightly spaced jobs.
- Selective backup/restore with strict validation for active Utility holds and
  durable daily-generation history.
- Read-only service status, deployment preflight, ops review, schedule preview,
  and diagnostic bundle commands with JSON output.
- Atomic packaged production releases with pinned verification, health checks,
  rollback, and wait-for-clear deployment support.
- Public-safe fixtures, probe redaction, secret scanning, public environment
  validation, and repository hygiene checks.
- A backwards-compatible `growatt_power_guard.py` shim, packaged
  `growatt-guard` entry point, split modules, and the `verify_local.sh` gate.
- A read-only top-up outcome scorecard that pairs starts and closures, compares
  planned and actual duration/SOC/load/grid evidence, classifies closure causes,
  and withholds tuning recommendations until three comparable outcomes exist.
- A separate morning-preservation scorecard that models the observed no-Utility
  SOC path, fails closed on incomplete metric windows, and gates the opt-in
  hourly-irradiance solar bridge behind three comparable post-change shadow
  outcomes.

## Priority Queue

Work from the top unless live operational evidence justifies reordering.

### P1. Top-up Decision Quality And Reporting Trust

Goal: make every reserve decision and weekly recommendation explainable before
adding more inverter writes.

The outcome scorecard foundation is implemented in `ops-review`. New top-ups
record structured plan evidence, while historical rows remain supported with
explicitly incomplete classifications. Continue dogfooding it against real
closures and add fixtures for any new trustworthy classification signal.

- Preserve the implemented target-reached, near-target-expiry, insufficient
  Utility/charge, load-overrun, and unknown closure classifications.
- Preserve planned-versus-actual duration, SOC gain, load, implied charge rate,
  and measured grid-import comparisons with metric coverage checks.
- Keep expected safety holds separate from inefficient charging; do not label
  charging avoidable without counterfactual evidence.
- Preserve the morning counterfactual floor/recovery evidence and keep the
  solar bridge disabled unless complete repeated outcomes justify a trial.
- Keep the three-comparable-outcome gate before recommending changes to
  `AUTO_TOPUP_TARGET_SOC`, charge rate, reserve margin, or thresholds.
- Make weekly solar and topup comparisons fail closed when their data windows
  are incomplete.
- Add regression fixtures whenever a production report exposes a new data-shape
  or interpretation bug.

Current concrete steps toward this goal are in Next Up: N1 (review the first
nights on the learned charge rate), N2 (plan with an evening load and an
overnight load), and N3 (one sunrise target through the night). The learned
charge rate went live on 2026-11-10 at the owner's direction, ahead of the
three-comparable-outcome gate; N1 collects those outcomes after the fact, and
N3 stays behind the gate.

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

Shipped on 2026-11-10: the dashboard is one layout with four tabs (Now, Energy,
Automation, System). The Now tab is the focused first viewport: live solar,
load, grid import, and battery; SOC and runtime; tonight's top-up need; power,
SOC, and 7-day supply charts; and alert rows for bypass, an active hold, stale
data, and skipped automation that render only when they apply. Today's schedule
is a compact table on the Automation tab. The follow-up clean-up is N6 in Next
Up.

- Preserve that first viewport: new detail belongs on the Energy, Automation,
  or System tab unless it answers what the Now tab is for.
- Add download/export for local metric history.
- Make ambiguous metric source paths easier to inspect next to the numbers they
  explain; they are a single drawer on the System tab today.
- Add safe copy-command panels.
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
