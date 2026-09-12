# Fix Hard Left/Hard Right bucket excusal: excuse when empty, also accept when non-empty

## Status: done, shipped

## Outcome (2026-09-12)
Steps 1-3 implemented as written, `/code-review high` run, two findings fixed (one real bug in the
Step 3 display code that didn't route through `_is_excusable`, one minor zip-fragility nit in
`helpers.py`), then re-verified (5/5 tests, mypy/ruff clean). Committed on the analysis branch
(`wat-ioniq-torqued-excusal-fix`) at `fb16ade49`.

Deployed here by cherry-picking `fb16ade49` onto `wat-ioniq-tuning` (this worktree), commit `d999c97cc`
— rather than reimplementing directly on this branch per this plan's original two-worktree Environment
section below. `tools/tuning/inspect_torque_buckets_streaming.py` doesn't exist on `wat-ioniq-tuning`
(it's only on `wat-ioniq-torque-analysis-tools`), so only the `helpers.py`/`test_torqued.py` changes
(Steps 1-2) carried over; the commit message on this branch was amended to say so. Not pushed yet.

## Context
Discovered while comparing on-device `torqued` state across routes 34 and 35 (Ioniq 6 drives with the shipped `IONIQ_6_FRICTION_CENTER_FADE_MAX` chatter fix, see [[project_ioniq6_tuning_investigation]] / `.scratch/ioniq-detailed-tuning.md`): route 34 ended at `liveValid=True`/`calPerc=100%`, but route 35 started at `liveValid=False`/`calPerc=62%`, with `totalBucketPoints` unchanged (7729.0 both times).

Root cause confirmed by reading `selfdrive/locationd/helpers.py:82-96` (`PointBuckets.is_valid`/`get_valid_percent`/`is_calculable`):

```python
individual_buckets_valid = all(len(v) >= min_pts or (self.allow_empty_buckets and len(v) == 0)
                                for v, min_pts in zip(self.buckets.values(), self.buckets_min_points.values(), strict=True))
```

`allow_empty_buckets` (shipped in `302eaaed0`, see the closed `.scratch/ioniq_torqued_allow_empty_buckets_plan.md`) only excuses a bucket while it is **exactly empty** (`len(v) == 0`). The moment a bucket that was excused-empty picks up even one point below its own `min_pts`, it satisfies neither branch (`len(v) >= min_pts` is false, `len(v) == 0` is now also false) and becomes a **new** blocker — worse than being empty. This is very likely what regressed route 35's `calPerc`/`liveValid`: Hard Left or Hard Right (the two buckets this mechanism exists for) picked up a small number of points between drives.

**Not fully confirmed** from logs: the periodic `liveTorqueParameters` message only carries `.points` when torqued runs with `DEBUG=1` (`torqued.py:281`), so the actual per-bucket counts at the route-34/35 boundary aren't recoverable from these route logs. The fix below is justified by the code logic itself (a real bug independent of whether it's confirmed as *the* cause of this specific transition) — worth shipping regardless.

**Desired behavior**: a bucket should be excused if it's empty, **and also accepted (not just excused-lower) once it has any points at all**, without needing to reach its own `min_pts`. In other words, this bucket should never gate `is_valid()`, full stop — whether it has 0 points or 37.

## Scoping decision: only the two extreme buckets, not all 8

`allow_empty_buckets` today is checked uniformly across **all 8 buckets** in `PointBuckets` (`torqued.py`'s `TorqueBuckets`, `STEER_BUCKET_BOUNDS`), not just Hard Left/Hard Right. If the "excuse regardless of fill" relaxation were applied the same uniform way, a middle bucket (e.g. Near-Center Right, `min_pts=500`) would become fully satisfied the instant it had even 1 point — defeating the actual purpose of requiring real coverage for buckets that normal driving fills naturally and heavily. That would be a much bigger, unintended widening of what "valid" means for every bucket, not a targeted fix.

**Decision**: scope the relaxation to exactly the two extreme buckets — `x_bounds[0]` (Hard Left, `[-0.5, -0.3)`) and `x_bounds[-1]` (Hard Right, `[0.3, 0.5)`) — which is what "Hard Left/Hard Right" already names in `inspect_torque_buckets_streaming.py`'s own bucket descriptions, and matches the actual intent recorded in the original plan (`ioniq_torqued_allow_empty_buckets_plan.md`): these two are the ones a normal driving pattern may never (or rarely) exercise. The other 6 buckets keep their existing strict `len(v) >= min_pts` requirement unchanged, even when `allow_empty_buckets=True`.

Only one caller exists today (`grep -rln "PointBuckets\|allow_empty_buckets"` → `torqued.py`, `helpers.py`, `test_torqued.py` — confirmed, no other consumer), and only `HYUNDAI_IONIQ_6` sets `allow_empty_buckets=True` (`torqued.py:259`), so blast radius is contained to this one car.

## Environment
- Worktree: `/home/kirin/starpilot/starpilot-ioniq-analysis`, branch `wat-ioniq-torqued-excusal-fix` (branched off `wat-ioniq-torque-analysis-tools` specifically to keep this fix separate from the ongoing analysis work there — `git log` on this branch for full context, currently just this plan doc committed at `be1c98e1e`).
- Python env: `source /home/kirin/starpilot/StarPilot/.venv/bin/activate`; run tools with `PYTHONPATH="$PWD"` set to this worktree's root.
- Route data for offline sanity checks (symlinked into this worktree): `routes/00000031--d305a9f4bd` (169-segment baseline, known `liveValid=True`/`calPerc=100%` at end), `routes/00000034--58b6d0552c` (16 segments), `routes/00000035--baa7756fc8` (15 segments).
- **Verified** (2026-09-12, this worktree): `PYTHONPATH="$PWD" python -m pytest selfdrive/locationd/test/test_torqued.py -v` runs all 4 existing tests clean, no native-ext rebuild needed — this worktree's committed x86_64 build already covers it.
- Commit discipline: **ask before every `git commit`**, per standing convention on this branch (see [[feedback_commit_permission]]). Do not bundle a commit into "the fix is done."

## Implementation

### Step 1 — `selfdrive/locationd/helpers.py`, `PointBuckets`
Replace the `len(v) == 0` check with a bucket-position check, and factor it into one helper so `is_valid`, `get_valid_percent`, and `is_calculable` all agree on which buckets are excusable:

```python
def _is_excusable(self, bound: tuple[float, float]) -> bool:
  # Only the two extreme buckets (Hard Left/Hard Right) are excusable -- a normal driving pattern
  # may rarely exercise them. Excuse regardless of fill level (0 or partial): a bucket that picks up
  # a few points below its own min_pts must not become a NEW blocker relative to being empty.
  return self.allow_empty_buckets and bound in (self.x_bounds[0], self.x_bounds[-1])

def is_valid(self) -> bool:
  individual_buckets_valid = all(self._is_excusable(bound) or len(v) >= self.buckets_min_points[bound]
                                  for bound, v in self.buckets.items())
  total_points_valid = self.__len__() >= self.min_points_total
  return individual_buckets_valid and total_points_valid

def get_valid_percent(self) -> int:
  total_points_perc = min(self.__len__() / self.min_points_total * 100, 100)
  bucket_percs = [len(v) / self.buckets_min_points[bound] * 100 for bound, v in self.buckets.items()
                  if not self._is_excusable(bound)]
  individual_buckets_perc = min(min(bucket_percs), 100) if bucket_percs else 0
  return int((total_points_perc + individual_buckets_perc) / 2)

def is_calculable(self) -> bool:
  return self.__len__() >= 3 and all(self._is_excusable(bound) or len(v) > 0
                                      for bound, v in self.buckets.items())
```

(Final shipped version iterates `self.buckets.items()` directly rather than zipping a separate
`x_bounds` list, per a `/code-review high` finding about zip/dict fragility if `x_bounds` ever had a
duplicate bound.)

No changes needed to `torqued.py` — `allow_empty_buckets` stays a plain bool, set the same way at both call sites (`torqued.py:259-260,267`).

### Step 2 — `selfdrive/locationd/test/test_torqued.py`
Two of the three existing `allow_empty_buckets` tests need attention:

- **`test_allow_empty_buckets_valid_percent_and_validity` must change.** It currently uses `partial_key = keys[-1]` (Hard Right) as its "must still hit its own threshold" bucket — but under this fix, Hard Right is now excusable regardless of fill, so that assertion path would no longer hold as written. Rewrite it to use a genuine **middle** bucket (e.g. `keys[1]` or `keys[-2]`, a "Moderate"/"Gentle" bucket) for the "still gates validity below its threshold" case, keeping `keys[0]` (Hard Left) as the excused-while-empty case.
- **Add a new test** covering the actual regression this plan fixes: an excusable bucket (`keys[0]` or `keys[-1]`) that goes from **empty → a few points below its own `min_pts`** (not zero, not full) must **stay** excused — `is_valid()`/`calPerc` must not regress when this happens, unlike today's code. This is the test that would have caught the routes-34/35 symptom. Shipped as `test_allow_empty_buckets_partial_fill_stays_excused`, with explicit `liveTorqueParameters.liveValid is True` assertions at both the fully-valid and excused-partial-fill checkpoints (not just `filtered_points.is_valid()`), confirming the real message field the on-device symptom was reported against doesn't regress.
- `test_allow_empty_buckets_no_crash_when_all_empty` and `test_allow_empty_buckets_default_path_unchanged` are unaffected (all-empty stays all-empty; default path never sets `allow_empty_buckets`) — left as-is.

Run: `PYTHONPATH="$PWD" python -m pytest selfdrive/locationd/test/test_torqued.py -v` — 5/5 pass (verified in the analysis worktree; this worktree's checked-in `.so` files are aarch64 device binaries and can't run pytest without a scons x86_64 rebuild, see [[project_native_ext_testing]]).

### Step 3 (optional, separate from the on-device fix) — offline tool parity
`tools/tuning/inspect_torque_buckets_streaming.py:81` constructed `TorqueEstimator(car_params, decimated=args.decimated, track_all_points=True)` **without** passing `allow_empty_buckets`, unlike the real on-device `main()` (`torqued.py:259-260`, which sets it `True` for `HYUNDAI_IONIQ_6`). Fixed on the analysis branch by passing `allow_empty_buckets=car_params.carFingerprint == CAR.HYUNDAI_IONIQ_6`, and by routing the tool's own bucket-status display and `calc_cal_perc` through `_is_excusable()`/`get_valid_percent()` instead of a parallel hand-rolled calc (a `/code-review high` finding — the display was still showing Hard Left/Right as "BLOCKING" even after the constructor fix). **Does not apply to this worktree** — this tool file doesn't exist on `wat-ioniq-tuning`.

## Verification
1. Unit tests (Step 2) are the primary verification — they exercise the exact empty→partial-fill transition without needing a car or a new drive. 5/5 pass.
2. Regression check against known-good data: reran `tools/tuning/inspect_torque_buckets_streaming.py` against route 31 and route 34 (analysis worktree only) — Hard Left/Right correctly show `EXCUSED (allow_empty_buckets)` and no longer contribute to "lowest bucket"/BLOCKING; remaining `Valid=False` cases are due to genuine mid-bucket shortfalls, not this fix.
3. Can't directly replay the exact route-34→35 transition (the real bug trigger) offline, since the local route logs don't carry the actual on-device bucket state (`DEBUG=1` gap noted above) — the unit test is the closest reproduction available.
4. `/code-review high` run on the analysis branch; both findings fixed and reverified.
5. Ask before `git commit`, as always.
