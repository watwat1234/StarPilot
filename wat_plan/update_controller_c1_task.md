# Chunk C1 — task list for a Gemini Flash subagent

**Status:** Completed (2026-08-08) — Fixed magnitude rate calculation for `unwind_detected` in `selfdrive/controls/lib/latcontrol_torque.py`.

## Context

`wat_plan/update_controller.md` diagnoses a sign inversion in the `unwind_detected` condition in
`selfdrive/controls/lib/latcontrol_torque.py`. The condition tests a **signed** lateral-accel rate where it means to
test whether the *magnitude* of the setpoint is decreasing. Consequence: entering a right turn from straight
(setpoint 0 → −2) the rate is negative, so the integrator freeze fires during turn-in; exiting a right turn
(−2 → 0) it never fires. Left turns get the intended behaviour on both. The freeze during right turn-in is the
mechanism behind the driver's reported symptom — a right 90° turn where the wheel slams right, then overcorrects left.

The fix is applied **globally** (private fork; the condition is wrong on every platform), as a **single-variable
change**, so the existing recorded route stays a valid before/after baseline. C2–C5 are out of scope here.

**Verified against current source before writing this list** (the plan doc's line numbers are stale):

| claim | status |
|---|---|
| Condition location | `latcontrol_torque.py:229-231` — plan doc says 217 |
| Feeds `freeze_integrator` | `latcontrol_torque.py:420-421` — plan doc says 397-398 |
| `desired_lateral_accel_rate` used elsewhere | **No.** Line 229 assigns, line 230 is the only reader. Whole repo checked. |
| Tests pin `unwind_detected` / `freeze_integrator` | **No.** Every `unwind` hit in `test_latcontrol.py` is a `tanh` gain function. |
| Constants | `UNWIND_D_DES_THRESHOLD = -1.0`, `UNWIND_LAT_ACCEL_NEAR_ZERO = 0.3` at lines 45-46 |

**Decisions taken with the user:** redefine the rate variable rather than inlining the expression (avoids a dead
local); the subagent makes the edit only — no tests, no commit.

---

## Subagent prompt

Model: Gemini Flash. Scope: one file, one line, plus one comment. Nothing else.

> ### Task
>
> In `selfdrive/controls/lib/latcontrol_torque.py`, fix a sign bug on **line 229**.
>
> The variable `desired_lateral_accel_rate` currently computes a *signed* rate of change of the lateral-accel
> setpoint. It is consumed on line 230 by `unwind_detected`, which is supposed to detect the wheel *unwinding* —
> i.e. the steering command's **magnitude** decreasing toward center. Testing the signed value makes the check fire
> when turning right (setpoint going more negative) and never fire when releasing a right turn. It must test the
> magnitude.
>
> **Current code (lines 229-232):**
>
> ```python
>       desired_lateral_accel_rate = (setpoint - self.prev_desired_lateral_accel) / self.dt
>       unwind_detected = (desired_lateral_accel_rate < UNWIND_D_DES_THRESHOLD and
>                          abs(setpoint) < UNWIND_LAT_ACCEL_NEAR_ZERO)
>       self.prev_desired_lateral_accel = setpoint
> ```
>
> **Replace with:**
>
> ```python
>       # magnitude rate, not signed rate: unwinding means |setpoint| is shrinking toward center.
>       # A signed rate fires on right turn-in (setpoint 0 -> -2) and never on right turn-out.
>       desired_lateral_accel_rate = (abs(setpoint) - abs(self.prev_desired_lateral_accel)) / self.dt
>       unwind_detected = (desired_lateral_accel_rate < UNWIND_D_DES_THRESHOLD and
>                          abs(setpoint) < UNWIND_LAT_ACCEL_NEAR_ZERO)
>       self.prev_desired_lateral_accel = setpoint
> ```
>
> Preserve the existing indentation exactly (six spaces — this sits inside `if/else` inside `update()`).
>
> ### Hard constraints — do not violate
>
> 1. **Edit only `selfdrive/controls/lib/latcontrol_torque.py`.** No other file in the repo may be touched.
> 2. **Do not change** `UNWIND_D_DES_THRESHOLD` (-1.0) or `UNWIND_LAT_ACCEL_NEAR_ZERO` (0.3) on lines 45-46. Changing
>    a threshold in the same commit would forfeit the single-variable property this change depends on.
> 3. **Do not rename** `desired_lateral_accel_rate`, and do not delete its assignment.
> 4. **Do not touch** line 232 (`self.prev_desired_lateral_accel = setpoint`) or line 206
>    (`self.prev_desired_lateral_accel = future_desired_lateral_accel` in the inactive branch).
> 5. **Do not touch** the `freeze_integrator` expression at lines 420-421, or anything in
>    `latcontrol_vehicle_tunes.py`.
> 6. **Do not gate the change by car model.** It is deliberately global — it sits on the shared path above every
>    car-specific branch, and that is intended.
> 7. **Do not run tests, do not run git commands, do not commit or stage anything.**
> 8. Do not "improve" anything else you notice in the file. Out-of-scope cleanups break the baseline.
>
> ### Report back
>
> - The exact diff produced.
> - Confirmation that the changed lines are 229-231 and that no other file was modified.
> - If line 229 does not match the "current code" block above verbatim, **stop and report** rather than guessing —
>   the file has moved since this task was written.

---

## Verification (user runs, after the subagent returns)

1. Diff is confined to one file, one changed expression plus a two-line comment.
2. `UNWIND_D_DES_THRESHOLD` / `UNWIND_LAT_ACCEL_NEAR_ZERO` unchanged.
3. Sanity-check the semantics by hand: right turn-in, setpoint 0 → −2 gives `abs` delta **positive** → no longer
   fires. Right turn-out, −2 → 0 gives `abs` delta **negative** → now fires. Left turns unchanged.
4. C2 (deferred to the user): `pytest selfdrive/controls/tests/test_latcontrol.py`. Expected green unchanged —
   nothing in that file pins this behaviour. A failure means something depends on the broken behaviour and must be
   understood before driving.

## Out of scope for this chunk

C2 test run and drive, C3–C5 analyzer work, the Chunk 5b formatting bug, any tune-value or FLM change, and the
fallback levers (`ff_gain_right`, `turn_in_boost_right`, `unwind_taper_right`) — bundling any of those would destroy
the single-variable property.
