# Chunk T1 — analyzer completeness — implementation record + handoff

**Status:** Code complete, reviewed, follow-up edits applied, **unverified, uncommitted.** All six items plus the two follow-up edits (`_param_float` revert and `flm=` no-initData handling) are implemented in `tools/tuning/analyze_bolt_lateral.py` in the working tree. Pending route verification and commit.

## Context

`update_tune.md` stages a correction of the Bolt's base torque parameters across several drives (T1 → T5). T1 is the
gate before any of that runs. One file, no driving, no controller change, no tune values.

T1 grew from two items to six after the FLM report `flm-1786229999.json` was reviewed against the analyzer. What is
now in the tree:

| # | item | where (current line numbers) |
|---|---|---|
| 1 | build provenance from `initData` | capture 835-840, print 890-899 |
| 2 | `center_output_scale` column | import 16, reconstruct 480/492/494, summary 503/521/527, call site 1031-1035 |
| 3 | **`force_auto_tune_off` mirror fix** | 377 |
| 4 | route identity | 889 |
| 5 | delay labelling | 901-904 |
| 6 | FLM state tripwire | `_param_str` 349-357, print 905-914 |

**Item 3 is the only correctness item.** The other five are provenance and labelling. Item 3 goes live the moment T3
lands.

### Why item 3 was a defect

The analyzer mirrors the toggle resolution in `starpilot/common/starpilot_variables.py`. The mirror had an extra
`has_auto_tune and` term that the source
([starpilot_variables.py:724](../starpilot/common/starpilot_variables.py)) does not have — its condition is
`advanced_lateral_tuning and is_torque_car and not is_angle_car`. Since `has_auto_tune` is `live_snapshot.useParams`
and GM is absent from `torqued`'s `ALLOWED_CARS`, the analyzer's copy was pinned `False` forever. Lines 390-395 both
end in `or force_auto_tune_off`, so the defect propagated to both resolved flags.

**Control-path impact today is nil.** `SteerLatAccel` and `SteerFriction` are level-3 gated, so both custom values
fall back to static and `effective=` prints 2.0 / 0.13 either way. The flags were wrong; the numbers were not. That
changes when T3 drops `SteerLatAccel` to level 2 — which is exactly why this had to land in T1.

---

## Review outcome

Diff read in full and hand-traced. **All six items are implemented correctly.**

| check | result |
|---|---|
| Line 377 now `alt and _param_bool(...)`, `has_auto_tune` gone | ✅ |
| Line 376 `force_auto_tune` still has `not has_auto_tune` — the symmetrical-looking trap avoided | ✅ |
| Lines 390-395 `use_custom_*` untouched | ✅ |
| `get_bolt_2022_2023_center_output_scale` called `(la, v)` — opposite order to the `friction_scale` line above it | ✅ |
| 5-tuple return matched by 5-element nan branch; sole callers at 1031/1034 both updated | ✅ |
| `steerRatio=` / `steerActuatorDelay=` names and `.4f` formats byte-identical, parenthetical appended only | ✅ |
| `_param_str` matches spec, no tuning-level argument | ✅ |
| `PARAM_TUNING_LEVELS` untouched; no other file modified | ✅ |
| Line length 103 vs the 160 limit at [pyproject.toml:247](../pyproject.toml) | ✅ no lint risk |

**Hand-traced behavior holds.** Post-fix route (`ForceAutoTuneOff=1`, `AdvancedLateralTune=1`, `tuningLevel=2`):
`alt` → True, `force_auto_tune_off` → True, so both flags flip **0 → 1**; `SteerLatAccel` / `SteerFriction` stay
level-3 gated so `_param_float` returns the static default and `effective=` stays `2.0000 / 0.0000 / 0.1300`. Pre-fix
route (`ForceAutoTuneOff=0`): both flags stay 0.

### Finding A — `_param_float` refactored beyond its brief

Constraint 9 said not to refactor anything unlisted. `_param_float` (line 333) was changed anyway: signature
`tuning_level: int = 3` → `int | None = None`, and gate `PARAM_TUNING_LEVELS.get(key, 0)` →
`.get(key)` guarded by two `is not None` checks.

**Behaviorally identical, including the argument-omitting path.** Both callers (`SteerFriction` line 380,
`SteerLatAccel` line 382) pass `tuning_level` explicitly and both keys are in the table. And no key in
`PARAM_TUNING_LEVELS` exceeds level 3, so the old default of `3` never gated anything either — old and new agree even
when the argument is omitted. The divergence would only surface if a level-4 param were ever added.

Revert anyway, for two reasons that stand on their own:

1. It leaves `_param_float` **inconsistent with `_param_bool`** (line 327, still `int = 3` / `.get(key, 0)`) — two
   helpers doing the same job in two different shapes.
2. T2's benchmark rests on this diff being auditable. Every line in it should be a line that was asked for.

### Finding B — the FLM tripwire reports "clear" when it is blind

**This is a real defect, not a style point.** Lines 905-914 read from `log_params`, which is `{}` when the log carries
no `initData`. In that case the tripwire prints:

```
flm=trialApplied=0 activeProfile=(none) overrides={}
```

with no warning — **identical output to a confirmed-clean route.** A tripwire that asserts "no FLM trial" when it
actually knows nothing is the exact false-negative class item 6 exists to prevent. `build=(no initData in log)` prints
four lines above, so the truth is recoverable by eye, but the `flm=` line actively contradicts it.

Fix: gate the block on `build_info is None`.

---

## Follow-up subagent prompt — two edits

Model: Gemini Flash. Scope: one file, two edits. Nothing else.

> ### Task
>
> Two edits to `tools/tuning/analyze_bolt_lateral.py`. One reverts an unrequested refactor; one fixes a defect in a
> newly added block. Everything else in the working tree is reviewed and correct — do not touch any of it.
>
> ---
>
> #### Edit 1 — revert the `_param_float` refactor (lines 333-339)
>
> **Current:**
>
> ```python
> def _param_float(
>   log_params: dict[str, bytes], key: str, default: float, tuning_level: int | None = None
> ) -> float:
>   if tuning_level is not None:
>     min_level = PARAM_TUNING_LEVELS.get(key)
>     if min_level is not None and tuning_level < min_level:
>       return default
>   val = log_params.get(key)
> ```
>
> **Replace with:**
>
> ```python
> def _param_float(log_params: dict[str, bytes], key: str, default: float, tuning_level: int = 3) -> float:
>   if tuning_level < PARAM_TUNING_LEVELS.get(key, 0):
>     return default
>   val = log_params.get(key)
> ```
>
> The function goes back to a one-line signature and a one-line gate, matching `_param_bool` directly above it. The
> `val = log_params.get(key)` line and everything after it (`if val is None`, the `try/except (ValueError,
> TypeError)`) are **unchanged** — they appear above only to anchor the replacement.
>
> Behavior is unchanged at both call sites. This is a consistency revert, not a bug fix. Do not "improve" either
> helper while you are in there.
>
> ---
>
> #### Edit 2 — fix the `flm=` no-initData case (lines 905-914)
>
> **Current:**
>
> ```python
>   flm_profile = _param_str(log_params, "FLMActiveProfileId")
>   flm_applied = log_params.get("FLMTrialApplied", b"0") == b"1"
>   flm_overrides = _param_str(log_params, "FLMActiveOverrides", "{}")
>   print(
>     f"flm=trialApplied={int(flm_applied)} "
>     f"activeProfile={flm_profile or '(none)'} "
>     f"overrides={flm_overrides}"
>   )
>   if flm_applied or flm_overrides not in ("", "{}"):
>     print("  WARNING: an FLM trial was active on this route — tune values below may not be the static tune.")
> ```
>
> **Replace with:**
>
> ```python
>   if build_info is None:
>     print("flm=(no initData in log)")
>   else:
>     flm_profile = _param_str(log_params, "FLMActiveProfileId")
>     flm_applied = log_params.get("FLMTrialApplied", b"0") == b"1"
>     flm_overrides = _param_str(log_params, "FLMActiveOverrides", "{}")
>     print(
>       f"flm=trialApplied={int(flm_applied)} "
>       f"activeProfile={flm_profile or '(none)'} "
>       f"overrides={flm_overrides}"
>     )
>     if flm_applied or flm_overrides not in ("", "{}"):
>       print("  WARNING: an FLM trial was active on this route — tune values below may not be the static tune.")
> ```
>
> The five statements are **unchanged in content** — they move one indent level to the right and gain an
> `if build_info is None:` / `else:` wrapper. Do not reword the warning string, change the field names, or alter the
> warning's condition.
>
> `build_info` is the same variable already used at line 890 for the `build=` line; it is `None` exactly when the log
> had no `initData` message, which is also when `log_params` is empty. Reuse it — do not test `log_params` emptiness
> instead.
>
> On a normal route this changes **no output at all**. It only affects logs missing `initData`.
>
> ---
>
> ### Constraints
>
> 1. **Edit only `tools/tuning/analyze_bolt_lateral.py`**, only `_param_float` and the `flm=` block.
> 2. **Do not touch `_param_bool`** (line 327). It is already in the correct shape; Edit 1 makes `_param_float` match
>    it, not the other way round.
> 3. **Do not touch `_param_str`** (line 349). It is new, correct, and intentionally has no tuning-level argument.
> 4. **Do not touch `PARAM_TUNING_LEVELS`.** The `ForceAutoTune` 3 → 2 and `SteerLatAccel` 3 → 2 entries belong to
>    T2.5 and T3 respectively.
> 5. **Do not touch the two `_param_float` call sites** (lines 380, 382). They pass `tuning_level` positionally and
>    stay valid under both signatures.
> 6. **Do not touch the mirror fix at line 377**, the `route=` / `build=` header lines, the `steerActuatorDelay=`
>    parenthetical, or any `center_output_scale` plumbing. All reviewed and correct.
> 7. **Do not touch the C3 work** (`reconstruct_unwind`, signed `d_des_rate`), `np.random.seed(0)`, the phase masks,
>    `detect_turn_in_events`, `summarize_turn_in_events`, or `summarize_bolt_gain_bands` (known field-width bug,
>    deliberately out of scope — leave it broken).
> 8. **Do not run the analyzer, do not run tests, do not run git commands, do not commit or stage anything.** The
>    working tree has uncommitted work that must not be disturbed.
> 9. Do not refactor, rename, or tidy anything. Edit 1 exists to undo exactly that.
>
> ### Report back
>
> - The full diff.
> - Confirmation that `_param_bool` and `_param_str` are byte-identical to before your edit.
> - Confirmation that the five moved statements in Edit 2 differ only in indentation.
> - Confirmation that no file other than `tools/tuning/analyze_bolt_lateral.py` changed.
> - If either "current" block above does not match the file verbatim, **stop and report** rather than guessing.

---

## Verification (user runs — needs routes, not yet done)

Nothing below has been executed. Static review is complete; all of this is outstanding.

1. Diff confined to `tools/tuning/analyze_bolt_lateral.py`.
2. Import clean:
   ```bash
   python -c "import openpilot.tools.tuning.analyze_bolt_lateral"
   ```
3. **Headline check — the post-fix route** (`ForceAutoTuneOff=1`):
   ```bash
   python tools/tuning/analyze_bolt_lateral.py <post-fix-route>
   ```
   - `resolved=` must now read `useCustomLatAccel=1 useCustomFriction=1`, where
     [post_controller_fix_report.txt:8](post_controller_fix_report.txt) recorded `0 0`.
   - `effective=` must still read `latAccelFactor=2.0000 latAccelOffset=0.0000 friction=0.1300`. If `effective=`
     moved, the fix went too far.
4. **Control — the pre-fix route** (`ForceAutoTuneOff=0`): `resolved=` unchanged at `0 0`. A flip here means the
   edit landed on the wrong line. Steps 3 and 4 together are what actually prove item 3.
5. New header lines on both routes: `route=` matching the argument, and
   `flm=trialApplied=0 activeProfile=flm-1784682467:baseline_fix:recommended overrides={}` with **no** warning —
   `flm-1786229999.json` confirms no trial was applied, and the stale profile ID alone must not trip the warning.
   Both routes have `initData`, so neither should print `flm=(no initData in log)`.
6. `steerActuatorDelay=` line carries the parenthetical, field names and formats unchanged.
7. **Regression:** every other pre-existing section — `staticTune=`, tracking summary, unwind old/new columns, torque
   residuals, gain bands, event table — byte-identical to the archived output apart from the additions above.
8. **Determinism:** two consecutive runs on the same route byte-identical.

## Next steps

1. **Run the follow-up subagent** above — two edits, one file.
2. **Run verification.** Steps 3 and 4 are the ones that matter; the rest is regression cover.
3. **Regenerate the archived baselines.** `pre_controller_fix_report.txt` and `post_controller_fix_report.txt` no
   longer match what the analyzer emits — they predate `route=`, `build=`, `flm=`, the `steerActuatorDelay=`
   parenthetical, and the flag flip. Re-run both routes and overwrite **after** verification passes, so T2 has a valid
   reference. Skipping this makes T2's byte-identical check fail for the wrong reason.
4. **Commit T1.** Stage only `tools/tuning/analyze_bolt_lateral.py` and the `wat_plan/` docs. The working tree also
   carries deletions of `starpilot/system/galaxy/bin/frpc_darwin_amd64` and `frpc_darwin_arm64` (~31 MB, one already
   staged) that predate this work and are unrelated — keep them out of the T1 commit.
5. **T2 is then unblocked** — pick the benchmark route and establish the new baseline.

Do **not** start T2.5 or T3 before step 3 lands. Both depend on comparing reports against a baseline that does not yet
exist in the right format.

## Known divergence, accepted

The analyzer's `force_auto_tune` / `force_auto_tune_off` do not mirror the source's `not is_angle_car` term.
`resolve_effective_tune` returns early unless `lateralTuning.which() == "torque"`, which covers `is_torque_car`, but
an angle car with torque lateral tuning would resolve differently in the analyzer than on the device. Pre-existing,
not introduced by this chunk, and impossible on the Bolt. Left alone.

## Out of scope for this chunk

- **Full FLM awareness** (`update_controller.md` C4 item 1). Item 6 prints three raw values and warns; it does not
  parse `FLMActiveOverrides`, resolve overrides into the effective tune, or read FLM report files. Confirmed
  unnecessary for the routes measured so far — `flm-1786229999.json` records `FLMTrialApplied: false` and
  `FLMActiveOverrides: {}`.
- Speed-band × direction symptom classification and chatter detection. Covered by FLM per the *Division of labor*
  section in `update_tune.md`; deliberately not duplicated in the analyzer.
- The `summarize_bolt_gain_bands` field-width bug.
- The `ForceAutoTune` level-3 → level-2 change and its mirrored `PARAM_TUNING_LEVELS` entry. That is **T2.5**.
- The `SteerLatAccel` level-3 → level-2 change and its mirrored entry. That is **T3**.
- Everything else in T2–T5: the benchmark loop, `LAT_ACCEL_FACTOR`, `latAccelOffset`, `ffScaleNeg`.
- Any change to the controller, to tune values, or to `override.toml`.
