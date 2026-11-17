# Growatt Guard Roadmap

Last reviewed: 2026-11-17

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

- Status: bands and headroom shipped 2026-11-12; banded planning went live at
  the 22:00 check on 2026-11-16 (night 1 of 3 read). Changes inverter mode
  timing.
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
- Night of 2026-11-16 (1 of 3 on bands; the split went live mid-night):
  - Two starts. 18:00 for 208 min (reserve, 56% to 78% against a 78.27% target,
    implied 1895 W against 1934.9 W planned, `planned_load_w=1819`, still the
    flat four-night fallback: `Planning with 1819 W from learned 1819 W (weekday
    history (last 4 of 21 nights)) and live 1457 W`). Then 22:00 for 41 min
    (76% to 80% against 80.29%, implied 1719 W against 1908.4 W planned,
    `planned_load_w=2175.86`).
  - The 22:00 window is the first banded plan: `Planning with 2176 W from
    learned 2176 W (weekday bands (overnight 2176 W for 8.6h over 3 nights))
    and live 1731 W`. The running row for the night supplies the third weekday
    overnight band, and 251 Utility minutes against 295, 146, 202 and 165 on the
    four previous nights.
  - The 31-minute gap between the 21:29 completion and the 22:00 start is the
    band switch, not a re-arm: the 18:00 plan sized to 78.27% on the flat
    1819 W, and the banded 2176 W overnight raised the target to 80.29%. The
    window it asked for landed (41 planned, 42 actual).
  - Nothing re-armed after 22:42. The 02:40 to 04:20 checks skipped as short
    (9.4 min down to 1.2 min against the 20-minute minimum) and the 04:40 to
    05:20 checks needed nothing at SOC 49 and 45 against the sunrise floor, so
    the last top-up row of the night is the 04:20 short skip.
  - The band over-plans the drain: live `bat_w` held 1650 to 1780 W all night
    against a planned 2019 to 2084 W, and the 02:40 to 05:20 drain (60% to 45%
    on 30 kWh) implies about 1690 W. That is N1's rate-accuracy weakness on the
    load side instead of the charge side, and it is why the 22:00 window asked
    for 41 minutes and still landed at 80%.
- When the bands can go live: readiness counts banded nights of the *same day
  type*, and `day_type` treats Saturday and Sunday as weekend. Two weekday
  nights carry `bands` (2026-11-12 and 11-13) and two weekend nights do
  (2026-11-14 and 11-15, confirmed in `state/discharge_rate_history.json` on
  2026-11-16). The night of 2026-11-16 becomes the third weekday banded night
  as it runs: its evening band counts from 18:00, and the 22:00 check records
  its first overnight-band sample before planning, so the first banded plan can
  come at the 22:00 check on 2026-11-16. It did: that check planned on
  `weekday bands (overnight 2176 W for 8.6h over 3 nights)`, so the evening of
  2026-11-17 is the first whole weekday night on bands. Weekend needs one more
  night (11-21), so the earliest weekend banded plan is after 22:00 on
  2026-11-21.
- Next reads, from the night of 2026-11-13 onward:
  - `auto-topup-started` count per night, against the 2026-11-09 window's three.
  - The gap between a `topup-complete-check` and the next start. The corrected
    sizing should land about three points above the policy target and stretch
    the re-arm interval towards two hours instead of the 2026-11-11 one.
  - `planned_load_w` on each start: it stays the flat average until three
    matching nights carry `bands`, so a banded value is the signal that the
    evening/overnight split is live. `state/discharge_rate_history.json` shows
    whether the newest rows have `bands`. This read is now answered: the 22:00
    start on 2026-11-16 planned on bands.
  - Whether the band should keep a learned figure that runs about 20% above the
    live `bat_w` it plans against. Two independent signals agree the band is
    high (the night's own samples and the 02:40 to 05:20 drain), so the next
    three nights decide whether the split needs a live-load cap the way the
    flat average has `_LIVE_LOAD_UPLIFT_CAP_FACTOR`.
- Done when: banded planning is in use in production (audit `planned_load_w`
  reflects the horizon-weighted load), the headroom change is in, and N1-style
  checks over three nights show fewer repeat top-ups than the 2026-11-09
  baseline.

### N12. Review a week of mornings with the outage-start preserve

- Status: four of five outage-day mornings read (2026-11-14, 11-15, 11-16,
  11-17). Read-only. A change it leads to alters inverter mode timing and needs
  the owner's go-ahead.
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
  outage starts. Compare that SOC with the day's preserve threshold. When a
  top-up holds the lock at 07:00 the row is `lock-skip` with `held_by` and
  `age_min` in its note; read that as a deferral, not a missing check.
- Mornings read so far: 2026-11-14 and 11-15. Both kept the 06:30 preserve
  (`switch`, projected 19% and 29% at the outage start against thresholds 45
  and 40) and both deferred the 07:00 check to the threshold top-up's hold. The
  08:01 watchdog reading on each morning was 45% and 40%, exactly the day's
  threshold, so neither outage started below it.
- 2026-11-16 (third morning): the 06:30 preserve switched at SOC 34 against a
  40 threshold (projected 23% at the outage start), the 06:40 completion check
  and the 07:00 check both gave way as `lock-skip` rows (the first production
  confirmation of the #93 evidence change; the top-up held the lock and
  completed at 07:16 with SOC 40 against its 40 target), and the 07:55 return
  skipped because the inverter was already on SBU. The 08:01 watchdog read 39%
  against the 40 threshold: the first morning the outage started below the
  preserve threshold, by one point.
- 2026-11-17 (fourth morning): the 06:30 preserve switched at SOC 38 against 40
  (projected 30% at the outage start), the 06:40 retry gave way as
  `lock-skip held_by=unknown` (no `age_min`, so the lock state was read before it
  was written, see N15), and the window completed at 06:52 with SOC 40 against
  its 40 target (22 min, implied 1640 W against 1787 W planned).
  - The 07:00 `morning-preserve-late` switched on the projection alone: live SOC
    40.0% against a 40.0% threshold, `decision_soc` = min(40, projected 35) = 35.
    The hold was recorded "until 06:55Z with target SOC 40.0%", and the 07:01
    completion check closed it as `topup-target-reached actual_min=1,
    start_soc=40, end_soc=40, target_soc=40` before returning to SBU at 07:01:06.
    The designed 55-minute hold lasted 57 seconds; see N14 for the cause.
  - So the inverter went back to SBU at 40% with a projected 35% at the 08:00
    outage start, which makes two mornings in a row below the threshold, and
    this one was self-inflicted rather than a slow drain.
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

### N13. Switch to `estate-18h` once the 2026-11-18 rationing notice is confirmed

- Status: profile defined and tested, switch pending the owner's confirmation.
  Read-only until then. Changes inverter mode timing on switch.
- Why: the estate is considering 18h weekday / 20h weekend supply from Wednesday
  2026-11-18 — weekday cuts 08:00-11:00 and 15:00-18:00, weekend cuts
  08:00-11:00 and 17:00-18:00. The arithmetic agrees with the stated hours
  (24 - 6h = 18h weekday, 24 - 4h = 20h weekend), so the windows are cuts, not
  powered blocks.
- Steps: after deploy, run `./install_cloud_cron.sh` from the running release
  so the profile's 18 job variants are installed, then
  `set-profile estate-18h --reason "…"` and keep `set-outage-days auto`.
  `schedule-preview --days 8` must show the 15:00 cut on Wednesday and the
  17:00 cut on Saturday.
- Done when: `profiles` reports `estate-18h` active, `health-check` reports the
  installed count across 3 profiles, and the first Wednesday 15:00 cut shows
  `afternoon-return-sbu` then `afternoon-watchdog` audit rows.

### N14. Keep a projection-triggered preserve hold past its first completion check

- Status: found 2026-11-17 on the fourth N12 morning. The fix changes inverter
  mode timing and needs the owner's go-ahead.
- Why: `_record_preserve_utility_hold` writes the hold with the default
  `completion_policy="soc"` and `target_soc=threshold`, and
  `topup-complete-check` closes any `soc` hold whose reading is at or above the
  target. #85 lets `preserve-battery` switch on
  `projected_soc_at_outage_start`, so the switch can now happen while the live
  SOC is already at the threshold — and then the hold satisfies its own
  completion test on the next check, one minute later.
- Evidence, 2026-11-17: 07:00:07 threshold 40.0%, live SOC 40.0%, projected 35%
  at the 08:00 outage start, "Preserve-battery Utility hold recorded until
  2026-11-17T06:55:00+00:00 with target SOC 40.0%"; 07:01:03 "Topup SOC target
  reached: 40% >= 40%" → `topup-target-reached actual_min=1, start_soc=40,
  end_soc=40, target_soc=40`; 07:01:06 `return-sbu`. The 06:30 window on the
  same morning was unaffected because it started at 38 against a 40 target.
- Steps: when the switch was decided on the projection and the live SOC is not
  below the threshold, record a `completion_policy="time"` hold with
  `target_soc=None`, the shape `_defer_completion_to_scheduled_return` already
  uses, so the hold survives to the scheduled `return-sbu`; keep the `soc`
  policy when `start_soc < threshold`, which is what stops the 06:30 window at
  the threshold. The decision line also says the reading "is below 40.0%" when
  it equals the threshold; quote `decision_soc` instead.
- Done when: a projection-triggered 07:00 preserve holds Utility to the 07:55
  return, and a live-below-threshold preserve still completes at the threshold.

### N15. Read the command-lock state without a torn read

- Status: found 2026-11-17. Read-only; audit quality only, no mode change.
- Why: `acquire_command_lock` creates the lock file with `O_CREAT | O_EXCL` and
  only then writes the JSON payload, so a concurrent `topup-complete-check` can
  read an empty file and audit `lock-skip held_by=unknown` with no `age_min` or
  `held_since`. Seen at 2026-11-17 06:40:03 against the 06:40 preserve retry.
- Steps: write the payload to a temporary file and link or rename it into place,
  or have `record_lock_skip` retry the state read once before writing the row.
- Done when: a locked `topup-complete-check` always names the holder and its age
  in the `lock-skip` row.

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
