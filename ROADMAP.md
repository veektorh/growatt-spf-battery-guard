# Growatt Guard Roadmap

Last reviewed: 2026-11-20

This is the working backlog for Growatt Guard. Priorities favor inverter safety,
low Growatt API pressure, trustworthy decisions, and clear operations before
convenience or larger product ideas.

## Next Up

This is the handoff queue: concrete tasks any agent can pick up without the
conversation that produced them. `AGENTS.md` explains how to use it. Each task
carries its evidence, the steps, and what "done" means. Remove a task when it
is finished and record anything it uncovered as a new task. Keep entries
public-safe: no IDs, serials, domains, or credentials.

### N5. Confirm the preserve-window deferral in production

- Status: waiting for a natural occurrence. Read-only.
- Why: PR #54 keeps the Utility hold when a time-boxed top-up ends inside a
  preserve window with SOC under the threshold. No such top-up has ended in
  production since it deployed on 2026-11-10.
- Done when: an audit row with action `topup-complete-deferred` is seen and the
  following scheduled `return-sbu` returned to SBU as planned.
- Read, 2026-11-20: still no `topup-complete-deferred` row. The latest
  completions (11-19 06:01-07:07 at 43-45%, 11-20 00:59 at 86%) ended outside
  a preserve window or above its threshold; the 11-19 21:17 hold expired
  instead of completing.

### N11. See a live alert row on the dashboard Now tab

- Status: waiting for a natural occurrence. Read-only.
- Why: N6 closed on 2026-11-14 with the renderer reading only the
  `dashboard.json` payload. The Automation tab history chart and decisions table
  were confirmed against live data on 2026-11-12, but no alert row (top-up
  running, Tonight Safe, cloud streak, pause, bypass) has been seen on the
  rebuilt Now tab yet.
- Done when: one alert row is seen on the live Now tab and its text matches the
  matching `dashboard.json` field (`automation.*` or `planner.tonight_safe`).

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
    SOC 41% at the first off reading, four points under the weather-aware 45%
    preserve threshold that morning.
  - 2026-11-18, scheduled 15:00-18:00: off between 15:05-15:09 at a read at
    15:56, so the window was still cut; back between 17:56-18:06. SOC 84% at
    the first off reading. The estate-18h cut began on schedule, with the
    14:55 `return-sbu` (skipped, already in SBU) and 15:01 `watchdog-sbu`
    (verified) audit rows before it.
  - 2026-11-19, scheduled 08:00-11:00: off between 07:59-08:09, back between
    10:54-11:09. SOC 43% before, 44% at the first off reading. This is the
    first outage morning after the sunrise-target change (N19, closed
    2026-11-19): the cut started at 43-44% against the 40% preserve
    threshold, above the line for the first time in the read mornings,
    against 39/39/41 on the N12 mornings.
  - 2026-11-19, scheduled 15:00-18:00: off between 14:55-15:10, back between
    17:55-18:10. SOC 90% before, 90% at the first off reading.
- Second read, 2026-11-20 (`supply-history --days 7`, 389 readings since
  2026-11-17T17:36): the 11-19 morning and afternoon edges above fill in, and
  the 11-18 afternoon end edge appeared. The 11-17 morning window has no
  readings because snapshots start at 17:36 that day, and the 11-17 afternoon
  start edge is unknown for the same reason. Still no unscheduled cut.
- Caveat found on the second read: `supply-history` labels every day with the
  profile active *now*, so the 11-18 morning window is reported as 08:00-11:00
  although the profile that morning was still estate-19h (08:00-10:30); the
  switch to estate-18h landed at 11:40 that day, before the afternoon jobs.
  Read a past day's edges against the profile that was active then.
- Edges sat within about 10 minutes (one refresh interval; 9.6 minutes average
  across the 389 readings) of the estate's scheduled edge on every window with
  readings on both sides. The widest is the 11-18 morning end, 10:54-11:09
  against an estate end of 11:00.
- Third read, 2026-11-20 13:23 (`supply-history --days 7`, 430 readings): the
  11-20 morning window is off between 07:58-08:01, back between 10:52-11:07.
  SOC 55% before and at the first off reading, the highest cut start read so
  far and 15 points above the 40% threshold the morning jobs used. Every
  window with readings on both sides still sits within one refresh interval
  of the schedule; no `unscheduled-cut` and no `no-cut` yet.
- Steps: after a few days, run `supply-history --days 7` on the VPS. Check that
  every scheduled window shows a `cut` and that the observed edges sit within
  one refresh interval of the schedule; note any `unscheduled-cut` or `no-cut`.
  Use the SOC at the first off reading in the outage-morning reads.
- Done when: a week of windows is read and the result is recorded here. If the
  estate's real cut times drift from the schedule by more than a refresh
  interval, open a task to adjust the profile windows. If edges need more
  precision, consider a durable ledger beside `bypass_daily.jsonl` rather than
  more polling.

### N20. Confirm the schedule-aware preserve skip on the next outage morning

- Status: the skip shipped 2026-11-19 with the owner's go-ahead
  (`PRESERVE_SCHEDULE_AWARE_SKIP_ENABLED`, default on, fail-closed to the
  current switch-now behaviour on any schedule read error). Read-only until
  the next outage morning.
- Why: the 2026-11-19 outage morning met every target but cycled the inverter
  to get there. The scheduled preserve jobs each switched to Utility and
  closed within minutes: 06:00 at SOC 45 (hold target 43 already banked, so
  the completion test fired on the first check), 06:30 at 43 the same way,
  then 06:40 and 07:00 at 42, charging seven minutes each back to 43. Eight
  mode writes between 06:00 and 07:07; the 07:55 return-sbu found SBU
  already; the 08:00 cut started at 43-44% against the 40% threshold. The
  same rows confirm the N14 fix path, closed 2026-11-19: the
  projection-triggered 06:40 and 07:00 holds stayed open past their first
  minute check and closed at the headroom target, and a live-below preserve
  completed at the threshold on 2026-11-18 (42% to exactly 45%). A reading
  already at or above threshold-plus-headroom self-completing in one check
  is correct — nothing is left to charge — but the churn is four Utility
  round-trips where one window could cover the pre-outage hour.
- Also read, 2026-11-19: the 18:00 reserve plan quoted `morning_target_soc=35`
  while the 00:00 plan quoted 40 — the evening weather-window estimate of the
  next morning's preserve threshold under-shot the actual 40, and only the
  midnight re-plan raised the target. Harmless that night (the evening plan
  sized to 93% on a low battery), but a high-SOC evening would leave the
  raise to the overnight checks alone.
- Shipped design: a projection-triggered preserve (live at or above the
  threshold, projection below it at the outage start) defers without writing
  mode when a later scheduled preserve still fires before the cut
  (`next_preserve_fire_before_outage`), recording a
  `deferred-to-later-preserve` audit row; the last job before the cut acts
  once and sizes its hold to the threshold plus the larger of the flat
  headroom and the projected drain to the outage start, so the battery
  arrives at the threshold instead of the last hour's load deciding it.
  Holding Utility until the 07:55 return was rejected on 2026-11-19: it buys
  two points of margin the threshold does not need for about 2.5-3 kWh of
  grid, and a larger flat morning headroom has the same cost. The trade is
  one acting window instead of one per scheduled job; the earlier jobs still
  run as live checks and the 08:01 watchdog still alerts.
- First live deferral row, 2026-11-19 11:05 (afternoon, not a morning): SOC 71%
  against a 45% threshold with a 40.8% projection at the 15:00 cut recorded
  `deferred-to-later-preserve`. The 12:05-14:05 jobs then found 83-89% and
  recorded `no-change`, the 14:55 `return-sbu` found SBU already, and the
  15:01 `watchdog-sbu` verified — no mode write all afternoon. The deferral
  path works, but PV recovery meant it never needed the last-job hold.
- Morning of 2026-11-20, read at 07:54: all four preserve jobs (06:00, 06:30,
  06:40, 07:00) projected 51-53% at the 08:00 cut against a 40% threshold, so
  each recorded `no-change` and the deferral path was not exercised.
- Same morning, after the cut: zero mode writes. The 07:55 `return-sbu` found
  SBU already and the 08:01 `watchdog-sbu` verified, so the whole morning ran
  as four live checks and no Utility round-trip. The cut started between
  07:58-08:01 at 55%, 15 points above the 40% threshold, which is why no job
  needed to act; the last-job hold stays unexercised until a morning projects
  below the threshold. First no-churn outage morning, though not the "one
  switch" shape the steps expected.
- Steps: read the next outage morning's preserve rows. Expect
  `deferred-to-later-preserve` rows for every job but the last, one
  `switch-to-utility`, and the cut starting at or above the threshold. Keep
  watching whether the 18:00 morning-target estimate under-shoots the
  actual threshold.
- Done when: the next outage morning shows one preserve switch and no churn,
  and the cut starts at or above the preserve threshold; if the later job
  fails its cloud write, the 08:01 watchdog alert and the fallback
  (switch-now) path record why.

### N21. Stop the morning Tonight card from demanding an impossible top-up

- Status: open, display-only; it changes no mode timing. Read-only until the
  owner picks a rule.
- Why: at 08:40 on 2026-11-20 the Now tab read "High risk", projected sunrise
  SOC 0% against the 40% reserve, and "Top-up Tonight 9h 36m / 18.7 kWh" —
  more grid energy than the 30 kWh battery can hold. `build_tonight_risk`
  projects from now to sunrise (21h 44m) at the weekday night bands
  (1988 W), which is 43 kWh over a daylight day; before noon
  `_should_use_projected_sunset_start` is false, so the projector it uses is
  not the sunset start. Its own note reads "Stress estimate from carrying
  that load until sunrise", so the number is deliberate but the badge and the
  "Top-up Tonight" tile present it as tonight's plan. Every morning on a
  full-sun day therefore shows High risk, and N11's alert-row read cannot
  tell a real alert from this one.
- Steps: choose the morning behaviour — project from the sunset start whenever
  a PV forecast exists, show the stress figure only as an explicitly labelled
  worst case, or suppress the card before noon — then make the badge, the
  tile, and the note say the same thing. Keep the evening behaviour that N11
  watches, and the sunset projection after 12:00.
- Done when: on a sunny morning the card states what tonight needs after the
  projected sunset SOC, or that it cannot estimate yet, and no badge or tile
  demands more energy than the battery holds.

### N22. End the night-plan horizon at PV takeover, not astronomical sunrise

- Status: open, read-only. The flat "+60 minutes to 48%" option was rejected on
  2026-11-20 as an over-correction; what remains is whether a cloudy cut-day
  needs a horizon offset at all.
- Why: the reserve plan targets SOC at sunrise, but the battery keeps carrying
  the house until PV covers the load. On 2026-11-20 (sunrise 06:36, normal,
  dry-season threshold 40) PV rose 6 W at 06:28 to 1160 W at 07:28 against
  1.2-1.3 kW of load; the crossover was about 07:35, 59 minutes after sunrise,
  and SOC fell 58% to 54% across that hour (4 points, 1.2 kWh). The 3-point
  headroom in the 43% target is calibrated to exactly that: a met target reads
  39% at 07:35, then PV gives back about a point before 08:00 (SOC 54 to 55
  from 07:35 to 07:58), so the cut starts at 40 - at the threshold, as designed.
  Moving the horizon to +60 minutes would raise the target to about 48 and the
  cut start to about 45, buying 1.5 kWh overnight for margin that PV provides
  free 25 minutes later. The N12 mornings that started at 39-41% predate N19's
  sunrise-target change, when the plan did not size to the next morning's
  preserve threshold at all (`AUTO_TOPUP_TARGET_SOC` is unset, so the floor was
  the 20% BMS cutoff), so they are not evidence for a higher horizon; and the
  band already overplans by 15-20 points, which is why today arrived at 54-55
  rather than 39.
- Remaining question: on a rainy/cloudy cut-day the dip is larger (6-8 points)
  and PV gives back less during the cut. The 45% dry-season threshold and N20's
  last pre-cut hold are the current cover, and N20's hold sizes only to the
  drain up to the cut start, not the drain during it.
- Partial answer from the retained window, 2026-11-20: both measured cut
  mornings were PV-positive, so a 35% arrival would have sufficed. On 11-18
  (rainy/cloudy, 45% threshold) the cut started at 41%, dipped to 40% between
  08:26 and 08:41, then climbed to 57% by 11:11; on 11-19 it started at 44% and
  climbed straight away to 69% by 10:54. PV ran 2.5-5.4 kW against 1.0-1.4 kW
  of load, and the deepest cloud pass (09:26-09:41 on 11-18) still held SOC
  flat with 1.1-1.4 kW of PV. So the 40% threshold's five points are insurance
  against a fully overcast or unscheduled cut, worth roughly the 1.5 kWh they
  cost - not the main lever on grid use, which is the band's 15-20 point
  overshoot (the hourly plan halves it, and the full-charge clamp bounds what
  that saves on any given window).
- Read, 2026-11-20 (third normal cut-morning): the cut started between
  07:58-08:01 at 55%, not 40-42. The horizon is not the cause: the 00:00 plan
  sized to 86% at sunrise, the 00:59 top-up hit 86 exactly, and the battery
  then carried the house to 55% at dawn. The miss is the load estimate — the
  plan used 2028 W (the bands) against an actual ~1.6 kW from 01:00 to 08:00,
  so it arrived ten to twelve points above the 43-45 target. N17's hourly
  planning deployed at 09:47 that day, so the 18:00 plan that evening is the
  first hourly-profile plan; read the next arrival against this 55% band-era
  baseline.
- Weather-category flip, 2026-11-20: the 06:00-07:00 preserve rows used the
  normal 40% threshold and the 08:40 Now card still read 40, while the
  11:00-onward rows and the same dashboard read rainy/cloudy 45%
  ("max cloud 59%, rain 2.2mm"). The day turned sunny, so the lower morning
  figure cost nothing; an overcast day whose category only firms up after the
  cut would have been planned five points light. Read a past cut morning's
  category from the audit rows of that morning, not the midday dashboard.
- Steps: read the next two normal mornings to confirm the cut-start arithmetic
  (target met, cut starts 40 plus or minus a point). Then read an overcast
  cut-morning: whether SOC drains through the cut with PV below load for hours,
  and whether the last pre-cut hold covered that. Only then choose between a
  category-scaled horizon (rainy/cloudy +90), a lower normal threshold, or
  leaving both alone.
- Done when: a normal cut-morning is confirmed within a point of the threshold
  with the target met, a cloudy cut-morning is read, and the owner decides
  whether any horizon offset is needed.

### N23. Name the midday surplus window when the battery is full

- Status: open, display-only; it changes no mode timing. Read-only until the
  owner picks the surface.
- Why: on 2026-11-20 the battery reached 99% at 12:53 and the SPF, which has
  no export path, pinned the array to the house load: PV 2413 W against a
  2322 W load at 12:53 with the battery at 5 W and charge 0 W, then 1268 W
  against 1202 W at 13:08 and 1360 W against 1284 W at 13:23. From 12:53 to
  13:23 `pv_today_kwh` and `load_today_kwh` both rose 0.7 kWh — nothing above
  load was stored or used. The day had peaked at 5.4 kW at 10:52 and the
  7-day same-time pace expected 40.1 kWh against 24.7 kWh recorded, so the
  afternoon's remaining PV had no sink.
- Why it matters: the only lever on an off-grid SPF is moving deferrable load
  into the strong-sun hours; the guard cannot store or export the surplus. The
  Now tab already says "Battery is full, so the current 1.4 kW solar is best
  used by flexible house loads", but that exists only while someone watches,
  and the daily summary, the ledger, and PVOutput cannot tell capped output
  from array potential.
- The overnight top-up was not the cause: the only grid charge was the
  00:00-00:59 reserve window, 80% to 86% at an implied 1835 W against 1868
  planned. Skipping it would have left about 48-49% at dawn and the array
  would still have filled the battery roughly half an hour later (1.8 kWh at
  the 3.4 kW it accepted in late morning). No avoidable charge to remove by
  that lever.
- Steps: choose the surface — a Now-tab row while `soc >= 95` and PV stays
  within about 100 W of load across consecutive refreshes, a daily-summary
  line naming the capped hours, or a one-shot Discord nudge — and state the
  day's peak so the reader can size the lost window without a fabricated
  potential figure. Keep it on the existing read-only refresh path.
- Done when: a full-battery afternoon states that output is capped and names
  the usable strong-sun window, and the day's record separates capped output
  from array potential.

## Current Baseline

The following capabilities are implemented and should be preserved:

- Cloud-safe morning and afternoon preservation, verified SBU returns,
  watchdog checks, health reporting, and conditional night auto-topup. A
  projection-triggered preserve defers to the last scheduled job before the
  cut, which sizes its hold to the projected drain so the outage starts at
  the threshold with one acting window per morning.
- Sunrise planning using learned weekday/weekend evening and overnight band
  loads (horizon-weighted), observed charge rates, weather calibration,
  minimum topup duration, SOC safety floors, and a sunrise target raised to
  the next outage morning's preserve threshold.
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

Current concrete steps toward this goal are in Next Up: N20 (next outage-morning
read). N2 closed on 2026-11-19 with the verdict "fewer repeat top-ups": three
banded weekday nights each ran two starts against the 2026-11-09 baseline of
three, and the band-vs-live direction proved unstable, so no live-load cap was
added. N19 closed the same day: the first outage morning after the change
started its cut at 43-44% against the 40% preserve threshold, the first read
morning above the line. The learned charge rate went live on 2026-11-10 at the
owner's direction, ahead of the three-comparable-outcome gate; N1 collected
those outcomes after the fact and closed on 2026-11-13 with the repeats still
present, which is why N2 owned them. N12 closed on 2026-11-18 with three of
five outage mornings starting below the preserve threshold, so the
sunrise-target change returned as N19. N17 closed on 2026-11-20 at the owner's
direction ("fix the estimate first", threshold left at 40):
`planning_night_load` prefers the night's hour-of-day profile (1827 W at the
18:00 horizon against the band's 2028 W, about 1750 W at 00:00 against 1990 W)
and falls back to the bands when any hour lacks three days, so weekend nights
keep the bands until they qualify. The gain is bounded by the full-charge
clamp: an evening window whose completion target is already 100% ends at the
same SOC, and the saving lands on post-midnight windows (86% to 81% on the
11-19 night, about 1.6 kWh of that window's 1.8 kWh).

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
