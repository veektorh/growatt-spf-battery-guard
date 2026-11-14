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

### N2. Plan the night with an evening load and an overnight load

- Status: bands and headroom shipped 2026-11-12. Waiting on three nights of
  observation. Changes inverter mode timing.
- Why: the planner uses one flat learned load (about 1450 W). On 2026-11-09
  every live reading was higher: about 2400 to 2700 W at 19:00 to 20:30, about
  1800 W at 21:00 to 22:30, and 1500 to 1650 W after midnight. The shortfall
  therefore regrows by about two minutes per 20-minute check, and a new top-up
  starts each time it crosses `AUTO_TOPUP_MIN_MINUTES`. On 2026-11-11 the same
  regrowth split a high-SOC night into three reserve top-ups plus the
  late-safety window; N1 has the numbers.
- Baseline, three nights on the learned charge rate (N1, closed 2026-11-13
  with the verdict "not fewer repeat top-ups"):

  | Night | Starts | Utility minutes | Planned vs implied rate |
  |---|---|---|---|
  | 11-10 | 3 | 295 | 1513-1556 W planned |
  | 11-11 | 4 | 146 | 1571-1741 planned, 1637-2405 implied |
  | 11-12 | 3 | 202 | 1887/1846/1847 planned, 1385/1888/1732 implied |
  | 11-13 | 2 | 165 | 1851/1856 planned, 2000/2160 implied (after N1 closed) |

  The near-target rule is exercised and correct (completions at 80 against
  80.62, 60 against 60.15, 53 against 53.08, 44 against 44.03, 47 against
  47.14). Rate accuracy is the open weakness: the same learned value ran 27%
  high on one evening window and 6% low on another.
- Decisions taken 2026-11-12: two bands (evening 18:00-22:00, overnight
  22:00-06:00), banded values stored on the existing nightly row, a fixed
  three-point SOC headroom, and bands before headroom.
- Steps: decide how discharge history is stored per time band (it is one
  nightly aggregate today, see `append_discharge_rate_reading` and
  `select_overnight_load`), then make `command_auto_topup_check` project the
  drain as evening hours times evening load plus overnight hours times
  overnight load.
- Progress, 2026-11-12:
  - One row per night now carries `bands` with a running evening and overnight
    rate, and after-midnight samples join the night that started the evening
    before, so the night also keeps the day type of its evening.
  - `select_night_load_profile` returns the horizon-weighted band load, which is
    energy-exact, and falls back to the flat nightly average whenever a band the
    horizon still needs has no history. The planner and the dashboard risk tile
    both use it, so one load feeds planning, the tile and the audit.
  - The replay does not plan one evening top-up instead of two, and that is the
    honest result rather than a test to bend. On the 2026-11-09 night the flat
    1448 W plan asked 27 minutes, which reproduces the production 27-minute
    window; the banded 1807 W plan asks 89 minutes. Charging at 1950 W against
    a heavier evening band still leaves a genuine deficit by 22:40, and the
    three-point headroom adds about 14 minutes to the first window without
    removing the second.
  - The SOC path implies the evening load is not one number: +2 points in the
    19:20-19:50 window means about 750 W of load there, while the drain from
    22:00 to 23:20 implies about 1700 W. Judge the repeat count over real nights
    after the headroom change, and revisit the band count only if the recorded
    bands keep missing.
- First headroom night, 2026-11-12 18:20 (reserve plan, SOC 78%):
  - The window was 25 minutes to a completion target of 80.62%, against the 8
    minutes the same maths gave for the old 20% sunrise target. It closed at
    18:46 with `topup-target-reached`, `end_soc=80`, `actual_min=26`.
  - The realized rate was 1385 W against the planned 1887.5 W, so it landed
    half a point under target and only the near-target rule recorded it as a
    completion. That is the same charge-rate noise N1 is measuring, now in the
    other direction (2026-11-11 came in above the plan).
  - The load was still the flat 1463.91 W; the bands need three matching nights
    before they replace it. What this night has to show is whether the next
    checks stay skipped for longer than the 2026-11-11 sawtooth, which re-armed
    about two hours later.
- Progress, headroom: `AUTO_TOPUP_HEADROOM_SOC` (default 3). The first
  implementation raised the *target* used for both the decision and the sizing,
  so the plan still landed exactly on the raised target and the re-arm interval
  was unchanged — the 19:04 payload showed `projected 23.0` against `target 23.0`
  with `topup 0.2min`, which is the sawtooth's signature. The decision now uses
  the policy target (20, or the late-safety floor) and only the *window* is sized
  to target plus headroom, so a completion leaves the projection three points
  above the target. The start band becomes target minus the 20-minute minimum to
  target plus headroom (about 6.7 points of drain instead of 3.7), roughly
  doubling the interval between windows. The risk tile reports both
  `target_soc` and `plan_target_soc`.
- Night of 2026-11-12 (first with bands and headroom, 1 of 3):
  - Three starts: 18:20 for 25 min, 22:00 for 123 min, 02:40 for 51 min, so
    202 Utility minutes against 295 on 2026-11-10 and 146 on 2026-11-11.
  - Every completion landed at or within a point of target (80/80.62, 72/71.52,
    60/60.15), and the gaps between windows were 3 h 14 m and 2 h 36 m against
    the roughly two hours the 2026-11-11 sawtooth re-armed in.
  - The bands went live mid-night, which exposed a gate bug: readiness counted
    matching *nights* rather than nights that carry `bands`, so one banded night
    replaced the flat average. Fixed on 2026-11-13; the source string now names
    the per-band night count.
  - The banded overnight rate came out near 2100 W against a flat fallback of
    1463 W, and the observed drain (72% to 55% in 2 h 36 m on 30 kWh) agrees
    with the banded figure. The stale flat average was fixed by #83 and #84,
    which read the fallback from the four most recent nights.
- Night of 2026-11-13 (2 of 3; still planned on the flat fallback):
  - Two starts, the first night under three: 18:00 for 90 min (reserve, 82% to
    92% against a 91.76% target, `topup-target-reached`) and 02:40 for 75 min
    (late-safety, 41% to 50% against 49.71%). 165 Utility minutes.
  - The gap between windows was 7 h 10 m, against 3 h 14 m and 2 h 36 m on
    2026-11-12 and about two hours on 2026-11-11. The headroom change is doing
    its job: the evening window no longer re-arms through the night.
  - `planned_load_w` was 1878 W in the evening (the four-night fallback) and
    1414.5 W at 02:40, while the night's recorded bands were 1796 W evening and
    2240 W overnight. Both windows still reached target because the charge rate
    came in high (implied 2000 W and 2160 W against about 1850 W planned).
  - The 05:20 check skipped a 4.4-minute top-up at 40%.
- When the bands can go live: readiness counts banded nights of the *same day
  type*, and `day_type` treats Saturday and Sunday as weekend. Two weekday nights
  carry `bands` (2026-11-12 and 11-13). Each check records its sample before it
  plans, so the night of 2026-11-16 becomes the third banded night as it runs:
  its evening band counts from 18:00, but a horizon before 22:00 also needs the
  overnight band, which reaches three nights only with the first sample after
  22:00. The first banded plan can therefore come at the 22:00 check on
  2026-11-16, and the evening of 2026-11-17 is the first whole night on bands.
  Weekend nights have no banded rows yet. They need three (the nights of 11-14,
  11-15 and 11-21), so the earliest weekend banded plan is after 22:00 on
  2026-11-21.
- Next reads, from the night of 2026-11-13 onward:
  - `auto-topup-started` count per night, against the 2026-11-09 window's three.
  - The gap between a `topup-complete-check` and the next start. The corrected
    sizing should land about three points above the policy target and stretch
    the re-arm interval towards two hours instead of the 2026-11-11 one.
  - `planned_load_w` on each start: it stays the flat average until three
    matching nights carry `bands`, so a banded value is the signal that the
    evening/overnight split is live. `state/discharge_rate_history.json` shows
    whether the newest rows have `bands`.
- Done when: banded planning is in use in production (audit `planned_load_w`
  reflects the horizon-weighted load), the headroom change is in, and N1-style
  checks over three nights show fewer repeat top-ups than the 2026-11-09
  baseline.

### N12. Review a week of mornings with the outage-start preserve

- Status: waiting on five outage-day mornings, from 2026-11-14. Read-only. A
  change it leads to alters inverter mode timing and needs the owner's go-ahead.
- Why: N3 closed on 2026-11-14 with the owner's agreement that #85 is the fix,
  and that neither `AUTO_TOPUP_TARGET_SOC=35` nor a target taken from the next
  morning's preserve threshold is needed yet. #85 makes `preserve-battery`
  decide on `projected_soc_at_outage_start` instead of the live reading, and
  adds the 07:00 `morning-preserve-late` check with a 55-minute hold before the
  07:55 return. The case it targets: on 2026-11-13 the 02:40 late-safety window
  ended at 60%, the 06:30 preserve skipped at 41% against a 40% threshold, and
  the outage began near 39%.
- Read per morning: the 06:30 and 07:00 preserve rows (`switch` or `skip`, with
  live and projected SOC), the SOC at the 07:55 return, and the SOC when the
  outage starts. Compare that SOC with the day's preserve threshold.
- Done when: five outage-day mornings are read. If the outage still starts below
  the threshold on two or more of them, reopen the sunrise-target options
  (`AUTO_TOPUP_TARGET_SOC=35`, or a target from the next morning's preserve
  threshold) as a new task; otherwise record the result and remove this one.

### N5. Confirm the preserve-window deferral in production

- Status: waiting for a natural occurrence. Read-only.
- Why: PR #54 keeps the Utility hold when a time-boxed top-up ends inside a
  preserve window with SOC under the threshold. No such top-up has ended in
  production since it deployed on 2026-11-10.
- Done when: an audit row with action `topup-complete-deferred` is seen and the
  following scheduled `return-sbu` returned to SBU as planned.

### N11. See a live alert row on the dashboard Now tab

- Status: waiting for a natural occurrence. Read-only.
- Why: N6 closed on 2026-11-14 with the renderer reading only the
  `dashboard.json` payload. The Automation tab history chart and decisions table
  were confirmed against live data on 2026-11-12, but no alert row (top-up
  running, Tonight Safe, cloud streak, pause, bypass) has been seen on the
  rebuilt Now tab yet.
- Done when: one alert row is seen on the live Now tab and its text matches the
  matching `dashboard.json` field (`automation.*` or `planner.tonight_safe`).

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
  recommendations. The rainy/cloudy factor learns from the last 14 completed
  rainy days and reports the all-history ratio beside it: on 2026-11-12 the
  45-day pooled ratio was 0.83 while the last 11 days pooled 0.70, so the
  long window was hiding a seasonal drop that the 14-day window follows.
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

Current concrete steps toward this goal are in Next Up: N2 (plan with an evening
load and an overnight load, now observing in production) and N12 (a week of
mornings with the outage-start preserve, which replaced N3). The learned charge rate went live on 2026-11-10 at
the owner's direction, ahead of the three-comparable-outcome gate; N1 collected
those outcomes after the fact and closed on 2026-11-13 with the repeats still
present, which is why N2 owns them now. A sunrise-target change returns only
if N12 shows outages still starting below the preserve threshold.

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

Shipped on 2026-11-12: the Energy tab has a month-by-month view. "This Month
by Day" stacks solar and Utility per day with load tick marks, and "Month by
Month" lists solar, load and Utility totals with a day count. Solar history
reaches back to 2026-03-14 from the daily ledger; load and Utility totals start
on 2026-11-12, the day the ledger began recording those counters, because
Growatt exposes only today's values and no history endpoint returns them.

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
