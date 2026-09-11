# Plan: Fix `tools/tuning/inspect_lateral_tuning.py`

**Target file:** [`tools/tuning/inspect_lateral_tuning.py`](../tools/tuning/inspect_lateral_tuning.py) — this is the **only** file you may modify.

**Goal:** the tool currently produces plausible-looking numbers that are, in several places, measuring something other than what the report says. Fix the measurement and the labelling. Do not add features beyond the tasks listed.

---

## Ground rules

1. **Do not modify any file except the target file.** Specifically: nothing under `selfdrive/`, `opendbc_repo/`, `starpilot/`, or `cereal/`. If a fix seems to require changing `torqued.py` or `lagd.py`, it doesn't — re-read the task.
2. **Do not implement anything from `wat_plan/ioniq_torqued_investigation.md`.** No dynamic bucket ladder, no `latAccelFactor = 5.64`, no `friction = 0.055`, no `1.8` lateral-accel gate in the estimator. That plan is unrelated and under review.
3. **Do not delete or modify `tools/tuning/inspect_torque_buckets.py`.** It stays as-is.
4. **Every number the tool prints must trace to a variable computed from the log.** Do not hardcode, estimate, or infer a value and print it as if it were measured. If something cannot be measured, print `N/A` and say why. This has been a repeated problem with analyzers in this repo.
5. Style: 2-space indent, matching the existing file. Python 3.11+, numpy available.
6. Work through the tasks **in order**. Tasks 1–8 are independent enough to land separately; Task 9 depends on Task 5 and should be done last.

**Verification command** (run after each task; it must not crash and must produce a full report):

```bash
python tools/tuning/inspect_lateral_tuning.py "62011dd63be92e96/00000005--96adff6433"
```

---

## Task 1 — Stop silently replaying qlogs

**Problem.** `--mode` defaults to `auto`. `ReadMode.AUTO` means "rlogs, **falling back to qlogs per segment**" ([`tools/lib/logreader.py:140`](../tools/lib/logreader.py:140), [`:166`](../tools/lib/logreader.py:166)), and it announces the fallback only through `cloudlog.warning`. From [`cereal/services.py`](../cereal/services.py), qlog decimation is: `livePose` 20 Hz ÷ 4 = **5 Hz**, `carState`/`carControl`/`carOutput`/`controlsState` 100 Hz ÷ 10 = 10 Hz, `liveCalibration` 4 Hz ÷ 4 = 1 Hz.

The lagd replay hardcodes `dt = 0.05`. On a 5 Hz qlog segment the 60-second window silently becomes 240 seconds of wall time, the 4 Hz evaluation cadence becomes 0.8 Hz, and the cross-correlation runs on a signal sampled 4× too slowly. Nothing errors. Every number in Section 3 comes out wrong and looks fine.

**Do this:**

1. Change the `--mode` default from `"auto"` to `"rlog"`.
2. During the log pass, record every `livePose` `logMonoTime`. After the pass, compute the observed mean rate over the route.
3. If the observed rate is below **15 Hz**, print a prominent multi-line warning at the very top of the report stating the observed rate, that qlog data or heavy frame loss is likely, and that **all lagd results in Section 3 are invalid**. Do not exit — still print the report, but the warning must be impossible to miss.
4. Print the observed `livePose` rate in the report header regardless, next to the existing fingerprint lines.

**Done when:** running with `--mode qlog` on any route prints the warning; running with the default does not.

---

## Task 2 — Make Section 2 measure what its title claims

**Problem.** The section is titled "ALL_CHECKS() GATING AUDIT" but only counts `msg.valid`. `all_checks()` is `all_alive AND all_freq_ok AND all_valid` ([`cereal/messaging/__init__.py:252`](../cereal/messaging/__init__.py:252)). `alive` and `freq_ok` are computed from wall-clock receive timing **inside the consuming process** and are not recoverable from a log.

This makes the evidence asymmetric, and the report doesn't say so: **validity below 100% is real evidence of gating; validity at 100% does not clear it.** A green table currently reads as "hypothesis dead," which is wrong.

Frequency, however, *is* derivable from `logMonoTime`, and that covers the `freq_ok` half.

**Do this:**

1. Track the previous `logMonoTime` per service during the pass. Accumulate inter-arrival deltas.
2. Add two columns to the Section 2 table: **observed mean Hz**, and **gap count** = number of inter-arrival gaps longer than 125 ms (i.e. instantaneous rate below 8 Hz).
3. The 8 Hz figure is the real `freq_ok` floor for `starpilotPlan` as consumed by `lagd`. Derivation, from `FrequencyTracker.__init__` ([`cereal/messaging/__init__.py:109`](../cereal/messaging/__init__.py:109)) — verify it, don't just trust it:
   - `service_freq = 20` (services.py), `update_freq = 20` (lagd polls `livePose` at 20 Hz), `is_poll = False`
   - `freq = 20`, `max_freq = 20`
   - neither `service_freq >= 2*update_freq` nor `update_freq >= 2*service_freq` holds → `min_freq = min(20, 10) = 10`
   - final band = `[10 * 0.8, 20 * 1.2]` = **[8.0, 24.0] Hz**
4. Print the `[8.0, 24.0]` band in the section header so the gap count is interpretable.
5. Add a caveat line, verbatim in substance: *"`valid` is only one of three `all_checks()` conditions. `alive` and `freq_ok` as seen by the consuming process are not recoverable from a log. Validity below 100% is evidence of gating; validity at 100% does not rule it out."*
6. Fix the flag logic at the bottom of the loop:
   - `cnt == 0` must print `NOT PRESENT IN LOG`, not `CRITICAL: Gating Risk`.
   - Apply the risk flag to **any** service in the list with validity below 100% or a non-zero gap count, not just `starpilotPlan` and `radarState`.

**Done when:** the table shows observed Hz and gap counts for all seven services, and a service absent from the log is labelled as absent rather than critical.

---

## Task 3 — Relabel the lagd replay state

**Problem.** `LateralLagEstimator.__init__` calls `self.reset(self.initial_lag, 0)`, so the replay always starts at `validBlocks = 0`. The device instead restores `(lateralDelayEstimate, validBlocks)` from the `LiveDelay` param ([`selfdrive/locationd/lagd.py:400`](../selfdrive/locationd/lagd.py:400)), carrying progress across drives.

So "Replayed Final State: validBlocks=N / 5 needed" and "Replayed calPerc" are **blocks earned on this one route**, not device state — but they're printed a few lines below the device's own calPerc, inviting a comparison that is meaningless.

**Do this:**

1. Relabel to make the semantics explicit, e.g. `Blocks earned on THIS ROUTE: N (replay starts from zero; device carries progress across drives)`.
2. **Delete the `Replayed calPerc` line entirely.** It cannot be compared to the device's calPerc and its only effect is to mislead.
3. Replace it with two lines: blocks earned this route, and samples toward the next block (`block_avg.idx` out of `BLOCK_SIZE`).
4. Where the report mentions successful updates, also express them as blocks: `eval_successful_updates / BLOCK_SIZE`. 7 successful samples is 7% of one block, not a success.

---

## Task 4 — Print the device-vs-replay deltas

**Problem.** This is the comparison the tool exists to make, and it is the one thing it doesn't print. The replay feeds `handle_log` unconditionally and therefore **bypasses `all_checks()` entirely**, so replay counts are an upper bound on what the device actually collected. Both halves of each comparison are already computed; they're just never placed side by side.

**Do this.** Add a new section — call it `REPLAY vs DEVICE FIDELITY` — placed after Section 2 and before Section 3. Print:

| Row | Device | Replay |
|---|---|---|
| Torque bucket points | `first_tq.totalBucketPoints` → `last_tq.totalBucketPoints` (and the delta) | `total_pts` |
| Lag blocks | `first_ld.validBlocks` → `last_ld.validBlocks` (and the delta) | blocks earned this route |

Then print these caveats, because both comparisons have real confounds:

- The replay bypasses `all_checks()`; the device does not. A replay total far above the device delta is consistent with on-device gating.
- Device `totalBucketPoints` is **not** zero at route start — `torqued` restores cached points from the `LiveTorqueParameters` param. Use the delta, not the end value.
- Each bucket saturates at `POINTS_PER_BUCKET = 1500`, so once buckets are full the device delta compresses toward zero for reasons unrelated to gating.

**Do not compute an automatic verdict.** Print the numbers and the caveats; the human interprets. No "gating confirmed" line.

---

## Task 5 — Decouple the SVD windows from `--max-lat-accel`

**Problem.** At the rejection chain around [line 378](../tools/tuning/inspect_lateral_tuning.py:378), any point at or below `lat_acc_cutoff` falls into the `else` branch and is appended to `post_proc_points_standard`. With `--max-lat-accel 1.8`, a point at 1.3 m/s² lands in the list still labelled `"Standard Filtered (latAccel <= 1.0 m/s^2)"`. The directional split reads the same list and inherits the contamination. The default path is correct; the bug only fires with the flag — which is exactly the flag someone would use.

**Do this.** Separate the two concerns that are currently fused:

- **Rejection statistics** keep using `lat_acc_cutoff` (the CLI value, defaulting to `LAT_ACC_THRESHOLD`). Unchanged behaviour.
- **Post-processing collection** uses the fixed constants **1.0 / 1.5 / 2.5**, always, regardless of the CLI flag.

Restructure so that a point passing the four kinematic gates (lat active, no driver override, speed above `MIN_VEL`, steer outside the deadband) is offered to all three window lists based only on its own `abs(lateral_acc)` against those fixed constants. Whether it also counts as "accepted" or "rejected: lat accel high" in the statistics is a separate decision driven by `lat_acc_cutoff`.

Keep the full rejection chain in place — points must never be collected during driver override or lateral-inactive periods.

**Done when:** the three window point counts are byte-identical with and without `--max-lat-accel 1.8`, while the rejection counts differ.

---

## Task 6 — Make `--max-lat-accel`'s scope honest

**Problem.** The flag does not affect the bucket replay at all. `torque_estimator.handle_log` uses the module-level `LAT_ACC_THRESHOLD = 1` inside `torqued.py`, which the tool cannot change without editing that file (forbidden — see ground rule 1). The help text also says *"default: auto from estimator"*, but `torque_estimator.lat_acc_threshold` **does not exist** — that attribute was proposed in the investigation doc and never implemented.

**Do this:**

1. Rewrite the help text to state plainly that the flag affects **post-processing rejection statistics only** and does not change the bucket replay.
2. Print one line in the Section 4 header stating the bucket replay used `LAT_ACC_THRESHOLD` from `torqued.py`, with its actual value.

---

## Task 7 — Offset-correct the directional split

**Problem.** Left/right are split at `±0.05` about zero. The fitted `latAccelOffset` on this car is near `-0.150`, so those two populations are not symmetric about the true neutral point, which biases the very asymmetry measurement they exist to produce.

**Do this.** Split relative to the fitted offset from the standard-window TLS fit, with a `0.20` margin:

- left = points where `lateral_acc - fitted_offset > 0.20`
- right = points where `lateral_acc - fitted_offset < -0.20`

Fall back to an offset of `0.0` if the standard fit returned `None`. Print the offset actually used in the section header so the split is reproducible.

---

## Task 8 — Add the OLS bracket (highest-value addition)

**Problem.** The tool computes only the TLS/SVD fit. TLS minimises perpendicular distance and is unbiased only when x-noise and y-noise are comparable. Here the x-axis (steer torque) carries rack friction and hysteresis while the y-axis (lateral accel) is comparatively clean, so **TLS systematically over-rotates and inflates `latAccelFactor`**. The tool as written cannot distinguish "the factor is 5.64" from "5.64 is TLS bias."

Two ordinary least-squares fits bracket the true slope and cost almost nothing.

**Do this.** For each of the three windows, mean-centre the data and compute three slopes:

- **Lower bound** — OLS of latAccel on steer: `beta_low = cov(x, y) / var(x)`. Attenuated by x-noise.
- **Upper bound** — inverse OLS of steer on latAccel: `beta_high = var(y) / cov(x, y)`. Inflated.
- **TLS** — the existing SVD fit, which lies between them.

Print all three per window, plus the bracket width as a percentage of the TLS value.

Add one interpretation line to the summary, stated as a measurement and not a conclusion: if the bracket width exceeds roughly 30% of the TLS value, the data does not constrain `latAccelFactor` well enough to justify writing it into `CarParams`.

---

## Task 9 — Lag sweep (do this last)

**Problem.** The alignment lag comes from the log's `liveDelay.lateralDelay` ([line 330](../tools/tuning/inspect_lateral_tuning.py:330)), which on this car is the unestimated `0.3` fallback. If the true lag differs, every torque sample is misaligned against yaw rate, which rotates the hysteresis parallelogram and shifts the fitted slope. The factor cannot be trusted until this is bounded.

**Do this.** Add an opt-in `--lag-sweep` flag. When set:

1. During the log pass, retain two flat arrays for steer torque — timestamps and values — covering the whole route, plus a list of per-pose-frame samples (`t`, `vego`, `yaw_rate`, `roll`) for frames that passed the kinematic gates.
2. After the pass, for each candidate lag offset in **−0.15 to +0.15 s in 0.025 s steps** (13 candidates), re-derive steer for every retained pose frame with a single vectorised `np.interp` at `pose_times + lag`, rebuild the ≤1.0 window, and refit.
3. Print a compact table: lag offset, n, TLS factor, offset, friction.
4. Print which lag maximises fit quality, using the OLS bracket width from Task 8 as the quality metric — narrower bracket means better alignment.

Keep this entirely behind the flag. It must not run or slow anything down by default, and it must not alter any existing output.

---

## Task 10 — Fix the summary section

**Problems.** Three of them:

1. The lagd diagnostic lines sit inside `if eval_successful_updates == 0`, so the interesting case — partial progress, mostly rejected — prints only a celebratory line and no breakdown.
2. `"earned N successful sample updates!"` oversells; N is samples, not blocks.
3. `"-> TORQUE (torqued): Bucket deadlock confirmed"` asserts a conclusion from a replay that is strictly more permissive than the device.

**Do this:**

1. Always print the dominant rejection cause for lagd — compute it as the largest of the `eval_rejected_*` counters — regardless of how many updates succeeded.
2. Report blocks (`updates / BLOCK_SIZE`) alongside raw sample counts. Drop the exclamation mark.
3. Replace the deadlock line with measurement language, e.g. `replay shows empty buckets: [0] — note the replay bypasses on-device all_checks(); compare against the device delta in the fidelity section above`.

---

## Task 11 — Cleanup

1. Remove unused imports: `MOVING_WINDOW_SEC`, `MIN_OKAY_WINDOW_SEC`, `MIN_RECOVERY_BUFFER_SEC`, `MIN_VEGO`, `MAX_LAT_ACCEL`, `MAX_LAT_ACCEL_DIFF`, `MAX_LAG_STD`, `BLOCK_NUM`, `MIN_BUCKET_POINTS`, `MIN_POINTS_TOTAL`, `MIN_POINTS_TOTAL_QLOG`, `FIT_POINTS_TOTAL`. Verify each is genuinely unused before removing it — some are used only inside f-strings.
2. Remove the dead `rejected_turning_low` counter and its `elif` branch. `MIN_ABS_YAW_RATE = 0.0`, so `turning` is always `True` and the branch can never fire. Leaving it implies a gate that does not exist.
3. Derive `dt` from `SERVICE_LIST['livePose'].frequency` instead of hardcoding `0.05`, matching how `lagd.py` constructs its estimator.
4. Add a comment above `InstrumentedLateralLagEstimator.update_points` noting that the body is duplicated from `LateralLagEstimator.update_points` and must be kept in sync if `lagd.py` changes.

---

## Acceptance checklist

Run the verification command and confirm:

- [ ] Report completes without exception; every section renders
- [ ] Header shows observed `livePose` Hz; `--mode qlog` triggers the invalidity warning
- [ ] Section 2 shows observed Hz and gap counts for all seven services, with the `[8.0, 24.0]` band and the asymmetric-evidence caveat printed
- [ ] No `Replayed calPerc` line anywhere
- [ ] Fidelity section shows device deltas next to replay totals, with caveats and **no verdict**
- [ ] Three window point counts identical with and without `--max-lat-accel 1.8`; rejection counts differ
- [ ] Each window prints OLS-low / TLS / OLS-high and bracket width
- [ ] `--lag-sweep` prints a 13-row table; omitting it changes no other output
- [ ] Summary prints the dominant rejection cause even when some updates succeeded
- [ ] No file outside `tools/tuning/inspect_lateral_tuning.py` is modified — confirm with `git status`

## What "done" does not mean

Do not tune anything. Do not recommend parameter values. Do not write conclusions into the report that the measurements don't support. The tool's job is to produce numbers a human can check; it has no opinions.

---
---

# ROUND 2 — post-review fixes

**Round 1 (Tasks 1–11 above) is COMPLETE. Do not redo any of it.** Tasks 1, 3, 4, 5, 6, 7, 10 and 11 were implemented correctly and must not be touched. This round fixes defects found reviewing that work.

The ground rules at the top of this document still apply in full. In particular: **only `tools/tuning/inspect_lateral_tuning.py` may be modified**, and every printed number must trace to a variable computed from the log.

R1 and R2 are blockers — they produce confidently wrong output. R3–R5 affect how much the sweep result can be trusted. R6 is cosmetic.

---

## R1 — The lag sweep is anti-causal (BLOCKER)

**Problem.** The sweep samples steer torque from *after* each pose frame instead of before it. The two code paths use opposite conventions and were never reconciled.

- **Main pass** ([line 373](../tools/tuning/inspect_lateral_tuning.py:373)) stores steer timestamps **already shifted**: `raw_points["carOutput_t"].append(t + lag)`, then samples at the raw pose time. Result: the steer used satisfies `t_out + lag ≈ t_pose`, i.e. `t_out ≈ t_pose − lag` — torque from `lag` seconds *before* the pose. This is correct causality and matches `torqued.handle_log`.
- **Sweep** ([line 376](../tools/tuning/inspect_lateral_tuning.py:376)) stores the **unshifted** `t` in `all_steer_times`, then at [line 766](../tools/tuning/inspect_lateral_tuning.py:766) does `np.interp(pose_t_arr + eff_lag, ...)` — torque from `lag` seconds *after* the pose.

At `off = 0` the two paths differ by `2 × lag` (0.6 s on this car). Every sweep row fits future torque against past yaw; the reported optimal lag is meaningless.

**Do this:**

1. Change the sign at [line 766](../tools/tuning/inspect_lateral_tuning.py:766) to subtract: sample at `pose_t_arr - eff_lag`.
2. Leave `all_steer_times` storing unshifted `t` — that is correct and is what makes the subtraction work.
3. Do not change the main pass. It is already right.

**Done when:** see the R3 acceptance check below — the two fixes are verified together.

---

## R2 — Section 2's gap threshold is not per-service (BLOCKER)

**Problem.** [Line 334](../tools/tuning/inspect_lateral_tuning.py:334) applies a flat 125 ms gap threshold to every service. `liveCalibration` runs at **4 Hz** ([`cereal/services.py`](../cereal/services.py)) — a 250 ms nominal interval — so *every* interval exceeds the threshold, `gaps = cnt - 1`, and it reports `[CRITICAL: Gating Risk]` on every route ever recorded. A guaranteed false positive in the one section built to test the gating hypothesis.

The deeper issue is that 125 ms came from the 8 Hz `freq_ok` floor I derived for `starpilotPlan` as a **20 Hz** service, and that figure does not transfer to other rates:

| Service group | Rate | Why the metric does or doesn't apply |
|---|---|---|
| `livePose`, `starpilotPlan`, `radarState` | 20 Hz | **Applies.** Logged rate ≈ rate the consumer receives. Band `[8, 24]` Hz → 125 ms threshold is correct. |
| `carState`, `carControl`, `controlsState` | 100 Hz | **Does not apply.** The consumer polls at 20 Hz with `conflate=True`, so it only ever receives ~20 Hz of a 100 Hz stream while the log holds all 100 Hz. Log inter-arrival says nothing about the consumer's `freq_ok`. (Their band is `[16, 24]`, not `[8, 24]`, for the same reason.) |
| `liveCalibration` | 4 Hz | **Does not apply.** Nominal interval is already double the threshold. |

**Do this:**

1. Compute the gap threshold per service from `SERVICE_LIST[s].frequency` rather than hardcoding 125 ms.
2. Only report a gap count for **20 Hz** services. For the others print `N/A (conflated)` for the 100 Hz group and `N/A (below poll rate)` for `liveCalibration`.
3. Adjust the status flag so a service with an `N/A` gap column is judged on validity alone and cannot trip the gap condition.
4. Amend the section header line so the `[8.0, 24.0] Hz` band is stated as applying to 20 Hz services only.

**Done when:** `liveCalibration` no longer reports `CRITICAL` on a clean route, and the gap column shows `N/A` with a reason for the four services where it is not interpretable.

---

## R3 — The sweep can only shrink its own sample set

**Problem.** `retained_pose_frames.append` sits at [line 435](../tools/tuning/inspect_lateral_tuning.py:435), inside the `else` that follows `elif abs(steer) <= STEER_MIN_THRESHOLD`. So frames rejected by the deadband **at the original lag** are never retained and cannot come back at a better candidate lag — while the sweep re-applies the deadband per candidate at [line 767](../tools/tuning/inspect_lateral_tuning.py:767). The population is pre-filtered by the very alignment the sweep exists to test, so it can only ever remove points, never discover them.

**Do this:**

1. Restructure the rejection chain so `retained_pose_frames.append((t, vego, yaw_rate, roll))` happens once a frame passes the **first three** kinematic gates — lateral active, no driver override, speed above `MIN_VEL` — *before* the steer deadband is evaluated.
2. Keep the deadband branch exactly as it is for `torque_stats["rejected_steer_center"]`, and keep the fixed-window SVD collection where it is, inside the post-deadband path. **Round 1's Task 5 behaviour must not change.**
3. The sweep already applies the deadband per candidate, so no change is needed there.

This affects **only** `--lag-sweep`. `post_proc_points_standard/relaxed/all` and every number in Sections 5 and 6 must be byte-identical before and after this change.

**Acceptance check for R1 + R3 together — implement this as a printed self-check:**

After both fixes, the sweep row at `off = +0.000` reconstructs exactly the same point set and alignment as the Section 6 "Standard Filtered" fit. Print the Section 6 standard-window TLS factor next to the `off = 0` row and flag a mismatch.

They should agree to within a few parts in 10⁴. They may not be bit-identical: the main pass interpolates against a rolling `deque` of `hist_len = 100` `carOutput` samples (~1 s of history at 100 Hz) while the sweep interpolates against full-route arrays, so edge behaviour can differ for a handful of frames. A disagreement in the third decimal place or worse means the alignment is still wrong — investigate rather than adjusting the tolerance.

Keep this self-check permanently. It is the assertion that would have caught R1 immediately.

---

## R4 — Best-lag is selected by a ratio with a moving denominator

**Problem.** [Line 790](../tools/tuning/inspect_lateral_tuning.py:790) picks the winner with `min(sweep_results, key=lambda r: r["bracket_pct"])`. `bracket_pct` is `bracket_width / laf_tls`, and `laf_tls` varies across the sweep — so a candidate can win by inflating the factor rather than by improving alignment.

**Do this:**

1. Select the optimal lag by **absolute** `bracket_width`, not `bracket_pct`.
2. Print both columns in the sweep table so the tradeoff stays visible.
3. State in the "optimal alignment lag" line which criterion was used.

---

## R5 — TLS is never checked against its own bracket

**Problem.** `fit_torque_params` runs SVD on `[x, 1, y]`, treating the constant column as another noisy dimension, while the OLS bounds in `compute_slopes_and_bracket` handle the intercept by mean-centering. These are not the same estimator, so **TLS is not guaranteed to land inside `[beta_low, beta_high]`**. If it falls outside, the intercept column is distorting the fit — which is real, useful information about fit conditioning that the tool currently discards.

**Do this:** in `compute_slopes_and_bracket`, add a boolean for whether `laf_tls` lies within `[beta_low, beta_high]`. When it does not, print a warning on the bracket line for that window stating that TLS fell outside the OLS bracket and the intercept term is likely distorting the fit.

---

## R6 — Minor

1. `eff_lag` is built from `lag`, which is the **last** `liveDelay.lateralDelay` seen anywhere in the log, applied retroactively to the whole route. Acceptable, but state it in the Section 7 header. Also handle the no-`liveDelay` case explicitly: if no such message was seen, say the sweep is centred on 0.0 rather than letting it silently happen.
2. `pose_mono_times` only accumulates after `carParams` is seen, so the header figure is "livePose rate after carParams," not the route rate. Relabel it accordingly — do not change the computation.
3. [Line 772](../tools/tuning/inspect_lateral_tuning.py:772) round-trips numpy → `.tolist()` → numpy, 13 times over potentially 20k points. Let `compute_slopes_and_bracket` and `fit_torque_params` accept an ndarray directly and skip the conversion.
4. Section 2 column alignment drifts — `{pct:>6.1f}%` plus manual spaces does not line up under the `{'Validity %':<12}` header. Fix the format specifiers so the table is square.

---

## Round 2 acceptance checklist

- [ ] Report completes without exception; every section renders
- [ ] Sweep samples steer at `pose_t − eff_lag`
- [ ] `off = +0.000` sweep row matches the Section 6 standard-window TLS factor to within a few parts in 10⁴, and this is printed as a self-check
- [ ] `liveCalibration` does not report `CRITICAL` on a clean route
- [ ] Gap column shows `N/A` with a reason for the 100 Hz services and `liveCalibration`
- [ ] Sections 5 and 6 numbers are unchanged by R3 — verify by diffing output before and after
- [ ] Sweep table shows both absolute bracket width and bracket %, and selects on the absolute
- [ ] A window whose TLS falls outside its OLS bracket is flagged
- [ ] `git status` shows only `tools/tuning/inspect_lateral_tuning.py` modified
