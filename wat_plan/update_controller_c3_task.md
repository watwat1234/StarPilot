# Chunk C3 — analyzer repair — task list for a Gemini Flash subagent

**Status:** Completed (2026-08-08) — Repaired import in `analyze_bolt_lateral.py` and updated unwind reconstruction to report both old and new classifications side-by-side.

## Context

C1 shipped: `unwind_detected` in `selfdrive/controls/lib/latcontrol_torque.py` now tests the **magnitude** rate of the
lateral-accel setpoint instead of the signed rate (commit `d55b679f5`). A post-fix drive has been recorded. C5 will
measure the result.

C3 is the gate before any of that runs: `tools/tuning/analyze_bolt_lateral.py` currently **fails at import**, and —
beyond what `update_controller.md` specifies — its unwind reconstruction still models the *old* controller.

**Verified against current source before writing this list.** The plan doc's line numbers are stale; these are right:

| claim | status |
|---|---|
| `get_friction_threshold` no longer exists | ✅ zero definitions repo-wide; import at [line 19](../tools/tuning/analyze_bolt_lateral.py), use at [line 469](../tools/tuning/analyze_bolt_lateral.py) |
| Replacement is `get_gm_base_friction_threshold(v_ego)` | ✅ `latcontrol_vehicle_tunes.py:1021` |
| Mirrors the `is_gm` brand dispatch | ✅ at `latcontrol_torque.py:302` — plan doc says 328-333 |
| Other 8 names in the import block still resolve | ✅ |
| `reconstruct_unwind` models the **old** signed logic | ⚠️ **yes** — `analyze_bolt_lateral.py:86-88`, not in the plan doc |

### Why the reconstruct change was added to C3

`reconstruct_unwind` recomputes `unwind_detected` from logged `desiredLateralAccel` using its own local copy of the
condition. That copy is still the pre-C1 signed form. Run unchanged against the post-fix drive, it reproduces what the
**old** code would have done — `entering_right` returns ~0.0777 again — and C5's primary criterion reads as "the fix
didn't work." A false negative on the one measurement the whole plan rests on.

**Decision taken with the user:** report **both** classifications side by side rather than swapping one for the other.
The old and new columns then show the flip within a single run, so C5 does not depend on trusting the archived Chunk 5
numbers from a different analyzer build.

### The trap in doing that

`d_des_rate` is not only used for the unwind test — it drives the phase classification at
[lines 122-125](../tools/tuning/analyze_bolt_lateral.py) (`entering_right` is `desired_la < 0 and d_des_rate < 0`).
It **must stay signed**. The magnitude rate has to live in a separate array. Overwriting `d_des_rate` with the
magnitude rate collapses the phase buckets and makes both columns meaningless.

---

## Subagent prompt

Model: Gemini Flash. Scope: one file, three edits. Nothing else.

> ### Task
>
> Repair `tools/tuning/analyze_bolt_lateral.py`. It currently fails at import. Three independent edits, all in this
> one file.
>
> ---
>
> #### Edit 1 — fix the broken import (lines 11-20)
>
> The name `get_friction_threshold` no longer exists in the codebase. Its replacement is
> `get_gm_base_friction_threshold`, which takes the same single `v_ego` argument.
>
> **Current:**
>
> ```python
> from openpilot.selfdrive.controls.lib.latcontrol_torque import (
>   BOLT_CARS,
>   BOLT_2022_2023_CARS,
>   DEADZONE_BOOST_LAT_ACCEL,
>   FF_SCALE_BLEND_LAT_ACCEL,
>   get_bolt_2022_2023_ff_scale,
>   get_bolt_2022_2023_friction_scale,
>   get_bolt_2022_2023_friction_threshold,
>   get_friction_threshold,
> )
> ```
>
> **Replace with:**
>
> ```python
> from openpilot.selfdrive.controls.lib.latcontrol_torque import (
>   BOLT_CARS,
>   BOLT_2022_2023_CARS,
>   DEADZONE_BOOST_LAT_ACCEL,
>   FF_SCALE_BLEND_LAT_ACCEL,
>   get_bolt_2022_2023_ff_scale,
>   get_bolt_2022_2023_friction_scale,
>   get_bolt_2022_2023_friction_threshold,
>   get_gm_base_friction_threshold,
> )
> ```
>
> Then at **line 469**, change the single call site:
>
> ```python
>     base_th = get_friction_threshold(v)
> ```
>
> to:
>
> ```python
>     base_th = get_gm_base_friction_threshold(v)
> ```
>
> Line 469 is the only call site. Do not search for others.
>
> ---
>
> #### Edit 2 — report both old and new unwind classifications
>
> `reconstruct_unwind` still implements the pre-fix signed condition. The shipped controller now uses a magnitude
> rate. Compute **both** and return both. Do not delete the old one — the comparison is the point.
>
> **Critical:** `d_des_rate` (the signed rate) is returned and used elsewhere for phase classification. It must keep
> its current signed values. Put the magnitude rate in a **new, separate** array. Do not overwrite `d_des_rate`.
>
> **Current (lines 65-91):**
>
> ```python
> def reconstruct_unwind(samples: list[ControlSample]) -> tuple[np.ndarray, np.ndarray]:
>   n = len(samples)
>   unwind = np.zeros(n, dtype=bool)
>   d_des_rate = np.full(n, np.nan, dtype=float)
>
>   prev_setpoint = 0.0
>
>   for k, s in enumerate(samples):
>     if not s.torque_active:
>       prev_setpoint = 0.0
>       unwind[k] = False
>       d_des_rate[k] = np.nan
>       continue
>
>     # Gap guard: k == 0 or prev sample inactive or time gap > 1.5 * DT_CTRL
>     if k == 0 or not samples[k - 1].torque_active or ((s.mono_time - samples[k - 1].mono_time) / 1e9 > 1.5 * DT_CTRL):
>       unwind[k] = False
>       d_des_rate[k] = np.nan
>       prev_setpoint = s.desired_la
>       continue
>
>     rate = (s.desired_la - prev_setpoint) / DT_CTRL
>     d_des_rate[k] = rate
>     unwind[k] = (rate < UNWIND_D_DES_THRESHOLD) and (abs(s.desired_la) < UNWIND_LAT_ACCEL_NEAR_ZERO)
>     prev_setpoint = s.desired_la
>
>   return unwind, d_des_rate
> ```
>
> **Replace with:**
>
> ```python
> def reconstruct_unwind(samples: list[ControlSample]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
>   """Reconstruct the integrator-freeze gate both ways.
>
>   unwind_old mirrors the pre-d55b679f5 controller, which tested the signed setpoint rate.
>   unwind_new mirrors the shipped controller, which tests the magnitude rate. Both are reported
>   so a single run shows the flip directly.
>
>   d_des_rate stays SIGNED: the phase classifier keys entering/exiting off its sign.
>   """
>   n = len(samples)
>   unwind_old = np.zeros(n, dtype=bool)
>   unwind_new = np.zeros(n, dtype=bool)
>   d_des_rate = np.full(n, np.nan, dtype=float)
>
>   prev_setpoint = 0.0
>
>   for k, s in enumerate(samples):
>     if not s.torque_active:
>       prev_setpoint = 0.0
>       unwind_old[k] = False
>       unwind_new[k] = False
>       d_des_rate[k] = np.nan
>       continue
>
>     # Gap guard: k == 0 or prev sample inactive or time gap > 1.5 * DT_CTRL
>     if k == 0 or not samples[k - 1].torque_active or ((s.mono_time - samples[k - 1].mono_time) / 1e9 > 1.5 * DT_CTRL):
>       unwind_old[k] = False
>       unwind_new[k] = False
>       d_des_rate[k] = np.nan
>       prev_setpoint = s.desired_la
>       continue
>
>     near_zero = abs(s.desired_la) < UNWIND_LAT_ACCEL_NEAR_ZERO
>     rate = (s.desired_la - prev_setpoint) / DT_CTRL
>     mag_rate = (abs(s.desired_la) - abs(prev_setpoint)) / DT_CTRL
>     d_des_rate[k] = rate
>     unwind_old[k] = (rate < UNWIND_D_DES_THRESHOLD) and near_zero
>     unwind_new[k] = (mag_rate < UNWIND_D_DES_THRESHOLD) and near_zero
>     prev_setpoint = s.desired_la
>
>   return unwind_old, unwind_new, d_des_rate
> ```
>
> ---
>
> #### Edit 3 — print both columns
>
> Update `summarize_unwind_reconstruction` (starts line 94) to accept and print both.
>
> **3a.** Change the signature:
>
> ```python
> def summarize_unwind_reconstruction(samples: list[ControlSample], unwind: np.ndarray, d_des_rate: np.ndarray) -> None:
> ```
>
> to:
>
> ```python
> def summarize_unwind_reconstruction(samples: list[ControlSample], unwind_old: np.ndarray, unwind_new: np.ndarray,
>                                     d_des_rate: np.ndarray) -> None:
> ```
>
> **3b.** Replace the overall-fraction line (line 112):
>
> ```python
>   overall_unwind_frac = np.mean(unwind[torque_active_mask]) if active_count > 0 else 0.0
> ```
>
> with:
>
> ```python
>   overall_unwind_old = np.mean(unwind_old[torque_active_mask]) if active_count > 0 else 0.0
>   overall_unwind_new = np.mean(unwind_new[torque_active_mask]) if active_count > 0 else 0.0
> ```
>
> **3c.** Replace the two header prints (lines 133-136):
>
> ```python
>   print(
>     f"  active_samples={active_count:5d} unwind_frac={overall_unwind_frac:.4f} unclassified={unclassified_count:5d}"
>   )
>   print("  phase            n      unwind_frac  mean|i|     bias    mean|d_des|")
> ```
>
> with:
>
> ```python
>   print(
>     f"  active_samples={active_count:5d} unwind_old={overall_unwind_old:.4f} "
>     f"unwind_new={overall_unwind_new:.4f} unclassified={unclassified_count:5d}"
>   )
>   print("  phase            n      unwind_old  unwind_new   mean|i|     bias  mean|d_des|")
> ```
>
> **3d.** Replace the per-phase loop body (lines 145-156). Current:
>
> ```python
>   for name, mask in phases:
>     n = int(mask.sum())
>     if n == 0:
>       u_str, i_str, b_str, d_str = "--", "--", "--", "--"
>     else:
>       u_str = f"{np.mean(unwind[mask]):.4f}"
>       i_str = f"{np.mean(np.abs(i_term[mask])):.4f}"
>       b_str = f"{np.mean(actual_la[mask] - desired_la[mask]):+.4f}"
>       d_str = f"{np.mean(np.abs(d_des_rate[mask])):.4f}"
>     print(
>       f"  {name:16s} {n:5d}       {u_str:>6s}   {i_str:>6s}  {b_str:>7s}        {d_str:>6s}"
> ```
>
> Replace with:
>
> ```python
>   for name, mask in phases:
>     n = int(mask.sum())
>     if n == 0:
>       uo_str, un_str, i_str, b_str, d_str = "--", "--", "--", "--", "--"
>     else:
>       uo_str = f"{np.mean(unwind_old[mask]):.4f}"
>       un_str = f"{np.mean(unwind_new[mask]):.4f}"
>       i_str = f"{np.mean(np.abs(i_term[mask])):.4f}"
>       b_str = f"{np.mean(actual_la[mask] - desired_la[mask]):+.4f}"
>       d_str = f"{np.mean(np.abs(d_des_rate[mask])):.4f}"
>     print(
>       f"  {name:16s} {n:5d}  {uo_str:>10s}  {un_str:>10s}  {i_str:>8s}  {b_str:>7s}  {d_str:>10s}"
> ```
>
> Keep whatever follows that f-string (the closing paren of the `print`) exactly as it is.
>
> **3e.** Update the call site at **lines 946-947**:
>
> ```python
>   unwind, d_des_rate = reconstruct_unwind(control_samples)
>   summarize_unwind_reconstruction(control_samples, unwind, d_des_rate)
> ```
>
> to:
>
> ```python
>   unwind_old, unwind_new, d_des_rate = reconstruct_unwind(control_samples)
>   summarize_unwind_reconstruction(control_samples, unwind_old, unwind_new, d_des_rate)
> ```
>
> ---
>
> ### Hard constraints — do not violate
>
> 1. **Edit only `tools/tuning/analyze_bolt_lateral.py`.** No other file may be touched — in particular nothing under
>    `selfdrive/`, and never `latcontrol_torque.py` or `latcontrol_vehicle_tunes.py`.
> 2. **Do not change** `UNWIND_D_DES_THRESHOLD` (-1.0) or `UNWIND_LAT_ACCEL_NEAR_ZERO` (0.3) at lines 47-48, or
>    `DT_CTRL` (0.01) at line 46.
> 3. **`d_des_rate` must remain the signed rate.** The phase classifier at lines 122-125 keys `entering_right` /
>    `exiting_right` off its sign. Do not substitute the magnitude rate into it.
> 4. **Do not delete the old classification.** Both columns are required output.
> 5. **Do not touch** the phase mask definitions (lines 114-130), `PHASE_LAT_ACCEL_DEADBAND`, or
>    `PHASE_RATE_DEADBAND`.
> 6. **Do not touch** `np.random.seed(0)` at line 766. Run-to-run reproducibility depends on it.
> 7. **Do not touch** the Chunk 5 event-table code or its formatting. There is a known field-width bug in that table
>    (`medFF`/`medFric`/`n`); it is deliberately out of scope. Leave it broken.
> 8. **Do not run the analyzer, do not run tests, do not run git commands, do not commit or stage anything.** The
>    analyzer needs a route file you do not have.
> 9. Do not refactor, rename, or tidy anything not listed above.
>
> ### Report back
>
> - The full diff.
> - Confirmation that only `tools/tuning/analyze_bolt_lateral.py` changed.
> - Confirmation that `d_des_rate` is still computed as `(s.desired_la - prev_setpoint) / DT_CTRL`.
> - If any "current" block above does not match the file verbatim, **stop and report** rather than guessing.

---

## Verification (user runs, after the subagent returns)

1. Diff confined to `tools/tuning/analyze_bolt_lateral.py`.
2. Import no longer raises:
   ```bash
   python -c "import openpilot.tools.tuning.analyze_bolt_lateral"
   ```
3. Run against a route; it should complete rather than dying at import:
   ```bash
   python tools/tuning/analyze_bolt_lateral.py <route>
   ```
4. **Determinism** (per the plan doc's C3 verify): two consecutive runs on the same route byte-identical.
5. **Sections that predate C3 must be unchanged** against the last recorded output — the import repair and the
   reconstruct change should not perturb the tracking summary or the event table.
6. **Sanity-check the new columns on the pre-fix baseline route**, where the answer is already known: `unwind_old`
   for `entering_right` should reproduce ≈0.0777 and `exiting_right` ≈0.0000. If it does not, the reconstruction
   drifted and the post-fix numbers cannot be trusted.

## Out of scope for this chunk

C4 (FLM awareness, `center_output_scale` column, build provenance), C5 (the before/after run), the Chunk 5b
table-formatting bug, and any change to the controller or tune values.

---

## Subagent Execution Report

- **Date:** 2026-08-08
- **Status:** Completed
- **Target File:** `tools/tuning/analyze_bolt_lateral.py` (only file modified)
- **Confirmations:**
  1. **Only file touched:** `tools/tuning/analyze_bolt_lateral.py`.
  2. **`d_des_rate` signed rate preservation:** Confirmed `d_des_rate[k] = (s.desired_la - prev_setpoint) / DT_CTRL` remains signed for phase classification while magnitude rate is handled separately in `mag_rate = (abs(s.desired_la) - abs(prev_setpoint)) / DT_CTRL`.
  3. **Diff overview:** Fixed stale import `get_friction_threshold` -> `get_gm_base_friction_threshold`, updated `reconstruct_unwind` to return `unwind_old` and `unwind_new`, updated `summarize_unwind_reconstruction` to format both columns side-by-side.

