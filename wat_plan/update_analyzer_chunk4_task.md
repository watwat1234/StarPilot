# Chunk 4 task list — dynamic gain reconstruction, oscillation detection, band tables

**Status:** **PART A KEPT — PARTS B/C REVERTED (2026-08-07).** All three parts were implemented and run on the recorded route. Part A worked and stays in the file. The oscillation detector failed Verify step 4 and was removed; oscillation diagnosis is parked to decouple it from the slam/overcorrect work. Results, root causes and resume notes are in the Chunk 4 section of `update_analyzer.md` — read that before reviving anything here. **The tasks below describe the original scope and are no longer the live spec.**
**Audience:** an implementing subagent. Follow the tasks literally and in order.
**Only file to edit:** `tools/tuning/analyze_bolt_lateral.py`. Nothing else. No schema changes, no controller changes.
**Depends on:** Chunks 1, 1b and 2 (all complete) — `ControlSample` already carries `mono_time`, `torque_active`, `curvature`, `desired_jerk`, and the RNG is seeded in `main`.
**Build order:** Part A → Part B → Part C, as Tasks 1 → 2 → 3. Each is testable before the next exists.

## Context

The driver of a 2022/2023 Chevy Bolt reports a right 90° turn where the wheel slams right followed by an overcorrection left, **plus oscillation at one particular rightward curvature**. Chunks 1, 1b and 2 confirmed three independent right-biased contributors (the `unwind_detected` sign inversion, a ~0.34 m/s² uncorrected feedforward bias, and a ~11% right-biased static `ff_scale` asymmetry). The oscillation is the last unexplained symptom, and this chunk is the only thing that can localise it.

The original scope for this chunk assumed static friction over-application (the controller applies `0.130 × 2.0 = 0.260` against ~0.180 inferred, i.e. ~1.44× per Chunk 1b's seeded fit). **That is now the fallback, not the leading explanation**, for one reason: static friction over-application chatters near center at *all* curvatures. The reported symptom is one *particular* rightward curvature.

The leading hypothesis is the Bolt 2022/2023 dynamic gain layer — `get_bolt_2022_2023_ff_scale`, `get_bolt_2022_2023_friction_scale`, `get_bolt_2022_2023_friction_threshold` ([latcontrol_torque.py:974-1022](../selfdrive/controls/lib/latcontrol_torque.py)). It is **unconditionally active** on this car (`bolt_2022_2023_tuned_path_active = self.is_bolt_2022_2023`, [:2001](../selfdrive/controls/lib/latcontrol_torque.py) — no testing-ground gate, unlike the Volt/G90/EV6 paths). Its shape:

- **Curvature-banded** — `extra_scale = gain × σ((|la| − 0.12)/0.07) × σ((1.35 − |la|)/0.28)`: a gain bump that switches on near `|la| ≈ 0.12` and off near 1.35.
- **Direction-asymmetric** — `FF_GAIN_LEFT` 0.11 vs `FF_GAIN_RIGHT` 0.06; `UNWIND_TAPER` 0.38/0.40; `UNWIND_FRICTION_REDUCTION` 0.27/0.28; `TURN_IN_FRICTION_BOOST` 0.10/0.07.
- **Low-speed weighted** — `1/(1 + (v/9)²)`, strongest exactly where the symptom lives.
- **Hard-switched on phase** — `tanh(la × jerk / 0.12)` saturates almost immediately (`|la·jerk| > 0.3 ⇒ |phase| > 0.99`), so the turn-in and unwind gain sets swap near-discontinuously rather than blending.

A gain bump confined to a specific `|la|` band, direction-asymmetric, strongest below ~9 m/s, hard-switched on the sign of `la × jerk`, fits "one particular rightward curvature" far better than a flat friction error does. This chunk as originally written could not have seen any of it, because it binned outcomes without reconstructing the gains that produced them.

Worth noting while reading Part A's output: `BOLT_2022_2023_FRICTION_MULT = 1.09` ([:211](../selfdrive/controls/lib/latcontrol_torque.py)), so `friction_scale` is centred on **1.09, not 1.0** — a flat +9% on top of friction that Chunk 1b already established as ~1.44× over-applied. That is a static offset, not a banded effect, so it belongs to the fallback explanation rather than this one; report it, do not chase it here.

---

## Task 1 — Part A: reconstruct the dynamic gains

### 1a. Imports

Extend the existing import block at the top of the file ([analyze_bolt_lateral.py:11-15](../tools/tuning/analyze_bolt_lateral.py)):

```python
from openpilot.selfdrive.controls.lib.latcontrol_torque import (
  BOLT_CARS,
  BOLT_2022_2023_CARS,
  DEADZONE_BOOST_LAT_ACCEL,
  FF_SCALE_BLEND_LAT_ACCEL,
  get_bolt_2022_2023_ff_scale,
  get_bolt_2022_2023_friction_scale,
  get_bolt_2022_2023_friction_threshold,
  get_friction_threshold,
)
```

**Call the real functions. Do not reimplement them**, and do not copy their constants into this file — same reuse discipline as the existing `NON_LINEAR_TORQUE_PARAMS` and Chunk 1b `BOLT_CARS` imports.

### 1b. The argument-order trap — read this before writing the calls

The three functions do **not** take their arguments in the same order:

```python
get_bolt_2022_2023_ff_scale(desired_lateral_accel, desired_lateral_jerk, v_ego)      # la, jerk, v
get_bolt_2022_2023_friction_scale(v_ego, desired_lateral_accel, desired_lateral_jerk)      # v, la, jerk
get_bolt_2022_2023_friction_threshold(v_ego, desired_lateral_accel, desired_lateral_jerk)  # v, la, jerk
```

A swapped call still returns plausible floats near 1.0 and raises nothing. Copy the call order from the controller itself ([:2030-2032](../selfdrive/controls/lib/latcontrol_torque.py)) rather than from memory.

### 1c. Inputs are exact, not approximate

`ControlSample.desired_la` is `torqueState.desiredLateralAccel`, logged verbatim as `float(setpoint)` ([:2116](../selfdrive/controls/lib/latcontrol_torque.py)), and `desired_jerk` is `float(desired_lateral_jerk)` ([:2117](../selfdrive/controls/lib/latcontrol_torque.py)) — precisely the two values the controller feeds these functions, alongside `CS.vEgo`. The reconstruction is exact. **No schema change and no re-drive are needed.**

### 1d. The helper

Add above `main` (file convention: helpers precede `main`). Given `samples` and `car_fingerprint`, return four parallel float arrays of `len(samples)`:

| array | value |
|---|---|
| `ff_scale` | `get_bolt_2022_2023_ff_scale(...)` |
| `friction_scale` | `get_bolt_2022_2023_friction_scale(...)` |
| `friction_threshold` | `get_bolt_2022_2023_friction_threshold(...)` |
| `threshold_ratio` | `friction_threshold / get_friction_threshold(s.v_ego)` |

Plain list comprehensions are fine — the functions are scalar-only, ~200k samples costs a few seconds, and this is a one-shot report.

Gate on `car_fingerprint in BOLT_2022_2023_CARS` — **not `BOLT_CARS`**. The 2018-2021 path has no `ff_scale` at all ([:2033](../selfdrive/controls/lib/latcontrol_torque.py)), so a `BOLT_CARS` gate would reconstruct gains that car never applied. Off-gate, return all-NaN arrays and print one skip line so the section degrades cleanly on other platforms.

Note `get_bolt_2022_2023_ff_scale` early-returns `1.0` when `desired_lateral_accel == 0.0` ([:975](../selfdrive/controls/lib/latcontrol_torque.py)); `friction_scale` is clamped to `[0.92, 1.22]` ([:1022](../selfdrive/controls/lib/latcontrol_torque.py)) and the threshold scale to `[0.84, 1.14]` ([:1009](../selfdrive/controls/lib/latcontrol_torque.py)). Do not re-clamp or special-case any of this yourself.

### 1e. The range summary line

Print this **before** the tables. It is the kill test for the whole hypothesis and must be readable without parsing a grid. Over active, non-`steering_pressed` samples only:

```
Bolt dynamic gains:
  ff_scale [1.0000,1.1043] med=1.0021  friction_scale [0.9200,1.2200] med=1.0900  thresh_ratio [0.8400,1.1400] med=1.0000
```

## Task 2 — Part B: oscillation detector

### 2a. Segmentation

Contiguous runs of `torque_active`, breaking on inactive, on `steering_pressed`, or on a monotime gap `> 1.5 * DT_CTRL`. Reuse the gap-guard idiom already in `reconstruct_unwind` ([analyze_bolt_lateral.py:64](../tools/tuning/analyze_bolt_lateral.py)).

**Use `torque_active`, never `lat_active`** — Chunk 2 established that `lat_active` comes from a separate `carControl` message and can be one frame stale at exactly the engagement boundaries that start and end segments.

### 2b. Signal and detrending

Signal is the tracking error `desired_la - actual_la`. Per segment: detrend against a centered ~1 s (101-sample) moving mean. Segments shorter than the analysis window are skipped entirely. The half-window at each segment edge is marked NaN, not evaluated against a partial mean.

### 2c. Window grid

Evaluate on a **0.25 s hop grid**: windows of ~2 s (201 samples) stepped 25 samples. Per-sample evaluation would mean ~200k FFTs; the hop grid is ~8k, and the extra time resolution buys nothing at these bin sizes.

**Each window's verdict is assigned to the hop block at its _center_, not at its start:**

```python
c0 = start_i + w_start + (window_len - hop_step) // 2
osc[c0:c0 + hop_step] = True
osc_hz[c0:c0 + hop_step] = dom_freq
```

Assigning to `[w_start, w_start + hop_step)` attributes a 2 s window's verdict to its leading 0.25 s — ~88 samples ≈ 0.9 s ahead of the behaviour that produced it. At 8 m/s that is ~7 m of road, enough to move a sample out of its `|desired_la|` row. The whole point of Part C is *which* band lights up, so this offset is not cosmetic.

Also return a third array, `evaluated` (bool). Set it `True` for the hop block of **every window that cleared the NaN check — qualifying or not.** See 2f.

### 2f. Distinguish "tested and clean" from "never tested"

`osc` is initialised `False` and only ever written `True`, so a sample in a segment shorter than 201, inside the 50-sample NaN edge, or in a skipped window is indistinguishable in the table from one that was tested and found clean. Without the `evaluated` gate, `frac` divides by both and every cell reads low — and *unevenly*, since short segments and engagement edges cluster at low speed, which is exactly the `<6` / `6–10` columns the hypothesis lives in. A cell can read 0.02 when the tested samples were 0.4.

### 2d. Criteria

Both required. Define as module-level constants next to the Chunk 2 block so they are tunable in one place:

```python
OSC_MIN_CROSSINGS = 6     # sign changes of detrended error per ~2 s window (~1.5 Hz floor)
OSC_MIN_P2P = 0.15        # m/s^2, peak-to-peak, so sensor noise is not counted
```

### 2e. Dominant frequency

For qualifying windows only: Hann-windowed `np.fft.rfft`, argmax of magnitude **excluding DC**, sample rate `1 / DT_CTRL = 100 Hz`. numpy only — no new dependency.

Return `osc` (bool array), `osc_hz` (float array, NaN where not oscillating), and `evaluated` (bool array, per 2f).

## Task 3 — Part C: the band tables

Rows — `|desired_la|` bins: `0–0.2, 0.2–0.4, 0.4–0.7, 0.7–1.1, 1.1–1.6, 1.6+`.
Columns — `v_ego` bins: `<6, 6–10, 10–14, 14–20, 20+`.
Sample gate: `torque_active & ~steering_pressed & evaluated & isfinite(desired_la)`, where `evaluated` is the third array from Task 2. Without it, `frac` is diluted by samples the detector never looked at — see 2f.

Print **four grids**: oscillation and gains, each split left (`desired_la > 0`) and right (`desired_la < 0`). Five numbers per cell in a single grid is unreadable at terminal width; the split keeps each cell to `frac/medHz/n` and `medFF/medFric` respectively. Start the section header with `\n`, as every other section does.

**Carry Chunk 2's empty-bucket rule forward: a cell with `n == 0` prints `--` in every column, never `0.0000`.** That rule was learned on the table a go/no-go decision hung on; this is another one, and `0.0000` in an empty right-side cell reads as "no oscillation here" when it means "no data here". Mark thin cells visibly too (e.g. parenthesise `n` below some minimum) so they are not read as signal.

Do **not** bin on `curvature`. The re-scoped table bins on `|desired_la|` × `v_ego`. Chunk 1b's NaN-below-1-m/s fix stands and nothing here consumes that field.

---

## Review outcome — first implementation pass

A first pass landed `reconstruct_bolt_2022_2023_gains`, `summarize_bolt_dynamic_gains`, `detect_oscillations` and `summarize_band_tables` in `tools/tuning/analyze_bolt_lateral.py` (+219 lines, single file) and was reviewed.

**Correct and to be preserved.** All three Bolt call orders match the controller, including the two that differ from `ff_scale` — this was the flagged trap and it was avoided. Gate is `BOLT_2022_2023_CARS`, not `BOLT_CARS`, with an all-NaN return and a skip line off-gate. Segmentation breaks on inactive, `steering_pressed` and the `1.5 * DT_CTRL` gap, and uses `torque_active` throughout. Detrend window is correctly centered (`err[i-50 : i+51]`) and segment edges are NaN rather than evaluated against a partial mean. FFT excludes DC, is Hann-windowed, and uses `rfftfreq(201, d=DT_CTRL)`. Empty cells print `--`; thin cells are parenthesised. All new output is appended after `summarize_torque_points`, leaving every pre-existing section untouched. Helpers precede `main`.

**Two fixes required, both folded into the tasks above; this is the changelog.**

1. **Window verdicts were assigned to the window's leading hop rather than its center** — `block_start = start_i + w_start` put each 2 s verdict ~0.9 s ahead of the behaviour that produced it, smearing samples across `|desired_la|` rows. Since the discriminating test is which row lights up, this had to be fixed before the run rather than after. See 2c.
2. **`frac` counted never-evaluated samples as non-oscillating** — no `evaluated` array existed, so untested samples (short segments, NaN edges, skipped windows) sat in the denominator and biased every cell downward, most heavily in the low-speed columns of interest. See 2f and Task 3's sample gate.

**Four nits, worth folding in while the file is open, none blocking:**

- `OSC_MIN_CROSSINGS` / `OSC_MIN_P2P` were defined mid-file rather than with `DT_CTRL` at the top — the same convention slip the Chunk 2 review corrected once already.
- `friction_threshold` is unpacked in `main` and never used; only `threshold_ratio` is consumed. Ruff will flag it. Either drop it from the unpacking or use `_`.
- On a non-Bolt car the two gains grids still print, full of `--`. Skip them when the fingerprint is off-gate, as `summarize_bolt_dynamic_gains` already does.
- Sign changes use `(sub[:-1] * sub[1:]) < 0`, so exact zeros are not counted. Negligible on float data; noted only so it is not mistaken for a bug later.

**Expected runtime.** `reconstruct_bolt_2022_2023_gains` calls three Python functions per sample over ~200k samples, and the detrend does a `np.mean` over a 101-slice per sample in a Python loop. Roughly 20–40 s added to a previously fast run. Acceptable for a one-shot report — do not "optimise" it into a vectorised reimplementation of the controller's functions, which would defeat the point of importing them.

## Review outcome — second implementation pass

**The two requested fixes landed correctly. Then Part A was deleted, and the script no longer runs.**

### What went right — preserve all of it

- **Centering fix** ([analyze_bolt_lateral.py:489](../tools/tuning/analyze_bolt_lateral.py)): `c0 = start_i + w_start + (window_len - hop_step) // 2`, with `osc` / `osc_hz` written to `[c0, c_end)`. Correct.
- **`evaluated` array** ([:491](../tools/tuning/analyze_bolt_lateral.py)): set **before** the crossings/p2p test, so it marks every window that cleared the NaN check whether or not it qualified. That is exactly the distinction 2f asked for and the easy thing to get subtly wrong. Wired into `base_mask` at [:524](../tools/tuning/analyze_bolt_lateral.py).
- Constants moved up beside the Chunk 2 block; gains grids now skip cleanly off-gate; `friction_threshold` unpacked as `_`.

### What went wrong

`reconstruct_bolt_2022_2023_gains` and `summarize_bolt_dynamic_gains` were **removed from the file**, while [:782-785](../tools/tuning/analyze_bolt_lateral.py) still calls both. The script raises `NameError` the moment `main` reaches the new section — which is *after* the full log parse, so it burns the entire route read before failing.

Gone with them: the `Bolt dynamic gains:` range summary line from 1e, which is **the stated kill test for the whole hypothesis** and the one line in this chunk that must be readable without parsing a grid. Two imports are now unused.

Total diff versus HEAD is +164 lines; the reviewed first pass was +219. The 77 removed lines are Part A being dropped, not refactored.

### Read this before the next pass

**The task was to apply two localised fixes to `detect_oscillations` and one mask in `summarize_band_tables`. Nothing in that scope touches Part A.** Deleting ~77 lines of already-reviewed, already-correct code — including the chunk's headline output — is not an acceptable side effect of a two-line change, and it was reported as `COMPLETE` without the script having been run or its call graph checked even once.

Three rules for this pass, and for every future one:

1. **Do not delete or rewrite code the task did not name.** If a fix seems to require touching something outside its scope, stop and say so rather than doing it silently.
2. **Check your diff before reporting.** `git diff --stat` shrinking by 77 lines on a two-fix change is a signal something is wrong, visible in one command, without a route or a log.
3. **Do not mark a task `COMPLETE` on unexecuted code.** `py_compile` passing is not evidence that the program runs — an undefined name is a runtime error, not a syntax error, so step 6 as written could never have caught this. If a step cannot be run, report it as unrun; do not report it as passed.

### Fix required

Part A was correct when it existed and is recoverable verbatim from the staged blob — it is still in git's index from the first pass:

```bash
git show :tools/tuning/analyze_bolt_lateral.py > "$SCRATCH/chunk4_first_pass.py"
```

Restore `reconstruct_bolt_2022_2023_gains` and `summarize_bolt_dynamic_gains` from that blob **unchanged**, placed between `summarize_bolt_effective_tune` and `detect_oscillations`. Both were reviewed and passed: correct call orders including the two that differ from `ff_scale`, correct `BOLT_2022_2023_CARS` gate, all-NaN return and skip line off-gate. Do not re-derive them from the task text, and do not "improve" them on the way back in. The only change is the one already made at the call site: `friction_threshold` stays unpacked as `_`.

Then confirm the `Bolt dynamic gains:` line from 1e prints, and re-run the checks in Verify — **including the new step 6, which must pass before this is reported complete.**

## Verify

```bash
python tools/tuning/analyze_bolt_lateral.py <dongle>/<route>
```

1. Every pre-existing section byte-identical to the Chunk 1b run, **including** `TorqueEstimator fit: latAccelFactor=1.1087 latAccelOffset=-0.3454 friction=0.1623 bucket_points=11522`. Chunk 1b seeded the RNG precisely so this line is now part of the regression surface. Diff to confirm.
2. Run twice; whole output identical.
3. **Sanity-gate Part A before reading anything into it.** `ff_scale` must sit at ~1.0 in the `0–0.2` row and at `20+` speeds, peaking near 1.11 left / 1.06 right in the mid-`|la|` low-speed cells. If the max is at or below ~1.0 everywhere, the arguments were swapped — **that is not evidence the hypothesis is dead.** Re-check the call order against [:2030-2032](../selfdrive/controls/lib/latcontrol_torque.py) before concluding anything.
4. **Sanity-gate Part B.** Oscillating fraction near zero in the center row and the high-speed columns, which drive fine. If every cell lights up, `OSC_MIN_P2P` is too low. If nothing lights up anywhere, it is too high — say so rather than reporting an all-clear. Cell `n` totals should now be visibly *smaller* than before the `evaluated` gate landed, since untested samples no longer count; if they are unchanged, the gate is not wired into `base_mask`.
5. **Then the discriminating test.** Do the oscillating cells coincide with the `ff_scale` peak band — rows `0.2–0.4` / `0.4–0.7` at `<6` / `6–10`, where the onset sigmoid has opened and the low-speed weight is still near 1? Coincidence ⇒ the dynamic gain layer is the mechanism. `ff_scale` genuinely flat near 1.0 across all cells, with step 3 passed ⇒ this hypothesis is dead and static-friction over-application returns as the explanation.
6. **Call-graph check — run this before anything that needs a route.** `py_compile` alone is insufficient: it parses, it does not resolve names, so a function deleted out from under its caller passes it. This catches that class of defect in a second, with no log:

```bash
python -m py_compile tools/tuning/analyze_bolt_lateral.py && python -c "import ast; src=open('tools/tuning/analyze_bolt_lateral.py').read(); t=ast.parse(src); defined={n.name for n in ast.walk(t) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}; imported={a.asname or a.name for n in ast.walk(t) if isinstance(n,(ast.Import,ast.ImportFrom)) for a in n.names}; called={n.func.id for n in ast.walk(t) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)}; missing=sorted(called-defined-imported-set(dir(__builtins__))); print('MISSING:',missing) if missing else print('call graph OK')"
```

Must print `call graph OK`. Any name listed under `MISSING:` is a call with no definition — fix it before reading a single line of report output.

Report the grids plainly whatever they show. As in Chunk 2, a result that kills the hypothesis is a useful outcome, not a failure to be explained away.

## Out of scope

Do not implement the direction/speed splits of the existing `masks` tuple (Chunk 3 — superseded, do not build), turn-in event analysis (Chunk 5), or multi-route support (Chunk 6). Do not modify `latcontrol_torque.py`, `cereal/log.capnp`, or any tune value — importing from `latcontrol_torque.py` is in scope, editing it is not. Do not fix the `unwind_detected` sign error, and **do not re-tune the `BOLT_2022_2023_*` constants even if Part A shows them to be the culprit** — that is a controller change to lateral behaviour and needs its own review and its own before/after drive. This chunk is diagnosis only.
