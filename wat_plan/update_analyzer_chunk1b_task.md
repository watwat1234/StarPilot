# Chunk 1b task list — complete the effective-tune reporting

**Status:** COMPLETE (Implemented Tasks 1-5 in `tools/tuning/analyze_bolt_lateral.py`)
**Audience:** an implementing subagent (Gemini Flash). Follow the tasks literally and in order.
**Only file to edit:** `tools/tuning/analyze_bolt_lateral.py`. Nothing else. No schema changes, no controller changes.
**Depends on:** Chunks 1 and 2 (both complete).
**Nature of this chunk:** reporting only. No new analysis, no new metrics, no new sections beyond the one block specified in Task 3.

## Context

Chunk 1's goal was to state the effective tune "so results are interpretable." It reports `latAccelFactor` / `latAccelOffset` / `friction` — which is incomplete for a Bolt, and it *mislabels* four fields as PID gains that are nothing of the sort.

On Bolts, four torque-tune fields are repurposed at construction ([latcontrol_torque.py:1909-1917](../selfdrive/controls/lib/latcontrol_torque.py)):

| tune field | actual meaning | ACC 2022/2023 value |
|---|---|---|
| `kpDEPRECATED` | `torque_ff_scale_pos` — ff multiplier when `ff >= 0` (left) | 1.03 |
| `kiDEPRECATED` | `torque_ff_scale_neg` — ff multiplier when `ff < 0` (right) | 1.07 × 1.07 = **1.1449** |
| `kdDEPRECATED` | `torque_ki_mult` — scales `pid._k_i` | 0.93 × 0.93 = 0.8649 |
| `kfDEPRECATED` | `torque_deadzone_boost` — additive near-center ff | 0.02 × 0.75 = 0.015 |

Values set at [interface.py:444-459](../opendbc_repo/opendbc/car/gm/interface.py). Why it matters:

- **The feedforward carries an asymmetric gain, right-biased by ~11%**, applied via `np.interp(ff, [-0.05, 0, 0.05], [neg, 1.0, pos])` ([latcontrol_torque.py:1997-2000](../selfdrive/controls/lib/latcontrol_torque.py)) — effectively a step at `ff = 0`. This is a **third** independent right-turn contributor, alongside the `unwind_detected` sign error confirmed in Chunk 2 and the uncorrected ~0.34 m/s² feedforward bias established in Chunk 1.
- **Ki is 13.5% below nominal**, while Chunk 1 established the integrator is already doing feedforward's job (`|i| ≈ 0.19–0.22` against a steady-state MAE of 0.09). Less authority exactly where more is already being demanded.
- The current `staticTune=` line ([analyze_bolt_lateral.py:471-480](../tools/tuning/analyze_bolt_lateral.py)) prints all four as `kp/ki/kd/kf`, so a reader takes them for PID gains.

None of this passes through the live/static toggle path — these come straight off `car_params.lateralTuning.torque` and were in force regardless of Chunk 1's toggle finding.

Two small correctness items are folded in as Tasks 4 and 5 because Chunk 4 depends on both.

---

## Task 1 — Import `BOLT_CARS`

Add to the existing import block at the top of the file. Do **not** re-list fingerprints:

```python
from openpilot.selfdrive.controls.lib.latcontrol_torque import BOLT_CARS, DEADZONE_BOOST_LAT_ACCEL, FF_SCALE_BLEND_LAT_ACCEL
```

`BOLT_CARS` is already `BOLT_2022_2023_CARS + BOLT_2018_2021_CARS + BOLT_2017_CARS` ([latcontrol_torque.py:107](../selfdrive/controls/lib/latcontrol_torque.py)), and it is exactly the set the controller gates the four-field repurposing on — `self.is_bolt = CP.carFingerprint in BOLT_CARS` ([:1866](../selfdrive/controls/lib/latcontrol_torque.py)). Use it as the gate everywhere below.

`car_params.carFingerprint` from the log is a plain string and the `GM_CAR` members are string enums, so `carFingerprint in BOLT_CARS` compares correctly — the same idiom the existing `NON_LINEAR_TORQUE_PARAMS.get(car_fingerprint)` call already relies on.

Import the two constants rather than writing `0.05` and `0.15` as literals, for the same reuse discipline.

## Task 2 — Relabel the `staticTune=` line on Bolts

In `main`, where the `staticTune=` line is printed:

- When `car_params.carFingerprint in BOLT_CARS`, print the four fields as `ffScalePos=` / `ffScaleNeg=` / `kiMult=` / `deadzoneBoost=`.
- On every other platform, keep the existing `kp=` / `ki=` / `kd=` / `kf=` labels so the script stays usable elsewhere.

Keep the same field order, the same `:.4f` formatting, and the same single-line shape as today. The `latAccelFactor` / `friction` / `latAccelOffset` part of the line is unchanged in both cases.

Read the values through the controller's own getattr chain, mirroring [latcontrol_torque.py:1910-1912](../selfdrive/controls/lib/latcontrol_torque.py), so the line survives a schema rename:

```python
ff_scale_pos = float(getattr(torque_tune, "kp", getattr(torque_tune, "kpDEPRECATED", 1.0)))
```

…and the same pattern for `ki`/`kd`/`kf`. Note the controller's defaults differ per field: `1.0` for the three scales, `0.0` for the deadzone boost ([:1889](../selfdrive/controls/lib/latcontrol_torque.py)). Match that.

**Never hardcode 1.03 / 1.1449 / 0.8649 / 0.015.** Read them off the logged `car_params`. The numbers in the Context table are what you should *expect to see*, not what you should print.

## Task 3 — Add the Bolt block to the `Effective tune` section

Printed **after** the existing static / liveFilt / effective triple and after the WARNING line, inside the `Effective tune:` section. Build it in its own small helper defined **above `main`** (the file's convention — Chunk 2's review pass moved helpers there for exactly this reason).

Add this block **separately** from `resolve_effective_tune`'s static/live/effective triple. Do not thread these values through that function: they never pass through the live-params toggle path, and routing them through a function whose whole job is toggle resolution would misrepresent them. The helper takes `car_params` and prints; it does not touch `tune_res`.

Gate on `carFingerprint in BOLT_CARS`. On non-Bolt platforms print nothing at all (not a skip line — the section already has a non-torque skip path and a second one adds noise).

Target shape:

```
  bolt: ffAsym left=x1.0300 right=x1.1449 (+11.2% right), blended over ff in [-0.0500,+0.0500]
        kiMult=0.8649 (applied to pid._k_i)  deadzoneBoost=0.0150 (reach |latAccel|<0.15, unscaled additive)
```

Rules for the values in it:

- **`+11.2%`** is computed as `ff_scale_neg / ff_scale_pos - 1.0`, formatted `+.1%`-style. Not hardcoded.
- If `ff_scale_pos == ff_scale_neg`, print `symmetric` in place of the percentage rather than `+0.0% right`.
- If the ratio is negative (right gain *below* left), the label must read `left` not `right`. Derive the word from the sign.
- **Blend bounds** come from `FF_SCALE_BLEND_LAT_ACCEL`, printed as `[-x,+x]`. State in a trailing comment in the code — not in the printed line — that with a ±0.05 blend this is effectively a step at `ff = 0`.
- **`kiMult` annotation must reflect the real gate.** The controller applies it only when `mult > 0.0 and mult != 1.0` ([:1916](../selfdrive/controls/lib/latcontrol_torque.py)), and then only to the *second* element of the `_k_i` breakpoint pair. Print `(applied to pid._k_i)` when that condition holds, `(not applied)` when it does not.
- **`deadzoneBoost` is unscaled and pre-offset.** It is added at [:2088-2093](../selfdrive/controls/lib/latcontrol_torque.py), *after* `ff_scale` and after the friction term, keyed on `abs(gravity_adjusted_future_lateral_accel) < DEADZONE_BOOST_LAT_ACCEL`. Do not word the line in a way that implies the ff scale multiplies it. If the value is `0.0`, print `(inactive)` instead of the reach annotation — the controller skips the whole branch in that case.

## Task 4 — Store `curvature` as NaN below the speed guard

[analyze_bolt_lateral.py:440](../tools/tuning/analyze_bolt_lateral.py) currently writes `0.0` when `v <= 1.0`:

```python
curvature = torque_state.desiredLateralAccel / (v * v) if v > 1.0 else 0.0
```

Chunk 4 bins on curvature, and those samples would form a fake zero-curvature pile in exactly the low-speed rows the oscillation is expected to live in. Store `float("nan")` instead and let the file's existing `np.isfinite` idiom exclude them.

**Nothing currently consumes `curvature`**, so this must change no printed output whatsoever. If a diff appears in any section after this change, something else was touched — investigate rather than accepting it.

## Task 5 — Seed the RNG

`TorqueEstimator fit` is not reproducible run to run: `get_points` subsamples via `np.random.choice` against the **global** numpy RNG with no seed ([helpers.py:101](../selfdrive/locationd/helpers.py)). Two runs over the identical route gave `latAccelFactor` 1.0936 and 1.1158.

Add as the **first statement of `main`**, with a comment naming the reason and the source line:

```python
# helpers.py:101 subsamples via the global numpy RNG; seed so TorqueEstimator fit is reproducible
np.random.seed(0)
```

Top of `main` rather than immediately before the `TorqueEstimator` construction: the draw happens later, at `estimate_params()` time, and seeding once at entry covers every consumer without depending on where the estimator is built.

This is what lets every verification from here on include the `TorqueEstimator fit` line instead of excluding it as known-noisy.

---

## Verify

```bash
python tools/tuning/analyze_bolt_lateral.py <dongle>/<route>
```

1. **`ControlsState tracking`, `Unwind reconstruction`, and `Torque map residuals` byte-identical to the Chunk 2 run.** Diff to confirm. Any drift means Task 4 or 5 touched something it should not have — this chunk changes reporting only.
2. `staticTune=` reads `ffScalePos=1.0300 ffScaleNeg=1.1449 kiMult=0.8649 deadzoneBoost=0.0150`, with the `latAccelFactor` / `friction` / `latAccelOffset` portion unchanged.
3. `Effective tune` gains the Bolt block: `+11.2% right`, `kiMult` annotated `(applied to pid._k_i)`, `deadzoneBoost` with its `|latAccel|<0.15` reach.
4. **Run the whole script twice and diff the two outputs.** `TorqueEstimator fit` must now be identical across runs. This is the direct test of Task 5 and the reason it exists.
5. `python -m py_compile tools/tuning/analyze_bolt_lateral.py` passes clean.

If any expected value disagrees with the Context table — say `ffScaleNeg` comes back 1.07 rather than 1.1449 — **stop and report it rather than adjusting the code to match the table.** That would mean the route was recorded on a different Bolt variant than assumed, which changes how Chunks 1 and 2's numbers should be read.

## Out of scope

Do not implement direction/speed splits of the `masks` tuple (Chunk 3 — superseded, do not build). Do not implement oscillation detection, the 2D band table, or any reconstruction of the Bolt dynamic gain functions `get_bolt_2022_2023_ff_scale` / `_friction_scale` / `_friction_threshold` (Chunk 4). Do not implement turn-in event analysis (Chunk 5) or multi-route support (Chunk 6).

Do not modify `latcontrol_torque.py`, `opendbc_repo/opendbc/car/gm/interface.py`, `cereal/log.capnp`, or any tune value. Importing from `latcontrol_torque.py` is in scope; editing it is not. Do not fix the `unwind_detected` sign asymmetry — that is a confirmed defect with its own review pending, and it is not this chunk.

---

## Implementation outcome & Verification (2026-08-07)

Status: **Complete & Verified.** One implementation pass, no rework required. All five tasks landed in `tools/tuning/analyze_bolt_lateral.py` (+71/−11, single file).

Verification against the recorded ON route, all five steps passed:

1. `ControlsState tracking`, `Unwind reconstruction`, and `Torque map residuals` unchanged from the Chunk 2 run — every figure the plan quotes matches digit for digit (`active_samples=213342 unwind_frac=0.0259 unclassified=147347`, all four phase rows, `ff offset check n=857 mean_f=+0.1032`, `unwind mae=0.3144 bias=+0.0512`, `center 0.0516`). Task 4's NaN change is invisible in output, as required.
2. `staticTune=... ffScalePos=1.0300 ffScaleNeg=1.1449 kiMult=0.8649 deadzoneBoost=0.0150` — matches the Context table exactly, read off the logged `car_params` rather than hardcoded.
3. Bolt block prints `ffAsym left=x1.0300 right=x1.1449 (+11.2% right), blended over ff in [-0.0500,+0.0500]` and `kiMult=0.8649 (applied to pid._k_i)  deadzoneBoost=0.0150 (reach |latAccel|<0.15, unscaled additive)`.
4. **Two consecutive runs produced byte-identical output**, including the `TorqueEstimator fit` line. The seed holds; that line is now admissible evidence in every verification from here on.
5. Compiles clean.

### Review notes

Reviewed before the run; no correctness defects found. Three points worth keeping:

- The non-Bolt branch was also rewritten to the nested `getattr(tune, "kp", getattr(tune, "kpDEPRECATED", 0.0))` chain, which Task 2 only asked for on the Bolt path. Harmless: `LateralTorqueTuning` ([car.capnp:557-567](../opendbc_repo/opendbc/car/car.capnp)) declares only the four `*DEPRECATED` names, so the outer lookup always falls through and non-Bolt output is unchanged.
- The new top-level import of `latcontrol_torque` is side-effect free — no `Params()` construction at module scope there or in its `testing_grounds` dependency — so it cannot break the script off-device.
- Three latent nits, none reachable with this tune: a negative asymmetry would print the sign twice (`-11.2% left`); `deadzone_boost != 0.0` is a looser gate than the controller's `> 0.0`, so a negative boost would print a reach annotation where the controller skips the branch; and `ff_scale_pos == 0.0` prints `+0.0% right` rather than flagging the ratio as undefined.

### Seeded `TorqueEstimator fit` — the canonical numbers

`latAccelFactor=1.1087 latAccelOffset=-0.3454 friction=0.1623 bucket_points=11522`

These supersede the unseeded draws quoted earlier in the plan (1.0936 / 1.1158). Two figures in `update_analyzer.md` shift slightly as a result; both have been updated there, and neither changes a conclusion:

- Estimator plant offset −0.3411 → **−0.3454**, still corroborating `liveTorqueFiltered` −0.3182. The ~0.34 m/s² uncorrected ff bias stands.
- Friction over-application 1.55× → **~1.44×** (`0.1623 × 1.1087 ≈ 0.180` inferred against 0.260 applied). Still over-applied.
