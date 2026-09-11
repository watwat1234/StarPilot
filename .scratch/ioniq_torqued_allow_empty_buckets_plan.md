# Minimal `torqued` convergence fix for this Ioniq 6 (no toggle/UI/dashboard)

## Context
Goal, stated explicitly by the user: seed the torque tuner (`torqued`) so it reaches real, ongoing convergence — `liveValid=True`, and `update_params()` continuing to refine `latAccelFactor`/`friction`/`latAccelOffset` from live driving data going forward — not a one-time cosmetic fix.

Two simpler alternatives were considered and ruled out for this specific goal:
- **Galaxy manual override** (`SteerLatAccel`/`SteerFriction` custom params, `torqued.py:235/237`, `controlsd.py:359-372`): only substitutes the *published* value after all real estimator logic has already run. Never touches `PointBuckets`/`is_calculable()`/`is_valid()`/`estimate_params()`/`update_params()`. `torqued` stays permanently deadlocked either way — this doesn't produce convergence or ongoing learning, only a static drive-time substitution.
- **`override.toml` stock-baseline edit**: fixes what value the car starts from, but `is_calculable()` still requires every bucket to have `>0` real points, so it doesn't change whether `torqued` ever reaches `liveValid=True` or resumes learning.

Root cause (confirmed via `helpers.py`/`torqued.py` trace and independently by `wat_plan/ioniq_torqued_investigation.md`'s own telemetry analysis across two real drives): Bucket 0 (Hard Left, `[-0.5, -0.3]`) never accumulates a single point under this car's actual driving pattern, and `PointBuckets.is_calculable()` (`helpers.py:91-92`) unconditionally requires every bucket to have `>0` points before `estimate_params()`/`update_params()` ever run — so the estimator is permanently stuck at 50% `calPerc`/`liveValid=False`, regardless of how well-filled the other 7 buckets are.

**Decision**: implement only the `allow_empty_buckets` mechanism from the previously-reviewed `.scratch/forcetorquedecimated.md` plan (its Steps 1-2), but hardcode it on for this car's specific fingerprint instead of building the persistent `ForceTorqueDecimated` toggle + settings row + dashboard card (Steps 3-6 of that plan) — those exist only to make the feature user-facing/shippable to other people's cars, which isn't needed here.

## What ships — two files

### 1. `selfdrive/locationd/helpers.py` — `PointBuckets`
Add `allow_empty_buckets: bool = False` to `__init__`, store as `self.allow_empty_buckets`, and update the three per-bucket-status methods (current code at lines 70-92):

```python
class PointBuckets:
  def __init__(self, x_bounds, min_points, min_points_total, points_per_bucket, rowsize, allow_empty_buckets: bool = False) -> None:
    self.x_bounds = x_bounds
    self.buckets = {bounds: NPQueue(maxlen=points_per_bucket, rowsize=rowsize) for bounds in x_bounds}
    self.buckets_min_points = dict(zip(x_bounds, min_points, strict=True))
    self.min_points_total = min_points_total
    self.allow_empty_buckets = allow_empty_buckets

  def __len__(self) -> int:
    return sum([len(v) for v in self.buckets.values()])

  def is_valid(self) -> bool:
    individual_buckets_valid = all(len(v) >= min_pts or (self.allow_empty_buckets and len(v) == 0)
                                    for v, min_pts in zip(self.buckets.values(), self.buckets_min_points.values(), strict=True))
    total_points_valid = self.__len__() >= self.min_points_total
    return individual_buckets_valid and total_points_valid

  def get_valid_percent(self) -> int:
    total_points_perc = min(self.__len__() / self.min_points_total * 100, 100)
    bucket_percs = [len(v) / min_pts * 100 for v, min_pts in zip(self.buckets.values(), self.buckets_min_points.values(), strict=True)
                    if not (self.allow_empty_buckets and len(v) == 0)]
    individual_buckets_perc = min(min(bucket_percs), 100) if bucket_percs else 100
    return int((total_points_perc + individual_buckets_perc) / 2)

  def is_calculable(self) -> bool:
    return self.__len__() >= 3 and all(len(v) > 0 or self.allow_empty_buckets for v in self.buckets.values())
```

**The `self.__len__() >= 3` floor in `is_calculable()` is load-bearing, not optional.** Without it, enabling `allow_empty_buckets` makes `is_calculable()` unconditionally `True` from the very first cycle after boot (0 total points), and `get_msg()` (`torqued.py:215-216`) calls `estimate_params()` whenever `is_calculable()` is true — `estimate_params()`'s `np.linalg.svd(points, ...)` / `v.T[0:2, 2]` slice (`torqued.py:151-158`) raises an **uncaught `IndexError`** on fewer than 3 rows (the `except np.linalg.LinAlgError` there does not catch it), causing `torqued` to crash-loop on every boot. `3` is purely the floor needed for the SVD slice to not raise — unrelated to `min_points_total` (4000/600). In the real scenario here (7 buckets fully filled), this floor is cleared thousands of points over and changes nothing about actual behavior.

The `get_valid_percent()` fix (filtering excused-empty buckets out of `bucket_percs`) is required too — without it, calPerc would report 0% forever even after `liveValid` goes `True`, since an excused bucket would otherwise contribute `0/min_pts*100=0` to the `min()`.

`TorqueBuckets` (`torqued.py:47`) subclasses `PointBuckets` with no `__init__` override, so it inherits the new parameter automatically.

### 2. `selfdrive/locationd/torqued.py` — thread the flag through, hardcoded on for this car

**`TorqueEstimator.__init__`/`reset()`** (currently `torqued.py:56`/`:136`): add `allow_empty_buckets: bool = False` to the signature, store `self.allow_empty_buckets = allow_empty_buckets` before the `self.reset()` call at line 85, and pass `allow_empty_buckets=self.allow_empty_buckets` into the `TorqueBuckets(...)` construction in `reset()` (lines 140-144).

**`main()`** (currently lines 253-263): the existing code already loads `CarParams` at line 254 before anything else, and today does so *twice* — once unconditionally (line 254) and again inside the `if not starpilot_toggles.liveValid:` block (line 261), each a separate blocking `params.get("CarParams", block=True)` read. Add a fingerprint check right after the first load, reuse the same loaded `car_params` for both constructions (collapsing the two blocking reads into one — a deliberate, low-risk simplification bundled into this edit, called out explicitly here so it isn't a surprise in the diff), and pass the flag into both:

```python
def main(demo=False):
  from opendbc.car.hyundai.values import CAR
  ...

  params = Params()
  car_params = messaging.log_from_bytes(params.get("CarParams", block=True), car.CarParams)
  allow_empty_buckets = car_params.carFingerprint == CAR.HYUNDAI_IONIQ_6
  estimator = TorqueEstimator(car_params, allow_empty_buckets=allow_empty_buckets)

  sm = sm.extend(['starpilotPlan'])

  starpilot_toggles = get_starpilot_toggles()

  if not starpilot_toggles.liveValid:
    estimator = TorqueEstimator(car_params, decimated=True, allow_empty_buckets=allow_empty_buckets)

  estimator.starpilot_toggles = starpilot_toggles
```

**Import placement**: put `from opendbc.car.hyundai.values import CAR` as a **local import inside `main()`**, not at module top — this matches the one precedent the plan cites for the fingerprint-check pattern itself (`modeld.py:145` imports `CAR` locally inside `_egmp_ready_bus`, not at module scope), and avoids a module-level dependency on `opendbc.car.hyundai` for a check that's only relevant inside this one function. Confirmed `torqued.py` has no existing `CAR`/`opendbc.car.hyundai.values` import today, so there's no duplicate-import conflict either way — this is purely a style/placement choice, not a correctness one.

This mirrors the fingerprint-check pattern already used elsewhere in this codebase (e.g. `latcontrol_torque.py:122`: `self.is_ioniq_6 = CP.carFingerprint in IONIQ_6_CARS`, `modeld.py:146`: `CP.carFingerprint not in (CAR.HYUNDAI_IONIQ_5_PE, CAR.HYUNDAI_IONIQ_6, CAR.KIA_EV9)`, and the `CP.carFingerprint == CAR.HYUNDAI_IONIQ_6` equality form specifically used live in `opendbc_repo/opendbc/car/hyundai/carcontroller.py:441,948,1007,1018`, `radar_interface.py:115`, and `carstate.py:557,595`). No new Param key, no settings row, no dashboard change — `allow_empty_buckets` is purely a function of which car is plugged in, evaluated fresh every `torqued` boot.

Since this is scoped to a single fingerprint, every other car in `ALLOWED_CARS` gets `allow_empty_buckets=False` — byte-for-byte unchanged behavior from today. Confirmed the other existing `TorqueEstimator(...)` call sites in this repo (`tools/tuning/inspect_torque_buckets.py:202`, `tools/tuning/inspect_lateral_tuning.py:369`, `tools/tuning/analyze_bolt_lateral.py:841`, `selfdrive/controls/tests/test_torqued_lat_accel_offset.py:37`) don't pass `allow_empty_buckets` and are unaffected — they silently keep the default (`False`) behavior, no changes needed there.

## What this gives you (vs. the ruled-out alternatives)
Once your 7 non-empty buckets clear their thresholds and total points clear `min_points_total`, `is_valid()` goes `True` with the empty bucket(s) — Bucket 0, in this car's actual driving pattern — still excused. (Precisely: the mechanism excuses *whichever* bucket(s) are currently empty, not literally index 0 specifically; it's harmless here because Bucket 0 is the one this car's telemetry shows is always empty.) From that point on, `get_msg()`'s existing `update_params()` call (`torqued.py:230`) runs every cycle exactly as it does for any normal car — `latAccelFactor`/`latAccelOffset`/`frictionCoefficient` keep getting refit and filtered from live data in the 7 real buckets, indefinitely, with real ongoing decay/adaptation. The excused bucket itself never contributes data (that's the accepted extrapolation trade-off, unchanged from the original plan's analysis), but this is genuine, continuing convergence — not a frozen snapshot.

## What's explicitly NOT included (deliberately, per this reduced scope)
- No `ForceTorqueDecimated` Param key, no settings/UI row, no reboot-confirm dialog, no dashboard card, no "Clear Learned Torque Data" button (Steps 3-6 of the original `.scratch/forcetorquedecimated.md` plan). None of that is needed for a single personal car — `allow_empty_buckets` is just always-on for this fingerprint.
- No changes to `override.toml` or the Galaxy custom-override params — this fix supersedes the need for either, since the estimator will now genuinely converge and self-correct rather than needing a manual seed value.
- `wat_plan/ioniq_torqued_investigation.md`'s broader architectural rewrite (dynamic bucket-ladder rescaling, `is_calculable()` redesign, controller offset-decoupling, analyzer tool changes) — out of scope; this plan reuses only its diagnostic findings, not its implementation.
- `latAccelOffset` correction: unaffected either way by this change — it's already read live from `initial_params`/`update_params()` once `is_valid()` goes `True`, so no separate offset-specific work is needed here (unlike the override.toml/Galaxy paths, which had no offset story at all).

## Review checkpoint
Both changed files (`helpers.py`, `torqued.py`) directly decide whether a fitted torque model with zero real coverage in Bucket 0 gets applied to live steering — that risk is unchanged by dropping the toggle/UI/dashboard, since the underlying `allow_empty_buckets` mechanism is identical to the original plan's. Unlike that plan, there's no in-car off-switch or "Clear Learned Torque Data" button here if something's wrong with the fit — recovery means redeploying code or manually clearing the `LiveTorqueParameters` param. Given that, this diff gets a review gate before it goes on the car, proportionate to "two files, one person, one car" (not the original plan's multi-tier escalation matrix, which exists for a larger, multi-contributor, shippable feature):

1. **Automated `/code-review` pass** on the two-file diff first — cheap, catches mechanical mistakes (wrong variable, missed edit site, typos).
2. **Manual read** of the same diff, specifically checking:
   - The `is_calculable()`'s `self.__len__() >= 3` floor is present and unaltered (its absence crash-loops `torqued` on every boot — see the note under Step 1 above).
   - The fingerprint scoping (`car_params.carFingerprint == CAR.HYUNDAI_IONIQ_6`) is exact, so no other car in `ALLOWED_CARS` gets `allow_empty_buckets=True`.
   - `get_valid_percent()`'s empty-bucket filtering (`bucket_percs` excluding excused-empty buckets) behaves as described — doesn't drag `calPerc` to 0% or raise on an all-excused edge case.

Do the manual read even if the automated pass comes back clean — it's not a substitute, just a cheap first filter before it.

## Testing
- New test in `selfdrive/locationd/test/test_torqued.py`: the crash-regression test for the `is_calculable()` floor — construct `TorqueEstimator(car.CarParams(), allow_empty_buckets=True)` with all buckets empty, call `get_msg()` immediately, assert no exception.
- Second new test: one bucket empty + another partial (all buckets combined well past 3 points), assert `is_calculable() is True`, `is_valid() is False` until the partial bucket clears its threshold, and `get_valid_percent()` doesn't count the excused-empty bucket. **Use the same two-stage pattern as the existing `test_cal_percent()` (`test_torqued.py:6-27`), not a single assert of `== 100`** — `MIN_BUCKET_POINTS` sums to 2800 but `min_points_total` (non-decimated) is 4000, so filling every non-excused bucket to just its own per-bucket minimum only reaches ~67.5% on the total-points term, not 100%. Stage 1: fill all non-excused buckets to their individual minimums, leave the target bucket empty, assert `get_valid_percent()` matches the expected partial value from the formula (not dragged down by the excused bucket's `0/min_pts`). Stage 2: additionally top up total points to `min_points_total` (while still leaving the target bucket at 0), then assert `get_valid_percent() == 100`.
- Assert the default (`allow_empty_buckets=False`) path is unchanged: still requires every bucket `>0` for both `is_calculable()` and `get_valid_percent()`.
- Run existing `selfdrive/locationd/test/test_torqued.py` (has `test_cal_percent()`) to confirm no regression for the default path.
- `python -m py_compile` both changed files.

## Review status (post-implementation)
Implementation shipped in `302eaaed0` (`helpers.py`, `torqued.py`, plus the two new tests from this section). Reviewed the diff against this plan and actually ran the unit tests — the implementer's env couldn't import `cereal`/`msgq` (checked-in `.so`/`.a` files in this repo are aarch64 device binaries; a review session with a working x86_64 build rebuilt the needed extensions via targeted `scons` calls, ran the tests, then reverted the rebuilt binaries so the committed device build is untouched).

- **`helpers.py`/`torqued.py` production code**: matches this plan exactly — `is_calculable()`'s `>= 3` floor, `is_valid()`'s per-bucket excusal, exact-match `CAR.HYUNDAI_IONIQ_6` fingerprint scoping, and the flag threaded through `__init__`/`reset()`/both `main()` constructions all confirmed correct via manual read and passing tests. No changes needed.
- **Test bug found and fixed** (commit `4431f1e1f`): `test_allow_empty_buckets_valid_percent_and_validity`'s topup step originally added points via `full_keys[0]` (already at its own `min_pts=300`), but `NPQueue`'s `maxlen` (`POINTS_PER_BUCKET=1500`) only left 1200 points of headroom there — 100 short of the 1300 needed to reach `min_points_total`. Points silently rolled off instead of accumulating (`calPerc` stuck at 98, `liveValid` never went `True`). Fixed by topping up via `partial_key` instead (smallest `min_pts`, most headroom). This was a test-authoring bug, not a defect in the shipped `is_calculable()`/`is_valid()`/`get_valid_percent()` logic.
- **One un-flagged deviation from this doc's literal spec, left as-is**: this plan's Step 1 code block and its own review-checkpoint item 3 (line ~102, "doesn't drag calPerc to 0%... on an all-excused edge case") specify/expect `get_valid_percent()`'s `individual_buckets_perc` to fall back to `100` when `bucket_percs` is empty (all buckets currently excused-empty, i.e. a truly cold boot with zero points anywhere). The shipped code uses `else 0` instead, and `test_allow_empty_buckets_no_crash_when_all_empty` asserts `calPerc == 0` in that state. Judged more correct in review (0% real calibration at literal zero data beats a misleading 50%), and it doesn't affect `is_valid()`/`liveValid`/steering — only the cosmetic `calPerc` readout during the brief window right after a fresh boot before any bucket has data. Not changed; noting it here so the discrepancy between this doc and the code isn't a surprise later.
- All 4 tests in `selfdrive/locationd/test/test_torqued.py` pass; `python -m py_compile` clean on both changed files.
- Manual/on-device verification below is still outstanding — nothing in this section has been done on-car yet.

## Manual/on-device verification
1. Deploy, reboot, confirm `torqued` doesn't crash-loop (the `IndexError` regression check above, live).
2. Drive normally; watch `liveTorqueParameters` (via `cabana`/PlotJuggler or the existing `the_galaxy` troubleshoot display) until the 7 real buckets clear their thresholds — confirm `calPerc` reaches 100% and `liveValid` flips `True` with Bucket 0 still at 0 points.
3. Confirm `latAccelFactorFiltered`/`frictionCoefficientFiltered`/`latAccelOffsetFiltered` continue to move (however slightly) across multiple drives after that point — direct evidence `update_params()` is genuinely running, not just a one-time snapshot.
4. `tools/tuning/inspect_torque_buckets.py <route> --data-dir ...` on a fresh route to cross-check the live fit against the offline SVD numbers already in `wat_plan/ioniq_torqued_investigation.md` (`latAccelFactor≈5.64`, `friction≈0.055`).
