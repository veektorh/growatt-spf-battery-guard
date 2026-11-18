# Growatt Guard Roadmap

Last reviewed: 2026-11-18

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
- Night of 2026-11-17 (2 of 3 on bands; the first whole weekday night on
  them):
  - Two starts: 18:00 for 214 min (reserve, 62% to 81% against an 83.09%
    target, planned 1996 W) and 02:40 for 51 min (late-safety, 53% to 58%
    against 58.11%, planned 1962 W), so 267 Utility minutes against 295,
    146, 202, 165 and 251 on the five previous nights.
  - The gap between the 21:35 completion and the 02:40 start was 5 h 05 m, and
    nothing re-armed after the 03:32 completion. Both plans quoted banded
    loads, so no flat fallback ran all night.
  - The night's own row recorded evening 2518 W over 2 samples and overnight
    1736 W over 21 samples; the 21:35 to 02:40 drain (81% to 53% on 30 kWh)
    implies about 1650 W, so the overnight band ran close this time rather
    than the 20% high of 2026-11-16.
  - One more weekday night (2026-11-18) completes the three-night read, then
    judge repeat top-ups against the 2026-11-09 baseline of three.
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

### N19. Raise the morning top-up target so outages stop starting below the preserve threshold

- Status: the owner picked the preserve-threshold option on 2026-11-18 and it
  shipped the same day: the night plan raises its sunrise target to the next
  morning's preserve threshold when that morning is an outage day
  (`AUTO_TOPUP_MORNING_TARGET_ENABLED`, default on, weather-aware, fail-closed
  to the flat target). Read-only until the next outage-day morning.
- Why: N12's five outage-day mornings ended with the outage starting below the
  preserve threshold on three of them: 39% against 40 on 2026-11-16, 39%
  against 40 on 2026-11-17 (its 07:00 hold was lost to the N14 bug), and 41%
  against a weather-aware 45 on 2026-11-18. Per N12's done-when, the
  sunrise-target options reopen.
- 2026-11-18 detail: the 06:30 preserve switched at 42% against 45%
  (projected 34% at the outage start), the 06:40 and 07:00 checks deferred as
  `lock-skip` rows (`held_by=topup-complete-check` with `held_since` and
  `age_min` populated — the 2026-11-17 payload fix confirmed in production),
  the window completed at 07:02 at exactly 45%, and the 08:01 watchdog read
  41%, four points under. The structural gap is unchanged: the top-up that
  serves the morning completes at the threshold and the load takes two to
  four points off before the 08:00 outage start, and the 07:00 preserve hold
  that should cover that drain has not survived to 08:00 on any morning read.
- Options carried over from N3/N12: `AUTO_TOPUP_TARGET_SOC=35`, or a target
  taken from the next morning's preserve threshold. Both land the battery
  nearer the next morning's preserve threshold than today's flat sunrise
  floor (about 20%), at the cost of more overnight Utility minutes. The
  owner chose the preserve-threshold option, so the battery arrives at the
  06:30 preserve at or above the threshold, the preserve switches on the
  projection alone, and its hold charges past the threshold by the headroom
  (the N14 fix path) instead of completing exactly on the threshold and
  letting the load drain the gap before the outage starts. Audit rows now
  carry `sunrise_target_soc` and `morning_target_soc` for the read.
- Note: N14's headroom fix is still unexercised — no projection-triggered
  preserve has run since it merged on 2026-11-17.
- Done when: the owner picks an option, it ships in a mode-timing PR, and a
  later outage-day morning starts at or above the preserve threshold.

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

### N14. Confirm a projection-triggered preserve hold survives its first check

- Status: fix merged 2026-11-17; waiting for the next projection-triggered
  morning. Read-only now. Still unexercised on 2026-11-18: the 07:00 preserve
  deferred as a lock-skip while the 06:30 window completed at 07:02, so no
  projection-triggered preserve has run since the fix merged.
- Why: `_record_preserve_utility_hold` writes the hold with the default
  `completion_policy="soc"` and `target_soc=threshold`, and
  `topup-complete-check` closes any `soc` hold whose reading is at or above the
  target. #85 lets `preserve-battery` switch on
  `projected_soc_at_outage_start`, so the switch can now happen while the live
  SOC is already at the threshold — and then the hold satisfied its own
  completion test on the next check, one minute later.
- Evidence, 2026-11-17: 07:00:07 threshold 40.0%, live SOC 40.0%, projected 35%
  at the 08:00 outage start, "Preserve-battery Utility hold recorded until
  2026-11-17T06:55:00+00:00 with target SOC 40.0%"; 07:01:03 "Topup SOC target
  reached: 40% >= 40%" → `topup-target-reached actual_min=1, start_soc=40,
  end_soc=40, target_soc=40`; 07:01:06 `return-sbu`. The 06:30 window on the
  same morning was unaffected because it started at 38 against a 40 target. The
  outage still started at 39% because the lost hold was the only thing covering
  the two points of drain between 06:52 and 08:00.
- What changed: when the live reading is already at or above the threshold, the
  preserve hold targets `threshold + AUTO_TOPUP_HEADROOM_SOC` so its own
  completion test cannot fire on the next minute check; a live-below switch
  still targets the threshold and still stops the 06:30 window there. The
  decision log and the switch print now quote the deciding SOC instead of
  claiming a reading equal to the threshold is below it.
- Why not `completion_policy="time"`: the time path in
  `_command_topup_complete_check_locked` does not wait on `max_expiry`. It falls
  through to `_defer_completion_to_scheduled_return`, which returns to SBU as
  soon as the reading is at or above the threshold, so the hold would have died
  at the same minute with an extra resume and a legacy-topup log.
- Done when: a projection-triggered 07:00 preserve stays open past its first
  minute check and closes at the headroom target or the 07:55 return, and a
  live-below preserve still completes at the threshold.

### N16. Read the first week of observed supply cuts

- Status: `supply-history` shipped 2026-11-17; snapshots carry `grid_v` from
  that deploy on. Read-only.
- Why: until now the outage start was inferred from the 08:01 watchdog reading
  (N12 reads "the 08:01 watchdog read 39%"). The SPF's `vGrid` reads 0 V during
  a cut (seen at 17:29 on 2026-11-17, inside the 15:30-18:00 window) and about
  215-235 V on supply, so each snapshot now says whether the estate was on.
- First windows read, 2026-11-18:
  - 2026-11-17, scheduled 15:30-18:00: already cut at the first reading
    (17:36), back between 17:51-18:06, so the end edge sits within one
    refresh interval of 18:00. SOC 66% at the first off reading.
  - 2026-11-18, scheduled 08:00-10:30: off between 07:55-08:11, back between
    10:56-11:11. The start edge is within one refresh interval of 08:00; the
    end ran 26 to 41 minutes past the scheduled 10:30, matching the 11:00
    end of the estate-18h notice rather than the installed profile's 10:30.
    SOC 41% at the first off reading, the number N19 quotes for that morning.
  - 2026-11-18, scheduled 15:00-18:00: off between 15:05-15:09 at a read at
    15:56, so the window was still cut; SOC 84% at the first off reading. The
    estate-18h Wednesday cut began on schedule, with the 14:55 `return-sbu`
    (skipped, already in SBU) and 15:01 `watchdog-sbu` (verified) audit rows
    before it.
  - No unscheduled cut appeared in the first read.
- Steps: after a few days, run `supply-history --days 7` on the VPS. Check that
  every scheduled window shows a `cut` and that the observed edges sit within
  one refresh interval of the schedule; note any `unscheduled-cut` or `no-cut`.
  Use the SOC at the first off reading in the N19 morning reads.
- Done when: a week of windows is read and the result is recorded here. If the
  estate's real cut times drift from the schedule by more than a refresh
  interval, open a task to adjust the profile windows. If edges need more
  precision, consider a durable ledger beside `bypass_daily.jsonl` rather than
  more polling.

### N17. Decide whether the night plan should use the hourly load profile

- Status: `load-profile` shipped 2026-11-17 as a shadow view. Read-only until a
  decision; wiring it into `command_auto_topup_check` changes inverter mode
  timing and needs the owner's go-ahead. Follows N2.
- Why: N2's two bands still smooth over a clear shape. Eight days of snapshots
  (to 2026-11-17) give medians of about 1370 W at 18:00, 1620 W at 19:00,
  2020-2170 W from 20:00 to 22:59, then 1630-1800 W until 06:00. The idea comes
  from Epilykos's consumption forecast (same hour over recent days, median and
  20th-80th percentile); the code here is our own.
- Caveat found on the first run: for a weekday night the hourly profile gives
  21.2 kWh (average 1769 W, likely 18.5-25.5 kWh) and the band planner 23.8 kWh
  (evening 1903 W, overnight 2019 W over 21 nights). `load_w` is the inverter's
  AC output, while the planner learns from battery drain, which also carries
  conversion and standby losses; the 11% gap is about that size. Any switch
  must first learn that ratio from nights where both are recorded.
- First morning read, 2026-11-18, for the night of 2026-11-17: hourly profile
  21.4 kWh (average 1786 W, likely 19.0-25.3 kWh) against band 23.4 kWh
  (evening 1959 W, overnight 1942 W, 22 nights) — an 8.5% gap, narrower than
  the 11% on the first run. The night's implied overnight drain was about
  1650 W against a learned overnight band of 1736 W.
- Steps: run `load-profile` on the VPS each morning for a week, beside the
  previous night's actual drain (`discharge_rate_history.json`). Record, per
  night, hourly-profile kWh, band kWh, and actual kWh. Snapshots mix weekday and
  weekend days; check whether weekend hours differ enough to split them.
- Done when: a week is recorded and the owner decides either to wire the hourly
  profile (scaled by the learned drain-to-load ratio) into the night plan in a
  separate mode-timing PR, or to keep the bands and remove this task.

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
- Observed estate supply from the recorded grid voltage (`supply-history`),
  set against the scheduled outage windows.
- A shadow hour-of-day load profile (`load-profile`) compared with the band
  planner's night energy.
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
load and an overnight load, now observing in production) and N19 (raise the
morning top-up target after N12's five mornings). The learned charge rate went live on
2026-11-10 at the owner's direction, ahead of the three-comparable-outcome gate; N1 collected
those outcomes after the fact and closed on 2026-11-13 with the repeats still
present, which is why N2 owns them now. N12 closed on 2026-11-18 with three of
five outage mornings starting below the preserve threshold, so the
sunrise-target change returned as N19.

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
  The known route for the SPF is the Shine stick's own push (an 8-byte header
  with Modbus input-register payloads on TCP 5279, as Epilykos receives it).
  Pointing the stick at our host would cut the cloud path that mode writes use,
  so it needs a relay that forwards to Growatt as well, and it is read-only.
- Rejecting implausible jumps in daily energy counters was considered on
  2026-11-17 and not needed: 248 ledger days showed no spike, and the stale
  carry-over and flat-repeat guards in `daily_generation.py` cover the faults
  seen so far.
- Holding the battery overnight instead of topping it up from the grid was
  considered on 2026-11-17 and dropped by the owner. It would save about 1 kWh
  a night of round-trip loss (about 8 kWh a night goes grid -> battery ->
  house) and some cycle wear, but running the house on SBU overnight keeps it
  isolated from night-time grid voltage swings and gives full cover for an
  unscheduled cut. Revisit only if N16 shows the night grid is consistently on
  and the tariff makes the saving matter.
- A deeper PVOutput comparison view for forecast versus actual production.

## Good First Issues

1. Add quiet-hours configuration and tests for non-critical notifications.
2. Add a semicolon-delimited multi-day PVOutput fixture to the public-safe
   fixture library.
3. Add metric-history download/export with offline tests.
