# Growatt Guard Roadmap

Last reviewed: 2026-11-23

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
- Read, 2026-11-21: still no `topup-complete-deferred` row. The 11-20 18:00
  window ran 140 planned minutes from 86%, closed `topup-expired` at 99% at
  20:21 on a 1661 W implied rate against 1879.6 planned (taper at the top),
  and the 20:21:07 `return-sbu` switched to SBU outside any preserve window.
- Read, 2026-11-22: still no `topup-complete-deferred` row. The 11-21 18:00
  window closed `topup-target-reached` at 22:02 at 100%, far outside any
  preserve window, and its `return-sbu` switched to SBU as planned.
- Read, 2026-11-23: still no `topup-complete-deferred` row. The night's last
  pre-cut hold (07:00, on the 87% overnight arrival) closed `topup-expired` at
  07:55 at 56% against a 60.33% target — above the 45% rainy/cloudy threshold —
  so the deferral condition never applied. The 07:55:03 `return-sbu` was
  `lock-skip`ped against the completion check and the 07:55:10 one switched to
  SBU as planned.

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
- Read, 2026-11-21 (fourth normal cut morning, second hourly-profile arrival):
  the cut started between 07:55-08:10 at 44%, four points above the 40
  threshold. Sunrise arrival was 46-47 (47 at 06:10, 46 at 06:40, 45 at 06:55,
  44 at 07:55 and 08:25) against the 43 target, so the target was met and
  overshot by 3-4 rather than landing on it; PV hit 1913 W by 07:55 against
  1038 W of load and discharge fell to 334 W, so nothing drained across the
  carry hour. Counterfactual on the night's own numbers: the 18:00 plan sized
  140 min against a 1826 W hourly-profile load to fill 86% to 100%, and the
  measured discharge from the 20:21 window end to sunrise was 99% to 49% in
  9.57 h, about 1568 W. Re-running the same plan at 1568 W still asks 86% to
  reach 43% at 06:37 against 12.62 h of load, which is 12.9 kWh available
  against 19.8 kWh of demand, a 6.9 kWh (23 point) deficit — still beyond 100%.
  So both the planned and the measured load sit on the clamp, the window could
  not have charged less, and there is no avoidable charge to remove here; the
  4-point overshoot at the cut is the clamp plus a load estimate about 17%
  high, not the horizon. Ignore this morning for the horizon question.
- The hourly plan improved arrival at the same 18:00 horizon: 46-47 at sunrise
  on 11-21 against 54-61 over 06:00-07:55 on 11-20, with the planned load down
  from the band's 2028 W to 1826 W. Two nights only, and both clamp-bound, so
  it is direction, not evidence for a further change.
- Read, 2026-11-22 (fifth normal cut morning, first weekend night since N17,
  and the second band-planned night whose completion target stayed under 100%):
  the 18:00 window asked 244 min from 73% to a 98.5%
  completion target on a 2124.5 W weekend-band load (`weekend history (last 4
  of 9 nights)`) at a 1881.7 W learned rate, and closed `topup-target-reached`
  at 22:02 after 242 min at 100% on a 2009 W implied rate. The morning read 50%
  at 06:00 and 44% at 07:00, so the arrival sat 15 points above the 35% sunrise
  target and 12 above the 38% the plan sized to (35 plus 3 headroom).
  Counterfactual on the night's own numbers: discharge from the 22:02 window
  end to 06:00 was 50 points in 7h58m, 1882 W, and the same plan run at 1882 W
  asks 211 min to the same 38% target, completing at 95.0% and arriving at
  38.1%. So 33 minutes, about 1.0 kWh of grid, were avoidable on this night and
  the whole gap is the band's 13.4% load overestimate. This is the same shape
  as 2026-11-20, the other band-planned night that ended under 100% — there
  the weekday bands planned 2028 W against about 1.6 kW and the arrival landed
  12 points over its 43-45 target — so the band error is a second sample of a
  known direction, not a new failure.
- The replacement basis is already measured: the weekend hour-of-day profile
  qualified between that plan and this read (four weekend dates inside the
  8-day window now, two at 18:00 yesterday) and reads 21.0 kWh, 1750 W over
  12 h from 18:00. The same arithmetic on it asks 191 min, completes at 92.9%,
  and against the night's actual 1882 W drain would have arrived at 33.8% —
  six points *below* the 40% threshold. The band's +13.4% and the profile's
  -7% bracket the night, so this one night justifies no basis change either
  way; tonight's 18:00 plan is the first weekend night sized by the profile
  and is the read to judge it on, with N20's last-job hold as the cover if it
  lands low.
- Fourth consecutive evening plan quoting `morning_target_soc=35` against the
  morning's 40. Here the completion target stayed under the ceiling (98.5%),
  so the gap shows in the window length: sizing to 35 plus headroom asked 244
  minutes where a 40-based plan asks 263 and ends at 100%. The under-shoot
  therefore removed 19 minutes of charge in the unsafe direction — this
  morning the band's overestimate covered it, a tighter night arrives under
  the threshold.
- Read, 2026-11-23 (sixth cut morning read, second night sized by the hour-of-day
  profile and the first weekend night, rainy/cloudy category at a 45%
  threshold): the 18:00 plan asked 365 min from 49% to an 88.54% completion
  target on a 1866 W load (`hourly profile median (weekend, 12.62h)`) at a 1950
  W learned rate, and quoted `sunrise_target_soc=45` — the same threshold the
  morning used, so the evening/morning category flip of 2026-11-20 is gone for
  this night. The plan is internally consistent: 39.54 points in 6h05m at 1950
  W, then 6h31m of drain to 06:37 leaves 48.0, the 48 target (45 plus 3
  headroom). It closed `topup-expired` at 00:06 after 366 min at 87%, 1.54
  points short on a 1869 W implied rate against 1950 planned (4% low, worth
  exactly those 1.5 points). The drain from 00:06 (87%) to 06:37 (52.2%) was
  34.8 points in 6h31m, 1602 W — 14.1% below the 1866 W the plan used — so the
  morning arrived at 52: four points over the 48 target, seven over the
  threshold.
- Counterfactual on the night's own numbers: the same window run at 1602 W
  needs an end SOC of 82.8% (48 plus 6h31m of drain), not 88.54 — 5.7 points,
  1.7 kWh of battery, 55 minutes of window at the implied 1869 W rate, about
  2.0 kWh of grid — and it would have arrived at 48.0% at 06:37. Unlike
  2026-11-21 this night was not clamp-bound, so the whole gap is the load
  estimate. Where 1866 W came from: the all-day hourly medians over 00:00-06:37
  average 1671 W, and the weekday/weekend split lifts the profile's own
  12-hour night about 6% (20.5 to 21.8 kWh), which is most of the distance to
  1866 W; the night then also ran below even the all-day medians (03:00-04:00
  at 1200 W), so the uplift and a quiet night compounded.
- Not evidence on the horizon itself: the cut started at 56%, eleven points
  above the threshold, because the 07:00 pre-cut job held on a 4611 W transient
  sample (N26); sunrise was 52. On the overcast branch this is the fourth
  consecutive PV-positive cut — the forecast was rainy/cloudy (cloud 100%, rain
  0.5 mm) yet PV ran 2200 W at 08:09 and 2862 W at 08:24 against 1.2-1.4 kW of
  load, and SOC rose 56 to 58 through the cut's first 25 minutes — so it still
  does not answer whether the horizon should scale with the category.
- Gate count: two hour-of-day-profile nights now point the same way (2026-11-21
  arrival +3.5 points, clamp-bound with no avoidable charge; tonight +4 points
  with 55 minutes avoidable) against the band's 2026-11-22 (+12 points, 33
  minutes), so the three-comparable-outcome gate stands at 2/3 and no basis
  change is proposed.
- Steps: read the next two normal mornings to confirm the cut-start arithmetic
  (target met, cut starts 40 plus or minus a point). Then read an overcast
  cut-morning: whether SOC drains through the cut with PV below load for hours,
  and whether the last pre-cut hold covered that. Only then choose between a
  category-scaled horizon (rainy/cloudy +90), a lower normal threshold, or
  leaving both alone.
- Done when: a normal cut-morning is confirmed within a point of the threshold
  with the target met, a cloudy cut-morning is read, and the owner decides
  whether any horizon offset is needed.

### N24. The pre-cut projection basis: read all three, keep the sample

- Status: answered, read-only watch. No basis change is justified on the
  evidence below, so nothing here moves mode timing; reopen only if a second
  false hold or any missed hold appears.
- Why it was opened: `projected_soc_at_outage_start` drains linearly from the
  single `outPutPower` reading and the minutes to the cut. On 2026-11-21 the
  same formula produced 36.35% at 06:00 (48% live, implying about 1747 W over
  the 120 minutes) and 41% at 06:30 (46% live, 945 W), so the 06:00 job
  deferred and the rest cleared. That looked like one noisy input deciding the
  morning.
- Measurement, 2026-11-21: every pre-cut job row on the four cut mornings with
  readings, re-run under three bases — the recorded `load_w` sample, the
  current hourly-profile medians (1515 W at 06:00, 1392 W at 07:00), and the
  net `discharge_w` (which already carries the PV offset) — against the SOC at
  the first cut reading. Last pre-cut job of each morning:

  | Morning | Threshold | Live | Sample | Profile | Discharge | Arrival |
  |---|---|---|---|---|---|---|
  | 2026-11-18 (06:30, last job that ran) | 45 | 42 | 34.0 | 34.8 | 31.7 | 41 |
  | 2026-11-19 (07:00) | 40 | 42 | 39.0 | 37.4 | 39.6 | 44 |
  | 2026-11-20 (07:00) | 40 | 56 | 52.0 | 51.4 | 51.9 | 55 |
  | 2026-11-21 (07:00) | 40 | 45 | 41.0 | 40.4 | 41.9 | 44 |
  | 2026-11-22 (07:00) | 40 | 44 | 38.1 | 39.4 | 38.3 | 44 |

- What it says: the three bases never diverge by more than 3.5 points and the
  mean bias at the last job is 4.5 points pessimistic for the sample, 5.0 for
  the profile and 4.7 for net discharge — within a point of each other on four
  mornings. The profile, which the task proposed, is the *worst* of the three,
  and the shipped sample basis is not the weakest. All three lose to the PV
  that rises between the last job and the cut — on 2026-11-20 from 171 W at
  06:58 to 2639 W at 08:01 against a 1.3 kW house — which no instantaneous
  input can see. The lever that fixes that is the deferral, not the input, and
  it is already shipped (N20).
- The one false hold on record predates it: 2026-11-19 07:00 projected 39.0%
  against a 40% threshold with a 152 W PV reading and switched, for a cut that
  started at 44%. That is about 1 kWh of grid on a morning where four jobs
  switched; the deferral would have sent the 06:00, 06:30 and 06:40 jobs to the
  07:00 re-decision, but the 07:00 sample was pessimistic on its own.
- Watch read, 2026-11-22: the 07:00 job projected 38.1% against a 40%
  threshold and switched, and the cut started at 43-44%, which is the surface
  shape of a second false hold. It is not one: the hold's own 16-minute charge
  supplied 2 of those points, and the no-hold counterfactual — 44% draining
  about 1.2 points in the 16 minutes the charge masked, then the measured 4
  points/h to the cut — arrives at about 40%, on the threshold. The sample was
  about 2 points pessimistic against that basis, better than the 4.5-point mean
  above, so the watch condition (second false hold, or an arrival below the
  threshold after the last job) did not fire and the basis stays as shipped.
  The fifth row's `Arrival` column carries the hold's 2 points, like the 11-19
  row; the counterfactual, not that column, is the error measure.
- Watch fired, 2026-11-23: the 07:00 job, the last before the cut, held on a
  4611 W sample — 2.7 times the 07:00 hour's 80th percentile — and the cut
  started at 56%. The three bases diverge by ten points on that morning rather
  than one, so the input question is open again; it is now N26. The table above
  still answers the PV-positive case with a plausible sample.
- Steps: none now. If a second false hold appears at a last pre-cut job, or any
  morning arrives below the threshold after its last job cleared it, read that
  morning against this table before proposing anything — and prefer widening
  the deferral to the cut hour over re-basing the input.
- Done when: closed as answered; the watch condition fired on 2026-11-23 and
  the input question continues as N26.

### N26. Keep one transient load sample from deciding the pre-cut hold

- Status: shipped 2026-11-23 (PR #138), now a read-only watch for the next two
  cut mornings. Option 3 was chosen and merged at the owner's direction.
- Why: the pre-cut projection drains a single `outPutPower` reading to the
  scheduled cut. On 2026-11-23 the 06:40 job projected 45.0% at the 08:00 cut
  from a 1508 W sample and cleared; twenty minutes later the 07:00 job, the
  last before the cut, projected 34.6% from a 4611 W sample and switched, with
  a hold target of 60.33% — the 45% threshold plus the 15.3-point projected
  drain. The hold closed `topup-expired` at 07:55 at 56%: 55 minutes, six
  points, about 2.0 kWh of grid. Sustained load around both jobs was
  1487-1519 W (the hour's median is 1328 W, its 20-80 band 1060-1697 W), so
  4611 W was a transient. The no-hold counterfactual is 50% draining about
  1500 W for the hour to the 08:00 cut, i.e. 45.0% — on the threshold, which is
  the arrival the hold exists to produce. The cut started at 56% instead.
- Why the N24 table did not cover it: those four mornings had all three bases
  pessimistic for one shared reason — PV rising between the last job and the
  cut, invisible to every instantaneous input. A spike is a different failure:
  one sample wrong by 3x, not a bias in a mean. On this morning the profile
  (1328 W to 45.6%), net discharge (1495 W to 45.0%) and the 06:40
  sample (1508 W to 45.0%) all cleared; only the 07:00 sample held.
- Cost: the first false hold (2026-11-19, 39.0% projected against 40) spent
  about 1 kWh; this one spent about 2.0 kWh and bought 11 points of cut-start
  margin the threshold did not need.
- Options, one input per PR, owner picks:
  1. Clamp the sample to the job hour's 20-80 percentile band before
     projecting. Bounded and keeps the shipped basis elsewhere, but silently
     caps a genuinely high-load hour.
  2. Project from the median of the last three 15-minute dashboard samples
     instead of one reading. Filters spikes, and the PV blindness that the
     4.5-point mean bias comes from stays as it is.
  3. Fall back to the hourly-profile median only when the sample exceeds the
     hour's 80th percentile by more than 50%. Narrowest: it fires only on the
     shape that misfired.
- Decision, 2026-11-23: option 3, the narrowest and the only one-sided one.
  `pre_cut_load_basis` replaces the reading with the current hour's profile
  median only when the sample exceeds 1.5 times that hour's 80th percentile
  (2546 W at 07:00); every other case — a quiet reading, an hour without three
  days, a missing band, a reading inside the ratio, or any read failure —
  keeps today's behaviour exactly. It adds no call and no new failure mode to
  the decision: the rows are the local snapshot file the top-up planner already
  reads, and the substitution is written into the audit note beside the
  projection. Against this morning it projects 45.6% at 07:00 instead of 34.6,
  clears, and the cut starts at about 45 rather than 56.
  Option 1 was rejected as broader without being sufficient: clamping at the
  80th percentile still projects 44.3% at 07:00, so it would have held today
  anyway while also touching every ordinary high-load morning. Option 2 was
  rejected as two-sided and coupled: a median of three readings filters a
  genuinely rising load back towards older, lighter samples, and it makes a
  mode decision depend on the dashboard refresh cadence, so a stale snapshot
  file would project from hours-old readings.
- Shipped and verified on deploy, 2026-11-23: replaying the same 07:00 decision
  against the production snapshot history reads 1368 W and 45.4% at the cut for
  the 4611 W sample (2.8x that hour's 80th percentile of 1648 W), while 1508 W
  and 1600 W pass through untouched at 45.0% and 44.7%. The cut that morning
  would have started near 45% instead of 56%, without the 55-minute hold.
- The shape is not new to the repo: `_planning_load_w` already refuses to let a
  live reading raise a night plan above the learned load, so the pre-cut
  projection was the one consumer of a raw `outPutPower` reading left
  unbounded. Option 3 follows that precedent — fall back to the learned hour
  median — rather than inventing a clamp the night plan does not use.
- Watch: the next two cut mornings, one quiet and one PV-positive. The two
  tests added with the option cover the shape: a 4611 W reading leaves SBU and
  audits the substitution, a 1600 W reading still switches. The audit note
  names the median, the sample and the ratio whenever it fires, so the read
  needs no extra data.
- Done when: two cut mornings show the last pre-cut job's projection within
  three points of the arrival, and a morning whose no-hold path starts at or
  above the threshold sees no hold at all.

## Current Baseline

The following capabilities are implemented and should be preserved:

- Cloud-safe morning and afternoon preservation, verified SBU returns,
  watchdog checks, health reporting, and conditional night auto-topup. A
  projection-triggered preserve defers to the last scheduled job before the
  cut, which sizes its hold to the projected drain so the outage starts at
  the threshold with one acting window per morning. Confirmed live on
  2026-11-22: three deferrals, one 16-minute Utility window, two mode writes,
  and a cut at 43-44% against the 40% threshold. Repeated on 2026-11-23
  (rainy/cloudy, 45% threshold): deferrals at 06:00 and 06:30, a clear at 06:40
  projecting exactly 45.0, then a 55-minute hold started at 07:00 by a 4611 W
  transient sample that took the cut start to 56% (N26).
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
  reconciliation, same-time insights, and schema-versioned JSON output. The
  Tonight card projects from a PV-aware sunset SOC whenever the day's
  remaining PV is known, says it is estimating later rather than publishing a
  whole daylight day of load before noon, labels the all-day figure a stress
  estimate, and never sizes a top-up past the battery's headroom (N21, closed
  2026-11-21; the same morning read 9h 44m for 15 kWh on a 44% battery, now
  under two hours from the sunset estimate). The Now tab's alert rows render
  from `dashboard.json` alone and were confirmed against a live one on
  2026-11-22, a `Top-up running` row reading `Owned Utility hold · returning to
  SBU at 46% · ETA 45m` for `automation.utility_hold_detail` (N11, closed
  2026-11-22).
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
- A Now-tab "Solar surplus" row while the battery is full and PV is pinned to
  the load across consecutive refreshes, naming the capped window and the
  day's peak (N23, closed 2026-11-21).
- Capped minutes and peak PV on every finalized day in the daily generation
  ledger, and a 21:00 daily-summary line setting them against the day-ahead
  forecast, so a full-battery day reads apart from a cloudy one without the
  dashboard (N25, closed 2026-11-21). The pin tolerance is the larger of 100 W
  and 5% of load: on 2026-11-20 a 3.7 kW load read 170 W under PV while pinned.
  Re-measured on the retained week, 2026-11-20 is the only capped day (89 min,
  peak 5.4 kW) and it generated 30.9 kWh against a 31.4 kWh forecast, so the
  cap cost little that day; the earlier 24.7-vs-40.1 comparison was a
  mid-afternoon total against a pace, not a weak day.
- A shadow hour-of-day load profile (`load-profile`) compared with the band
  planner's night energy.
- A separate morning-preservation scorecard that models the observed no-Utility
  SOC path, fails closed on incomplete metric windows, and gates the opt-in
  hourly-irradiance solar bridge behind three comparable post-change shadow
  outcomes.

## Priority Queue

Work from the top unless live operational evidence justifies reordering.
P2 and P3 were retired on 2026-11-23 as not wanted; their identifiers stay
unused so older notes that cite them keep pointing at the same work.

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

Current concrete steps toward this goal are in Next Up: none for the hold.
N20 closed on 2026-11-22 with the last pre-cut hold's first live run. The
06:00, 06:30 and 06:40 preserve jobs each deferred to the next one, projecting
39.22, 38.65 and 38.11 against the 40% threshold; the 07:00 job, the last
before the cut, switched once at 44% on a 38.0% projection and sized its hold
to 45.86% — the threshold plus the 5.86-point projected drain; the 07:16
completion check closed it at 46% after 16 minutes and the return-sbu switched
back to SBU; the 07:55 return-sbu found SBU already at 44% and the 08:01
watchdog verified at 43%, so the cut started at 43-44%. Two mode writes for the
morning against the 2026-11-19 baseline's eight, one Utility window instead of
four, about 0.56 kWh of grid, and a no-hold counterfactual of about 40% — so
the switch carried the cut three to four points clear of the threshold, and the
projection was about 2 points pessimistic, better than N24's 4.5-point mean.
The deferral had already earned 2026-11-21 three re-decisions and zero writes.
The projection-basis question (N24) was answered on 2026-11-21: the
sample, the hourly profile and net discharge land within a point of each other
on four cut mornings, so the input stays as shipped. N2 closed on 2026-11-19
with the verdict "fewer repeat top-ups": three banded weekday nights each ran
two starts against the 2026-11-09 baseline of three, and the band-vs-live
direction proved unstable, so no live-load cap was added. N19 closed the same
day: the first outage morning after the change started its cut at 43-44%
against the 40% preserve threshold, the first read morning above the line. The
learned charge rate went live on 2026-11-10 at the owner's direction, ahead of
the three-comparable-outcome gate; N1 collected those outcomes after the fact
and closed on 2026-11-13 with the repeats still present, which is why N2 owned
them. N12 closed on 2026-11-18 with three of five outage mornings starting
below the preserve threshold, so the sunrise-target change returned as N19.
N17 closed on 2026-11-20 at the owner's direction ("fix the estimate first",
threshold left at 40): `planning_night_load` prefers the night's hour-of-day
profile (1827 W at the 18:00 horizon against the band's 2028 W, about 1750 W
at 00:00 against 1990 W) and falls back to the bands when any hour lacks three
days, so weekend nights keep the bands until they qualify. They qualified on
2026-11-22 — four weekend dates inside the 8-day window where the 18:00 plan
had two — so tonight's 18:00 plan is the first weekend night sized by the
hour-of-day profile, and N22 holds the one-night comparison against the band
it replaces. The gain is
bounded by the full-charge clamp: an evening window whose completion target is
already 100% ends at the same SOC, and the saving lands on post-midnight windows
(86% to 81% on the 11-19 night, about 1.6 kWh of that window's 1.8 kWh). N16
closed on 2026-11-21 with a week of observed supply windows read from
`supply-history` (507 readings since 2026-11-17T17:36, about one every 10
minutes): eight readable windows across five cut days, every one a cut, and no
`unscheduled-cut`. Every start edge sits within one refresh interval of the
scheduled start (widest 07:55-08:11 against 08:00), and so does every end edge
except one: five of the six windows read on both sides close within a refresh
interval (widest 10:52-11:11 against a scheduled 11:00). The sixth is the 11-20
afternoon window, scheduled to 18:00 and restored between 15:37-15:52 at 97%
SOC: the estate handed supply back two hours early, no job runs after 14:05 that
day so nothing acted on it, and the profile keeps the later end because an
early estate return costs nothing while a profile ending at 15:45 would stop
covering the days the estate runs to 18:00. The 11-21 morning window was still
open at the read. Morning cut starts rose from 39-41% on the N12 mornings to
44-55% after N19 and N17, and read 43-44% again on 2026-11-22 with N20's hold
carrying the pre-cut hour. The `supply-history` caveat stands: it labels every
past day with the profile active now, so read an old edge against the profile
of that morning.

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
- Introduce a notification-channel abstraction only when a second channel is
  actually wanted.

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
