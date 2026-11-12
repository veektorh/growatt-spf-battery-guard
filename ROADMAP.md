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
    1950 W, 58% at the realized 1877 W). Fixed by sizing the scheduled window
    from the horizon: `DISCORD_TOPUP_MAX_MINUTES` now caps only a manual
    `/growatt_topup`, `AUTO_TOPUP_MAX_MINUTES` (default `0`) bounds the
    schedule, and a bound that binds records the SOC the window can reach.
  - Count only `auto-topup-started` rows. A morning preserve Utility hold also
    closes through `topup-complete-check` and appears as a `topup-target-reached`
    row (07:36 today, `ownership=owned`, target 45%); it is not a top-up start.
  - Baseline correction: "7 of 14 nights with more than one top-up" reproduces
    only for 2026-10-21 to 2026-11-03. The 14 nights ending 2026-11-09 have 4,
    and the two nights before the change had 3 each. Judge the new nights
    against the 2026-11-09 window.
- Progress, 2026-11-12 (2 of 3 nights reviewed, night of 2026-11-11):
  - First night under the horizon-sized window (the fix deployed 2026-11-11
    11:44 UTC). The 210-minute clamp is gone: plans ran 20, 22, 21 and 85
    minutes.
  - The learned rate held all night: `Planning with charge rate 1571 W` rising
    to `1772 W (learned from 10 top-ups); configured 1950 W`.
  - The near-target rule is now exercised. Three expiries landed one point
    below a fractional target and were recorded as `topup-target-reached`
    rather than misses: 53 against 53.08, 44 against 44.03, 47 against 47.14.
  - Repeat top-ups did not fall: four starts (21:20, 23:20, 01:20, 02:40)
    against three on each of the two prior nights. Total Utility minutes fell
    from 295 to 146.
  - The mechanism changed. This was a high-SOC night (69% at 20:20), so every
    check found a deficit too small to justify the 20-minute minimum and the
    night sawtoothed: skips of 0.3, 9.1 and 11.8 minutes, a 20-minute start at
    21:20 that reached target, skips of 7.3, 9.7, 12.1 and 14.4 minutes, a
    22-minute start at 23:20, skips of 4.4, 10.2, 11.5 and 19.4 minutes, then a
    21-minute start at 01:20 and the 85-minute late-safety window. Each plan
    aims at exactly the projected sunrise requirement (`target_soc` about one
    point above the starting SOC), so a completion leaves no headroom and the
    next checks rebuild the same deficit.
  - The realized rate is well above the planned rate and varies widely: implied
    1637 to 2405 W against planned 1571 to 1741 W. One learned rate cannot hold
    the target tightly, so plan headroom, not only per-band load, is in scope
    for N2.
- Remaining: one more night (2026-11-12), then write the verdict and close this
  task.
- Done when: three nights are reviewed and the result is written here as either
  "fewer repeat top-ups" or a follow-up task with the numbers.

### N2. Plan the night with an evening load and an overnight load

- Status: bands shipped 2026-11-12. Headroom is the next step. Changes inverter
  mode timing.
- Why: the planner uses one flat learned load (about 1450 W). On 2026-11-09
  every live reading was higher: about 2400 to 2700 W at 19:00 to 20:30, about
  1800 W at 21:00 to 22:30, and 1500 to 1650 W after midnight. The shortfall
  therefore regrows by about two minutes per 20-minute check, and a new top-up
  starts each time it crosses `AUTO_TOPUP_MIN_MINUTES`. On 2026-11-11 the same
  regrowth split a high-SOC night into three reserve top-ups plus the
  late-safety window; N1 has the numbers.
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
- Done when: banded planning is in use in production (audit `planned_load_w`
  reflects the horizon-weighted load), the headroom change is in, and N1-style
  checks over three nights show fewer repeat top-ups than the 2026-11-09
  baseline.

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
  - Done 2026-11-12: the renderer reads nothing. Pause, alert, cloud streak,
    PVOutput, audit rows and discharge-rate reads moved into
    `dashboard_viewmodel.py`, and the payload now carries automation history,
    recent decisions, upcoming overrides, runtime, the learned load and the
    Utility-hold detail. Rendered HTML was byte-identical to the previous code
    with empty and with populated state. Still open in this step:
    `build_dashboard_html` takes raw status, schedule and config scalars, so a
    payload-only signature remains.
  - Done 2026-11-12: one top-up estimate. The "Topup to Sunrise" row reads the
    planner's minutes instead of a second estimate built from a different load
    and horizon, and the manual `DISCORD_TOPUP_MAX_MINUTES` cap no longer
    shortens it.
  - Done 2026-11-12: the grid label comes from the payload, so a night on
    battery reads "No meaningful grid draw" instead of "Solar covering entire
    load".
  - With live data, look once at the Automation tab history chart and decisions
    table, and at an alert row on the Now tab when one applies. The chart and
    table are confirmed against the live payload on 2026-11-12 (7-day series,
    8 decisions); the alert row still needs a live alert to observe.

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
