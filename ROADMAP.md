# Growatt Guard Roadmap

Last reviewed: 2026-11-28

This is the working backlog for Growatt Guard. Priorities favor inverter safety,
low Growatt API pressure, trustworthy decisions, and clear operations before
convenience or larger product ideas.

## Next Up

This is the handoff queue: concrete tasks any agent can pick up without the
conversation that produced them. `AGENTS.md` explains how to use it. Each task
carries its evidence, the steps, and what "done" means. Remove a task when it
is finished and record anything it uncovered as a new task, an entry under
Settled Decisions (answered questions) or one under Standing Watches (shipped
acceptance conditions). Keep entries public-safe: no IDs, serials, domains, or
credentials.

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
- Read, 2026-11-24: still no `topup-complete-deferred` row, and this morning is
  the closest the condition has come. The 06:30 job switched with the live SOC
  at 36% already under the 40% threshold, so the hold took the bare threshold
  as its target (`soc >= threshold` is the branch that adds the headroom),
  completed `topup-target-reached` at 40% at 07:06 inside the preserve window,
  and `_defer_completion_to_scheduled_return` returned SBU because 40 >= 40.
  The 54 minutes on battery to the cut took the 08:00 start to 38%. Deferring
  to the 07:55 return-sbu instead would hold Utility about 49 minutes for
  roughly 1.1 kWh of grid to keep 0.6 kWh (2 points), and PV was 2000 W against
  1003 W of load two minutes into the cut, so the two points cost nothing. The
  guard stays as shipped.
- Read, 2026-11-25: still no `topup-complete-deferred` row. Both pre-cut holds
  (`06:40` and `07:00`) started with live SOC at 39% under the 40% threshold,
  took the bare 40% target, completed `topup-target-reached` at 40% (`06:51`
  after 11 min, `07:06` after 6 min), and returned SBU because `40 >= 40`. The
  repeat short-cycling between those two jobs is tracked as N29.
- Read, 2026-11-26: still no `topup-complete-deferred` row. The only pre-cut
  hold (`07:00`, live `41%`, target `44.66%`) closed `topup-target-reached` at
  `07:32` at `45%`, inside the preserve window but above its `40%` threshold,
  so the deferral condition never applied; the `07:32:09` `return-sbu`
  switched to SBU and the `07:55` one found SBU already.
- Read, 2026-11-27: still no `topup-complete-deferred` row. Zero morning holds
  ran (`06:00`, `06:40`, and `07:00` cleared at `35%` sunny; `06:30` deferred),
  and last night's `18:00` top-up closed `topup-target-reached` at `23:43` at
  `83%`, outside any preserve window.
- Read, 2026-11-28: still no `topup-complete-deferred` row. Zero morning holds
  ran (`06:00`–`07:00` all cleared at `65–68%` against `45%` rainy/cloudy), and
  last night's `18:00` top-up closed `topup-target-reached` at `21:22` at `99%`,
  outside any preserve window.

### N30. Evaluate lowering dry-season `normal` preserve threshold from 40% to 35%

- Status: waiting on N28's three-evening watch (ready after ~2026-11-28). Needs
  owner go-ahead before merging (alters inverter mode timing).
- Why / Evidence: owner preference (2026-11-25) is comfortable arriving around
  `35–38%` at 08:00 on normal dry-season mornings to buy less overnight grid
  (~`1.5 kWh`/night on unclamped nights) rather than paying for extra morning
  margin, because morning sun routinely takes over by ~08:00. Eight consecutive
  measured cut mornings through 2026-11-25 (including a 100% cloud forecast on
  2026-11-23) were PV-positive at or right after 08:00. With N28 now reading
  tomorrow morning's weather window directly, `DRY_SEASON_THRESHOLDS["normal"]`
  (`40%`) actually governs unclamped evening plans instead of clear-evening
  `35%` noise; wait until N28's three-evening watch settles so the baseline is
  clean before changing a threshold.
- Steps:
  1. During the morning reads over N28's three-evening watch, record sunrise
     SOC, 08:00 SOC, morning hold count (N29), and PV crossover time.
  2. Once N28 has three clean reads, present the counterfactual (expected 08:00
     arrival and grid saved if `DRY_SEASON_THRESHOLDS["normal"]` moves from
     `40.0` to `35.0`, keeping `rainy/cloudy` at `45.0`) and ask the owner for
     go-ahead.
  3. On go-ahead, change `DRY_SEASON_THRESHOLDS["normal"]` in
     `growatt_guard/weather.py` in a single PR with updated unit tests.
- Step 1 record, 2026-11-26 (N28 evening 1 of 3, normal `40%`): sunrise
  (`06:39`) SOC `44%` at `06:41`; `08:00` SOC `45%` after one hold; morning hold
  count `1` (`07:00`–`07:32`, two mode writes); PV crossed load between `07:41`
  (`1437 W` vs `1800 W`) and `07:56` (`1887 W` vs `1135 W`). No-hold
  counterfactual at `08:00` is about `38.6%` (see the N26 read), so on this
  morning a `35%` normal threshold would have cleared the `07:00` job and saved
  the hold (about `2 kWh` of grid) as well as roughly `1.5 kWh` of evening
  charge.
- Step 1 record, 2026-11-27 (not counted — sunny `35%` under N31): sunrise
  (`06:39`) SOC `45%` at `06:36`; `08:00` SOC `40%` with `0` morning holds; max
  cloud at `07:00` was `31%` (`0.2 mm` rain), so N31 classified the morning as
  `sunny`, not part-cloud `normal`. Clouds thickened after the cut (`08:28`
  health check read `normal` at `60%` max cloud, `0.3 mm`), holding PV at
  `534–651 W` below `731–1095 W` load through `08:28` (`39%` SOC).
- Step 1 record, 2026-11-28 (not counted — `rainy/cloudy` `45%`): sunrise
  (`06:40`) SOC `66%` at `06:41`; `08:00` SOC `61%` (`60%` at first off reading
  `08:11`) with `0` morning holds; max cloud at `07:00` was `91%` (`1.0 mm`
  rain). PV stayed at `381–511 W` below `878–1714 W` load through `08:50`
  (`58%` SOC).
- Rescoped, 2026-11-26, after N31: the trace-rain fix already moves the clear
  mornings to the sunny `35%`, so N30 now covers only the part-cloud mornings
  that stay `normal` (cloud `36–69%`). The forecasts at the last pre-cut job
  (`07:00`): 11-17 `34%`/`0.4 mm`, 11-19 `41%`/`0.7`, 11-20 `37%`/`0.5`,
  11-21 `30%`/`0.2`, 11-22 `32%`/`0.3`, 11-23 `100%`/`1.3`, 11-24 `47%`/`0.3`,
  11-25 `45%`/`0.5`, 11-26 `18%`/`0.1`. Under the strict rule none was sunny;
  under N31, 11-17, 21, 22 and 26 are. The ones left are 11-19, 20, 24 and 25,
  and all four were PV-positive by about 08:00. N30's evidence should now
  count only part-cloud mornings, and on those the step-1 record should add
  the max cloud at `07:00` and the PV-crossover time.
- Not taken, 2026-11-26: sizing the N20 pre-cut hold's drain headroom to the
  time left after the hold instead of the full hour to the cut. On 11-26 it
  over-charged by about 3-4 points (target `44.66`, about `41` needed), but
  under N31 that morning has no hold at all, and the saving elsewhere is about
  `0.5 kWh` on the rarer mornings that still hold. Reopen if two post-N31
  mornings hold and the cut starts four or more points above the threshold.
- Done when: either `DRY_SEASON_THRESHOLDS["normal"]` is lowered to `35%` and
  verified over three post-deploy mornings, or the owner decides to keep `40%`
  and records the verdict in `Settled Decisions`.

## Settled Decisions

Answered questions, with the measurements that answered them. Nothing here is
queue work: read one only if its stated reopen condition fires, or if a new
observation contradicts it. Closed items keep their N-numbers so older notes
still resolve.

### N22. End the night-plan horizon at PV takeover, not astronomical sunrise

- Status: **closed 2026-11-24, verdict "no horizon offset"**. Reopen only if a
  cut morning drains through the cut with PV below load for hours, or if a
  normal cut starts more than three points below the threshold with the plan
  target met at sunrise.
- Why the verdict supersedes the original done condition: that condition asked
  for a normal cut-morning inside a point of the threshold with the target met,
  then an overcast cut-morning read. The first half happened twice (2026-11-21
  and 2026-11-22 both started at 44 against a 40% threshold and a 43% morning
  target); the second never happened and now looks unobservable — all seven
  cut mornings read so far were
  PV-positive through the cut, on 2026-11-23 by 2200 W at 08:09 under a 100%
  cloud forecast. The first morning since N19 to start under the threshold
  (2026-11-24 at 38%) missed its target because the pack was full at window
  end and the night load ran 1948 W against 1698 W planned, not because the
  horizon ended too early: at that drain the 48% target needed 107.5% at window
  end. A horizon offset therefore has no case to answer, and the evidence
  below stands as the record.
- Read, 2026-11-24 verdict read (seventh cut morning): the sunrise target was
  unreachable, the cut started 2 points under, and PV covered it within two
  minutes — the fifth consecutive PV-positive cut and the strongest evidence
  that the carry hour, not the horizon, is the right place to spend margin.
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
- Read, 2026-11-24 (seventh cut-morning read, third night sized by the hour-of-day
  profile, normal category at a 40% threshold — the first cut morning since N19
  to start under its threshold with the sunrise target missed): the 18:00 plan
  asked 170 min from 85% to a 100% completion target on a 1698 W load
  (`hourly profile median (weekday, 12.63h)`) at a 1950 W learned rate and
  quoted `sunrise_target_soc=45`; its own arithmetic wanted 103.4% at window
  end, so it clamped. It closed `topup-expired` at 20:51 after 171 min at 99%,
  one point short on a 1474 W implied rate against 1950 planned (24% taper).
  The night then drained at a mean 1948 W — 98% at 21:00 to 41% at 05:47 is 57
  points in 8.78 h, and the sampled `discharge_w` mean 1996 W, median 1950 W —
  against the 1698 W planned, so sunrise read about 36% against the 43.5 the
  same window should have produced and the 48 target it sized to. The plan was
  12.8% low on load (1948 W measured against 1698 W), which cost 7.5 of the
  twelve missing points. Counterfactual on the night's own
  numbers: the same plan at 1948 W asks 208 min (window to 21:28) and still
  clamps at 100%, because a 48% arrival needs 107.5% at window end, and arrives
  40.5% at 06:37 — so a correct estimate closes about four and a half points and
  the remaining 7.5 are the 100% ceiling, not the horizon. Arriving at 48 from
  an 18:00 start on a 1948 W night would need the window held to about 22:38
  (278 min, about 4 kWh more grid than the 171-minute run) to keep 3.6 kWh of
  battery the outage did not need; not proposed.
- This morning says nothing about the horizon itself: the cut started at 38%
  after the 06:30 hold completed on the bare threshold (N5 read), and PV was
  already 2000 W against 1003 W of load at 08:02, charging 911 W, so the two
  missing points cost nothing measurable. The night was capacity-bound, not
  horizon-bound: 12.63 h at 1.95 kW is 24.6 kWh against a 30 kWh pack entered
  at 85%.
- Gate count, 2026-11-24: three comparable hour-of-day-profile outcomes now
  bracket in both directions — the 2026-11-20 night at 1826 W planned against
  about 1568 W measured (+16.4%), the 2026-11-22 night at 1866 W against
  1602 W (+16.5%), and tonight at 1698 W against 1948 W (-12.8%) — while the
  band night of 2026-11-22 read +13.4%. No direction is stable, so no estimate,
  threshold or horizon change is proposed and the gate stays at 3/3 with no
  consistent sign.
- Read, 2026-11-26 (reopen conditions not fired): the 18:00 plan asked 253 min
  from 65% to a `92.33%` completion target on a `1755 W` load (`hourly profile
  median (weekday, 12.65h)`), sizing to `43%` at sunrise (`40` plus 3 headroom),
  and closed `topup-target-reached` at 22:14 at 92% on a 1914 W implied rate
  against 1950 planned — unclamped. The night drained 92% at 22:14 to 44% at
  06:41 (sunrise 06:39), 48 points in 8.45 h, about 1704 W, so the estimate was
  3% high and the arrival landed one point over the plan's 43. This is the
  closest arrival on record and adds a fourth hourly-profile outcome inside the
  existing ±17% spread; no change proposed.
- Read, 2026-11-27 (reopen conditions not fired): the 18:00 plan asked 342 min
  from 47% to an unclamped `83.97%` completion target on a `1769 W` load
  (`hourly profile median (weekday, 12.65h)`), sizing to `43%` at sunrise (`40`
  plus 3 headroom), and closed `topup-target-reached` at 23:43 (`343 min`,
  `83%`, `1890 W` implied rate against `1950` planned). Overnight drain from
  23:43 (`83%`) to sunrise 06:39 (`45%` at 06:36) was 38 points in 6.93 h, about
  `1644 W`, so the estimate was `+7.6%` high and sunrise landed `+2` points over
  the `43%` target (counterfactual at `1644 W`: `81.0%` end SOC, ~28 min / ~1.0
  kWh grid). Fifth hourly-profile outcome inside the existing ±17% spread; no
  change proposed.
- Read, 2026-11-28 (reopen conditions not fired): the 18:00 plan asked `204 min`
  from `76%` to an unclamped `98.1%` completion target on a `1786 W` load
  (`hourly profile median (weekday, 12.67h)`), sizing to `43%` at sunrise (`40`
  plus 3 headroom), and closed `topup-target-reached` at `21:22` (`202 min`,
  `99%`, `2050 W` implied rate against `1950` planned). Overnight drain from
  `21:22` (`99%`) to sunrise `06:40` (`66%` at `06:41`) was `33` points in
  `9.30 h`, about `1065 W`, because `00:00`–`06:40` ran an unusually quiet
  `711–856 W` (well below the 14-day hourly 20th–80th band of `1189–1972 W`), so
  sunrise landed `+23` points over the `43%` target. Counterfactual on the
  night's own post-window drain (`1065 W`): `76%` at `21:22` already covers
  `33` points to `43%`, so the overshoot is a one-off quiet-night load outlier
  rather than a systematic estimate bias; no change proposed. The `08:00` cut
  (off between `07:56`–`08:11`, starting `61%`→`60%`) is the first genuinely
  overcast cut observed (`rainy/cloudy`, `91%` cloud): PV ran `381–511 W`
  against `878–1714 W` load through `08:50` (`58%` SOC, `-3` points in 50 min),
  still 13 points above the `45%` threshold; reopen condition not fired.
- Original steps, closed unfinished: confirm the cut-start arithmetic on two
  more normal mornings, then read an overcast cut-morning and choose between a
  category-scaled horizon (rainy/cloudy +90), a lower normal threshold, or
  leaving both alone. The overcast read never arrived; the answer is "leave
  both alone", and N5 stays the cover for the one morning that started low.

## Standing Watches

Shipped or answered work whose acceptance condition is a live observation, not
a code change. These stay here instead of Next Up because they carry no task
until their watch fires; a firing watch becomes a new N-item with the read that
fired it. Keep the N-numbers so older notes still resolve.

### N24. The pre-cut projection basis: read all three, keep the sample

- Status: answered 2026-11-22, watch standing. No basis change is justified on
  the evidence below, so nothing here moves mode timing; reopen only if a
  second false hold or any missed hold appears — which is what N26 owns.
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
- Watch read, 2026-11-24: neither watch condition fired — no second false hold
  and no arrival below the threshold after a last job that cleared — so the
  basis stays. The 06:30 job, the last that decided, sampled 1958 W against the
  06:00 hour's 80th percentile of 1835 W and projected 26.1% at the 08:00 cut
  from a live 36%: 1958 W for the 90 minutes to the cut is 9.8 points, so the
  projection matched the no-hold arithmetic and the hold it started is what put
  the cut at 38% (N5 read). The profile basis (1545 W at
  06:00 → 28.3%) and net discharge (about 2000 W → 26.0%) also cleared the
  threshold, so all three agreed in the switch direction — the first such
  morning after the four where the three bases agreed in the clear direction.
- Watch read, 2026-11-25: neither watch condition fired — no false hold (both
  `06:40` and `07:00` read live `39%`, already under the `40%` threshold) and no
  under-threshold arrival after a last job that cleared. At `07:00` (the last
  pre-cut job), the live sample (`1641 W`) projected `34%` at `08:00` from `39%`
  against a ~`37%` no-hold counterfactual (actual `38%` after the 6-minute `+1%`
  hold, with PV crossing over at `07:55` at `1597 W` against `1646 W` load) —
  3.0 points pessimistic, inside the 4.5-point historical mean; the `07:00`
  hourly profile median (`1400 W` → `34.3%`) agreed within a point.
- Steps: none now. If a second false hold appears at a last pre-cut job, or any
  morning arrives below the threshold after its last job cleared it, read that
  morning against this table before proposing anything — and prefer widening
  the deferral to the cut hour over re-basing the input.
- Done when: closed as answered; the watch condition fired on 2026-11-23 and
  the input question continues as N26.

### N26. Keep one transient load sample from deciding the pre-cut hold

- Status: shipped 2026-11-23 (PR #138), **watch satisfied and closed 2026-11-27**.
  Option 3 was chosen and merged at the owner's direction; both clauses of the
  done condition are now met.
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
- Watch read, 2026-11-24: first of the two cut mornings, and the clamp was not
  exercised. The 06:30 job, the only one before the cut that decided (06:40 was
  `lock-skip`ped, and the 07:00 one found Utility already), sampled 1958 W
  against the 06:00 hour's 80th percentile of 1835 W — ratio 1.07 against the
  1.5 threshold — so `pre_cut_load_basis` kept the reading and the audit note
  carries no substitution. It switched once and its 36-minute hold completed
  `topup-target-reached` at 40%, 4 points above the 36% it started from.
- Watch read, 2026-11-25: second cut morning since deploy, and the 1.5x clamp
  was again not exercised (`06:40` sampled `1824 W` against the 06:00 hour's
  80th percentile `1835 W`, ratio `0.99`; `07:00` sampled `1641 W` against the
  07:00 hour's 80th percentile `1722 W`, ratio `0.95` — no substitution in
  either audit note). The `07:00` last pre-cut job projected `34%` at `08:00`
  against a ~`37%` no-hold counterfactual (3.0 points pessimistic; actual `38%`
  at `08:00` after the 6-minute hold, while the estate cut itself started late
  between `09:10–09:25` at `46–47%` SOC under `3.5–4.4 kW` PV; yesterday's
  `15:00–18:00` supply stayed on all window). Because both post-deploy mornings
  started below the `40%` threshold, the second clause of the done condition
  ("a morning whose no-hold path starts at or above the threshold sees no hold
  at all") has not been observed yet; watch stays standing.
- Watch read, 2026-11-26: the clamp fired for the first time in production. The
  `07:00` job (last pre-cut, live `41%`) read `3316 W`, 1.9x the 07:00 hour's
  80th percentile `1722 W`, and `pre_cut_load_basis` substituted the `1400 W`
  median, as the audit note records. It still projected `36%` at the cut and
  switched, so the clamp did not change the decision. The spike was real but
  short: the `06:56` snapshot read `3316 W` and the `07:11` one `1695 W`, and it
  had already cost about two points between `06:41` (44%) and `07:00` (41%).
  No-hold counterfactual from the snapshots: `41%` at `07:00`, net drain
  load minus PV of about `1.4`, `1.2`, `0.4` and `0` kW over the four quarter
  hours to `08:00` (PV `314` → `673` → `1437` → `1887 W`), about `0.73 kWh` or
  2.4 points, so about `38.6%` at the cut — under the threshold, so the hold was
  justified and the `36%` projection was 2.6 points pessimistic, the second
  morning inside three points (`3.0` on 11-25). The hold took `41%` to `45%` in
  32 minutes. Clause 1 of the done condition is met; clause 2 (no hold when the
  no-hold path clears the threshold) is still unobserved, so the watch stays.
- Watch read, 2026-11-27 (watch satisfied): the `1.5x` clamp was not exercised
  (`06:30` sampled `2173 W` against `1835 W` 80th percentile, ratio `1.18`,
  deferred by N20; `07:00` last pre-cut job sampled `770 W` against `1614 W`,
  ratio `0.48`). `07:00` projected `41%` against `40%` actual cut start (`1.0`
  point error — third consecutive cut morning inside three points after `3.0` on
  11-25 and `2.6` on 11-26), and the morning's no-hold path cleared the `35%`
  sunny threshold (and even `40%` normal) with zero holds. Both clauses of the
  done condition are now met; watch closed.
- Done when: two cut mornings show the last pre-cut job's projection within
  three points of the arrival, and a morning whose no-hold path starts at or
  above the threshold sees no hold at all.

### N28. Evaluate tomorrow morning's weather window in `next_morning_preserve_target_soc`

- Status: shipped 2026-11-25 at the owner's direction, watch standing for the
  next three evening plans.
- Why: `next_morning_preserve_target_soc` (`growatt_guard/topup.py`) called
  `choose_preserve_threshold(config, today=morning.date())`, which forwarded
  `today` only to `current_season` and ran `analyze_weather_window` against
  `dt.datetime.now()` (18:00–22:00 evening sky at the 4-hour default) instead
  of tomorrow morning's preserve window. Five evening plans quoted
  `morning_target_soc=35` ahead of a normal `40%` morning (2026-11-19 through
  11-22 and 2026-11-24), and 2026-11-23 evening quoted `45%` ahead of a normal
  `40%` morning. On 2026-11-24 18:00 the plan was not clamp-bound (`155 min`,
  `80%` to `96.72%`, `1753 W` planned vs `1728 W` SOC-implied, `1950 W` vs
  `1962 W` implied rate) and hit its `38%` target (`39–40%` at sunrise), landing
  one point under the morning's `40%` threshold and forcing two morning holds;
  sizing to `40` (`43%` target) asks `179 min` (`99.38%` end SOC) and arrives
  at `43.8%` with zero morning holds.
- Shipped: `analyze_weather_window` and `choose_preserve_threshold` accept an
  optional `window_start: dt.datetime | None = None` (normalized to
  `weather_timezone` and failing closed to `unavailable` if the forecast omits
  that window), and `next_morning_preserve_target_soc` passes
  `window_start=morning`. Live morning callers keep `window_start=None`.
- Done when: three post-deploy evening `auto-topup-check` plans quote a
  `morning_target_soc` matching the following morning's weather category
  (outside genuine overnight forecast shifts).
- Watch read 1 of 3, 2026-11-26: the 2026-11-25 18:00 plan quoted
  `morning_target_soc=40`, and every preserve row from `06:00` to `07:00` used
  `normal` at `40%`, so it matched. The midday category has flipped again (the
  `08:10` health check reads sunny, max cloud 11%, dry-season `35%`), which is
  the 2026-11-20 shape; read the morning's category from its audit rows, not a
  later dashboard.
- Watch read, 2026-11-27 (overnight boundary shift; still 1 of 3 clean matches):
  the `2026-11-26 18:00` plan (running post-N31, deployed `08:32`) quoted
  `morning_target_soc=40` (`normal`), while `06:00`–`07:00` preserve rows read
  `sunny` (`35%`, max cloud `31–34%`, rain `0.1–0.2 mm`) and `08:28` flipped
  back to `normal` (`40%`, max cloud `60%`, rain `0.3 mm`). Because N31 was
  already live at `18:00`, this was a genuine overnight forecast shift right on
  the `35%` cloud boundary rather than a window-start mismatch; wait for two
  more clean matching evenings.
- Watch read, 2026-11-28 (second consecutive overnight forecast shift; still 1
  of 3 clean matches): the `2026-11-27 18:00` plan quoted
  `morning_target_soc=40` (`normal`), while `06:00`–`07:00` preserve rows read
  `rainy/cloudy` (`45%`, max cloud `91%`, rain `0.4–1.0 mm`, rising to `1.5 mm`
  at `08:50`). Wait for two more clean matching evenings.

### N29. Stop repeat morning short-cycling when live SOC enters a preserve job under the threshold

- Status: shipped 2026-11-25 at the owner's direction, watch standing for the
  next cut morning that enters a preserve job under its threshold.
- Why: on 2026-11-25 (normal, `40%` threshold), `06:40` read live `39%`,
  switched to Utility targeting bare `40%`, completed at `06:51` (`11 min`) and
  returned SBU; nine minutes later `07:00` read `39%` again, switched to Utility
  a second time targeting bare `40%`, completed at `07:06` (`6 min`) and
  returned SBU again — four mode writes in 26 minutes for `+1%` twice, still
  draining to `38%` by `07:55`/`08:01`.
- Choice (Option 2 over Option 1): `_record_preserve_utility_hold`
  (`growatt_guard/modes.py`) now always adds `auto_topup_headroom_soc` (`+3`
  points -> `43%` on a `40%` threshold) even when `soc < threshold`, while
  keeping `drain_soc` gated on `soc >= threshold` (N20's deferred last-job
  branch). Simulating both options on the `2026-11-24` (`06:30`, `36%`,
  `drain=9.8%`) and `2026-11-25` (`06:40`, `39%`, `drain=8.1%`) mornings showed
  Option 1 (`threshold + drain_soc`) would hold Utility all the way to `07:55`
  and over-charge to `48–50%`, whereas Option 2 (`43%`) finishes around `07:13`
  to `07:33` (past the `07:00` job, one hold / two mode writes) and arrives at
  `~41–42%` at `08:00`.
- Done when: a morning entering a preserve job under the threshold runs at most
  one Utility hold before the cut (two mode writes, not four) and arrives at or
  above the threshold at the scheduled cut time.
- Watch read, 2026-11-26: not exercised. Every pre-cut job read live SOC at or
  above `40%` (48, 45, 44, 41), so the `07:00` hold took the N20 branch with
  `max(3, drain 4.66)` as headroom (target `44.66%`). It was still one hold and
  two mode writes, arriving at `45%`.
- Watch read, 2026-11-27: not exercised. Every pre-cut job read live SOC well
  above `35%` (`48`, `45`, `45`, `44`) and zero holds ran.
- Watch read, 2026-11-28: not exercised. Every pre-cut job read live SOC well
  above `45%` (`68`, `66`, `66`, `65`) and zero holds ran.

### N31. Count trace forecast rain under a clear sky as sunny

- Status: shipped 2026-11-26 at the owner's direction, watch standing for the
  next three clear mornings with trace rain in the forecast.
- Why: `analyze_weather_window` called a window sunny only on `total_rain == 0`.
  On 2026-11-26 the 06:00-10:00 forecast was 5-27% cloud with `0.1 mm` at 06:00
  and 08:00, so 0.2 mm made it `normal` (`40%`) instead of sunny (dry-season
  `35%`). By 08:10 the window had moved past the rain hours and read sunny. At
  `35%` the `07:00` job's `36%` projection clears: no hold (about `2 kWh` of
  grid), and the cut starts near the `38.6%` no-hold counterfactual instead of
  `45%`.
- Shipped: `WEATHER_SUNNY_MAX_RAIN_MM` (default `0.5`) is the total forecast
  rain over the lookahead that still counts as sunny. The cloud limit
  (`WEATHER_SUNNY_THRESHOLD`) and the rainy/cloudy branch (`1 mm` or `70%`) are
  unchanged, and `0` restores the strict rule. Because the evening plan reads
  the same function for tomorrow's window (N28), trace-rain clear mornings now
  also plan to the sunny target (`35 + 3`) rather than `40 + 3`.
- Done when: three cut mornings whose forecast carried `0 < rain <= 0.5 mm` under
  the sunny cloud limit used the sunny threshold in their preserve rows and
  started the cut at or above it, or one such morning starts the cut below the
  sunny threshold with PV below load. That second case reopens this and N30.
- Watch read 1 of 3, 2026-11-27: first live fire. All four pre-cut jobs
  (`06:00`–`07:00`) carried trace rain under the `35%` cloud limit (`max cloud
  34%, rain 0.1 mm` at `06:00`–`06:40`; `31%, 0.2 mm` at `07:00`), used the
  `35%` sunny threshold, ran zero holds (`06:30` deferred cleanly on a `2173 W`
  reading), and started the `08:00` cut at `40%` (`5` points above `35%`).
  Clouds thickened after the cut (`08:28` read `normal`, `60%` max cloud,
  `0.3 mm`), holding PV at `534–651 W` below `731–1095 W` load through `08:28`
  (`39%` SOC), still four points above `35%`, so the reopen condition did not
  fire.

### N32. Read 14 days of snapshot history for the hourly load profile so Saturdays qualify

- Status: shipped 2026-11-27 at the owner's direction, watch standing for the
  next Saturday evening plan (`2026-11-28 18:00`).
- Why: `planning_night_load` requires `MIN_PROFILE_DAYS = 3` nights per hour,
  and `weekend` is Saturday–Sunday (2 nights/week). Readers were passing
  `DASHBOARD_METRICS_RETENTION_DAYS = 8` (the dashboard chart's display window)
  even though `DASHBOARD_METRICS_KEEP_DAYS = 90` retains 90 days on disk. In an
  8-day window, Saturday `18:00` can only ever see 2 prior weekend nights, so
  every Saturday night fell back to the band planner (~`2033 W`), while Sunday
  `18:00` barely qualified by catching 3 nights. On live production snapshots on
  2026-11-27, widening to 14 days gives 4 weekend nights (`1816 W` vs ~`2033 W`
  band) while moving weekdays by `+1.2%` (`1787 W` vs `1766 W`, 10 days).
- Shipped: `LOAD_PROFILE_HISTORY_DAYS = 14` in `growatt_guard/load_profile.py`,
  used by `topup.py`, `load_profile.py`, `modes.py`, and `dashboard_service.py`
  / `dashboard_viewmodel.py` (keeping the dashboard's 8-day chart window intact).
- Done when: the next Saturday `18:00` `auto-topup-check` audit row records
  `planned_load_source=hourly profile median (weekend, ...)` instead of falling
  back to the band planner, and `load-profile` shows both weekday and weekend
  hourly totals midweek.
- Pre-test read, 2026-11-28 (release `4e61c92fb3e4` live): `load-profile` now
  shows `14 days` per hour with weekend `18:00` 12h hourly total `21.9 kWh`
  (vs `24.4 kWh` band), and `dashboard.json` `tonight_risk` already quotes
  `hourly profile median (weekend, 12.23h)` (`1832 W`). Tonight's `18:00`
  `auto-topup-check` is the live Saturday acceptance test.

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
  transient sample that took the cut start to 56% (N26). A switch taken on the
  live reading when it is already under the threshold holds to the bare
  threshold instead, so the drain between completion and the cut can take the
  start a point or two below: on 2026-11-24 the 06:30 job switched at 36%,
  completed at 40% at 07:06 and the 08:00 cut started at 38% (N5 read).
- Sunrise planning using a learned hour-of-day night load, falling back to the
  weekday/weekend evening and overnight bands when an hour lacks three days,
  observed charge rates, weather calibration,
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

N22 closed on 2026-11-24 with the verdict "no horizon offset", superseding its
own done condition rather than meeting it. Seven cut mornings were read after
N19 and N17: six started at 43-56% against their thresholds, two of them
(2026-11-21 and 2026-11-22) inside a point with the target met, and every one
of the seven was PV-positive through the cut, so the overcast cut that the
condition waited for never arrived and the drain it feared could not be
observed. The exception is 2026-11-24, the first morning since N19 to start
under the threshold at 38% against 40%, and it was not a horizon miss: the
plan asked 170 min from 85% to a 100% completion target whose own arithmetic
wanted 103.4% at window end, closed `topup-expired` one point short on a 24%
taper, and the night then drained 1948 W against 1698 W planned, so a 48%
arrival needed 107.5% at window end. The estimate gate it fed reached 3/3 with
no consistent sign (+16.4%, +16.5%, -12.8% against measured load), which is a
variance floor rather than a tuning signal, so no estimate, threshold or
horizon input changed. The morning read from here is acceptance monitoring:
N24 and N26 keep their watches under Standing Watches, and N5 stays the only
queue item.

N27 closed on 2026-11-24. "Today Remaining" now multiplies today's full-day
Open-Meteo estimate by the median share of a completed day's PV still to come
at the same clock time (last 14 days, at least three valid days with a late
final sample and a snapshot within 90 minutes), instead of splitting the
remaining irradiance at the flat performance ratio; the current irradiance
hour is prorated and the figure reads 0 after sunset. The evidence that forced
it: at 13:50 the linear split showed 13.3 kWh while 24.2 kWh was already
generated and the previous 12 days produced 4.4–6.6 kWh after 14:00 (median
5.3), and the old sum stepped by a whole bucket at each hour boundary. Replayed
against the 2026-11-22/23 snapshots and that day's cached forecast, the shaped
figure reads 7.0 kWh at 14:10 instead of 12.6. Full-day totals are unchanged;
the change is dashboard presentation only, not mode timing.

N26 watch closed on 2026-11-27 (shipped 2026-11-23, PR #138): three
consecutive cut mornings landed within three points of the `07:00` projection
(`3.0` on 11-25, `2.6` on 11-26 with the clamp firing once on a `3316 W`
spike, `1.0` on 11-27), and 2026-11-27 cleared the threshold with zero holds.

N32 shipped on 2026-11-27 at the owner's direction: `LOAD_PROFILE_HISTORY_DAYS`
(`14` days from the 90 days kept on disk) replaces the 8-day chart window for
hourly-profile readers (`topup.py`, `load_profile.py`, `modes.py`, and the
dashboard Tonight/load-profile views), so Saturday nights see four weekend
nights and qualify instead of falling back to the band planner. Watch stands
under Standing Watches for the `2026-11-28` Saturday `18:00` plan.

N28 shipped on 2026-11-25 at the owner's direction: `choose_preserve_threshold`
and `analyze_weather_window` now accept `window_start`, and
`next_morning_preserve_target_soc` passes `window_start=morning` (normalized to
`weather_timezone`, failing closed to `unavailable` if the forecast omits the
window) instead of evaluating the evening's 18:00–22:00 sky. Watch stands under
Standing Watches for the next three evening plans.

N29 shipped on 2026-11-25 at the owner's direction (Option 2):
`_record_preserve_utility_hold` now always adds `auto_topup_headroom_soc`
(`+3` points) even when `soc < threshold`, while keeping `drain_soc` on the
deferred `soc >= threshold` branch so early under-threshold holds stop
short-cycling bare `39% -> 40%` without over-charging to `48–50%`. Watch stands
under Standing Watches for the next under-threshold preserve morning.

N31 shipped on 2026-11-26 at the owner's direction: the sunny weather category
now tolerates up to `WEATHER_SUNNY_MAX_RAIN_MM` (`0.5 mm`) of total forecast
rain over the lookahead instead of requiring exactly zero, so a clear morning
with a drizzle trace uses the sunny threshold. Watch stands under Standing
Watches.

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
