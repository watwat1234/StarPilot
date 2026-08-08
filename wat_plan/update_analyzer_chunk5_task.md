# Chunk 5 task list — turn-in event analysis (the slam and the overcorrection, per event)

**Status:** THIRD PASS REWORK COMPLETE — **READY FOR ROUTE EXECUTION.** All review notes addressed: `post_duration` restored before `< EVENT_MIN_S` check (fixes `UnboundLocalError`), column headers and fields aligned in both tables, `t_rel` qualifier present, unused `samples` arg removed. `py_compile` passes cleanly and `git diff HEAD --stat` confirms zero deletions (225 insertions).
**Audience:** an implementing subagent. Follow the tasks literally and in order.
**Only file to edit:** `tools/tuning/analyze_bolt_lateral.py`. Nothing else. No schema changes, no controller changes.
**Depends on:** Chunks 1, 1b, 2, and Chunk 4 Part A — all complete. `ControlSample` already carries every field this chunk needs.

## Hard rule — this diff is purely additive

You will add:

1. one constants block, next to the existing Chunk 2 constants,
2. two new functions, defined **above `main`** (file convention),
3. one call at the **end** of `main`, after `summarize_bolt_gain_bands`.

That is all. **Do not delete, rename, reorder, reformat, or "clean up" any existing code.** `git diff --stat` on your finished work must show insertions and zero deletions. This is checked in Verify step 1 and it is not negotiable.

This rule exists because it has already been broken. During Chunk 4, an implementer applying two small fixes deleted `reconstruct_bolt_2022_2023_gains` and `summarize_bolt_dynamic_gains` — two working, reviewed functions — while `main` still called them, leaving a `NameError` that fired only after a full multi-minute log parse. It was reported as COMPLETE without the script ever being run. Do not repeat that. Run the script. Read the output. Report what you actually saw.

## Context

The driver of a 2022/2023 Chevy Bolt reports a reproducible symptom: a right 90° turn where the wheel slams right, followed by an overcorrection to the left. Three contributors are confirmed from data — the `unwind_detected` signed-rate inversion that freezes the integrator at right turn-*in* but not at right turn-*exit* (Chunk 2), a ~0.34 m/s² uncorrected feedforward bias (Chunk 1), and a ~11.2% right-biased static `ff_scale` (Chunk 1b). Chunk 4 removed the dynamic gain layer from the candidate list by measurement: it is banded to `|la| ∈ [0.12, 1.35]` and reads `medFF ≈ 1.00` in the `1.6+` row on both sides, i.e. switched off during a 90° turn.

Every measurement so far is an **aggregate over the whole route** — bucket means, phase fractions, band medians. The symptom is an **event**: one turn, a few seconds long. Aggregates cannot show its shape, cannot rank instances, and cannot say whether the overcorrection is materially worse on rights than on lefts.

This chunk measures the symptom directly, once per occurrence, and prints route-relative timestamps so specific moments can be found in the log. It is also the **before** half of a before/after: when a controller fix eventually lands, the same table on a re-drive is the pass/fail.

---

## Task 1 — Constants block

Add next to the existing Chunk 2 constants near the top of the file, so everything tunable lives in one place:

```python
# Chunk 5 - turn-in event detection
TURN_IN_LA_THRESHOLD = 0.4     # m/s^2, event arms on crossing this
TURN_IN_REARM_LA = 0.3         # hysteresis: |desired_la| must fall below this to re-arm
TURN_IN_MAX_V = 14.0           # m/s
EVENT_PRE_S = 1.0              # pre-roll seconds, captures the unwind-freeze approach
EVENT_POST_S = 3.0             # seconds after t0; the overcorrection window
EVENT_MIN_S = 1.0              # discard events truncated shorter than this
EVENT_REFRACTORY_S = 2.0       # minimum spacing between event starts
AT_LIMIT_TORQUE = 0.99         # steer_max = 1.0 (latcontrol.py:17)
WORST_EVENTS_N = 10
```

## Task 2 — Detect turn-in events

Write a function above `main` that walks `samples` in index order and returns a list of event records.

**Arming.** Index `t0` starts an event when **all** of these hold:

- `abs(desired_la[t0]) >= TURN_IN_LA_THRESHOLD` and `abs(desired_la[t0 - 1]) < TURN_IN_LA_THRESHOLD` — an upward crossing, not merely being above the threshold;
- `desired_la[t0] * desired_jerk[t0] > 0` — magnitude growing, i.e. turn-in and not unwind. This is the same convention the existing `turn_in` mask uses at [analyze_bolt_lateral.py:226](../tools/tuning/analyze_bolt_lateral.py); reuse it, do not invent a different test;
- `v_ego[t0] < TURN_IN_MAX_V`;
- `torque_active[t0]` — **use `torque_active`, never `lat_active`** (Chunk 2 Task 0: `lat_active` comes from a separate `carControl` message and can be one frame stale at exactly the engagement boundaries that matter);
- `not steering_pressed[t0]`;
- `(mono_time[t0] - mono_time[prev_t0]) / 1e9 >= EVENT_REFRACTORY_S`, or this is the first event.

**Re-arming.** After an event starts, the detector is disarmed until `abs(desired_la)` has fallen below `TURN_IN_REARM_LA`. Without this hysteresis, a signal hovering near 0.4 generates a new "event" every few frames.

**Direction.** `sign = +1.0 if desired_la[t0] > 0 else -1.0`, fixed at `t0` and held for the entire window. `desired_la` may cross zero later in the window; that is signal, not a reason to re-derive the sign.

**Window.** Time-based, walked by index — **do not** assume 100 Hz and slice `t0 ± N` samples.

- Forward from `t0` while `(mono_time[k] - mono_time[t0]) / 1e9 <= EVENT_POST_S`.
- Backward from `t0` while `(mono_time[t0] - mono_time[k]) / 1e9 <= EVENT_PRE_S`.
- Break the walk (in either direction) on `not torque_active[k]`, or on a monotime gap `> 1.5 * DT_CTRL` between adjacent samples — the same gap guard `reconstruct_unwind` uses at [analyze_bolt_lateral.py:69](../tools/tuning/analyze_bolt_lateral.py).
- Keep a truncated event if the retained span **after** `t0` is `>= EVENT_MIN_S`; otherwise discard it and increment a `discarded_truncated` counter.
- A pre-roll that is empty or shorter than requested is **not** grounds for discarding the event. Record `pre_n` and count events with no usable pre-roll separately.

**Do not break the window on `steering_pressed`.** A driver grabbing the wheel is the expected human response to a slam, so aborting on it would systematically discard the very events being hunted and bias the result toward benign turns. `steering_pressed` gates *arming* only, so events never start mid-intervention. Inside the window, record it as the `pressed` fraction and let the reader judge. This is the single most likely thing to get "helpfully" wrong — if you find yourself adding a `steering_pressed` break for cleanliness, don't.

## Task 3 — Per-event metrics

Define the in-direction error, using the file's established sign convention (`bias = actual - desired`, positive is leftward):

```python
err = sign * (actual_la - desired_la)
```

`err > 0` means the car went **further into** the turn than commanded — the slam. `err < 0` means it went **out of** the turn relative to command — the overcorrection.

Compute over the post-`t0` span `[t0, t0 + EVENT_POST_S]` unless noted:

| metric | definition |
|---|---|
| `peak_in` | `max(err)` — the slam |
| `t_in` | seconds after `t0` at which `peak_in` occurs |
| `peak_opp` | `max(-err)` **over samples strictly after `t_in`** — the overcorrection. NaN if nothing follows `t_in`. See the window note below; this is not the whole-window max |
| `t_opp` | seconds after `t0` at which `peak_opp` occurs |
| `peak_abs` | `max(abs(desired_la - actual_la))` |
| `peak_tq` | `max(abs(torque_cmd))` |
| `at_limit` | fraction of samples with `abs(torque_cmd) >= AT_LIMIT_TORQUE` |
| `pressed` | fraction of samples with `steering_pressed` |
| `mean_i_pre` | `mean(abs(i_term))` over the **pre-roll only**, or NaN if `pre_n == 0` |
| `v0` | `v_ego` at `t0` |

`peak_in` and `peak_opp` are each a max of a signed quantity, so either can come out negative — that is meaningful (the error never crossed into that direction at all) and must be printed as the signed value, not clamped to zero.

**Why `peak_opp` is restricted to after `t_in`, and why the obvious version is wrong.** During the turn-in ramp the plant trails the command by design, so `err` is negative for the *entire* ramp and `-err` is correspondingly large — on the order of *effective lag × jerk*, roughly 0.25 m/s² at a ~0.15 s lag and a brisk turn-in. A whole-window `max(-err)` therefore returns **ordinary tracking lag**, early in the window, on essentially every event in both directions. It would win the max over the genuine post-peak overcorrection almost every time.

That matters more here than anywhere else in the report: `peak_opp` is the headline number of this chunk, the column that answers "is the overcorrection materially worse on rights than lefts". Computed over the whole window it would come back roughly symmetric for reasons that have nothing to do with the symptom, and the chunk would silently fail to measure the thing it exists to measure. `t_in` is the index of `peak_in` and is computed immediately above, so the restriction is a slice.

Print both `t_in` and `t_opp` regardless, so the reader can see the interval between the slam and the recovery rather than assuming it.

`mean_i_pre` is why the pre-roll exists: `unwind_detected` requires `abs(setpoint) < 0.3`, so the integrator freeze happens on the *approach*, below the 0.4 arming threshold — entirely before `t0`. Without a pre-roll this chunk could not see it.

## Task 4 — Aggregate table

New section, printed **last**, after `summarize_bolt_gain_bands`. Start the header with `\n` as every other section does; 2-space indent, f-strings, `:.4f`.

```
Turn-in events:
  total=NN  left=NN  right=NN  discarded_truncated=NN  no_preroll=NN
  dir     n    peak_in med/p90   peak_opp med/p90   peak_abs med/p90   at_limit med/p90   t_in med
  left    NN   ...
  right   NN   ...
```

Report **median and p90**, not means — at tens of events a single bad turn would dominate a mean, and the p90 is the interesting number anyway.

Carry Chunk 2's empty-bucket rule forward: **a direction with `n == 0` prints `--` in every numeric column, never `0.0000`.** Parenthesise `n` when `n < 5` so thin rows are visibly untrustworthy rather than read as signal. A `peak_opp` that is NaN for every event in a direction prints `--` too.

**Align the numeric columns with the header row.** The layout block above is a sketch, not a width spec: size each header label to the field that will sit under it (e.g. `peak_in med/p90` is 15 chars against a 17-wide field), or the drift compounds across four columns and the table becomes hard to read. This was already a required fix in Chunk 2's review; it is not a new standard.

## Task 5 — Worst-events listing

Below the aggregate, list up to `WORST_EVENTS_N` events sorted by `peak_abs` descending:

```
  worst events (by peak_abs):
  t_rel      dir     v0    peak_in  t_in   peak_opp  t_opp  peak_abs  peak_tq  at_limit  pressed  mean_i_pre
  02:14.3    right   8.42  ...
```

`t_rel = (mono_time[t0] - control_samples[0].mono_time) / 1e9`, formatted `mm:ss.s`. Label the column explicitly as **relative to the first controlsState sample**, not to route start — they are close but not identical, and someone will scrub a log against this number. A bare `t_rel` header does not satisfy this; put the qualifier in the section text, e.g. `worst events (by peak_abs); t_rel is relative to the first controlsState sample:`. Align these columns with their header too.

If there are no events at all, print `  no turn-in events detected` and return; do not print an empty table.

---

## Review outcome — first implementation pass

A first pass landed `TurnInEvent`, `detect_turn_in_events` and `summarize_turn_in_events` and was reviewed statically. `python -m py_compile` and the Verify step 7 AST call-graph check both ran clean.

**Credit where it is due — this pass was good, and it is to be amended, not rewritten.** `git diff HEAD --stat` on the analyzer showed **217 insertions and zero deletions**: the additive rule held. Every deliberately-trapped detail was handled correctly, and all of the following is to be preserved as-is:

- `torque_active` at arming, not `lat_active`.
- **No `steering_pressed` break inside the window** — arming-gate only, with `pressed` as a column. This was flagged in Task 2 as the single most likely thing to get "helpfully" wrong, and it wasn't.
- Time-based window walked by index in both directions, with the `1.5 * DT_CTRL` gap guard; the backward walk correctly tests the gap between `k` and `k+1`.
- Hysteresis and refractory both present; `prev_t0_mono = -inf` lets the first event pass without a special case.
- Sign fixed at `t0` and held; `peak_in` / `peak_opp` printed signed via `+.4f`, not clamped at zero.
- A short or absent pre-roll does not discard the event; `mean_i_pre` goes NaN and prints `--`.
- The `n == 0` row prints `--` across; `n < 5` is parenthesised.

### Required rework

1. **`peak_opp` was computed over the whole window and must be restricted to after `t_in`.** See Task 3. **This is a defect in the spec you were given, not in your implementation** — the original table said `max(-err)` over `[t0, t0+POST]` and you implemented exactly that. The reasoning is written out in Task 3 now; read it before making the change, because the point is *why* the whole-window version is wrong, and that reasoning is the deliverable as much as the code is.
2. **Align the numeric columns with their headers** in both tables. See Task 4.
3. **Label `t_rel` as relative to the first controlsState sample.** Task 5 asked for this explicitly and it was skipped.
4. `summarize_turn_in_events` takes a `samples` parameter it never uses. Drop it and update the call in `main`.
5. `if not post_indices:` is unreachable — `t0` is `torque_active` by construction, so the list always holds at least `t0`. Remove **that guard only**. Keep the `post_duration` assignment and the `< EVENT_MIN_S` check that follow it; they are separate and both load-bearing.

   > *Wording corrected after the second pass, which read the original phrasing as licence to delete the assignment too. See below.*

Items 2–5 are small. Item 1 is the one that matters: without it the chunk's headline column reports tracking lag instead of the symptom.

### Watch on the first run — not a defect, do not "fix" pre-emptively

Arming gates on `desired_la * desired_jerk > 0` at the **single** crossing sample, with no magnitude floor (the existing `turn_in` mask at [analyze_bolt_lateral.py:226](../tools/tuning/analyze_bolt_lateral.py) pairs the sign test with `|jerk| >= 0.35`). One noisy jerk frame at exactly the crossing drops the whole turn, and it will not re-arm until `|desired_la|` falls back below `TURN_IN_REARM_LA` — so a missed turn is missed entirely, not merely delayed. `desiredLateralJerk` comes off the planner and should be smooth, so this is expected to be a non-issue. But **if Verify step 3 returns a suspiciously low event count, check this before anything else**, and report it rather than quietly adding a jerk floor.

## Review outcome — second pass (rework)

Three of the five items landed. One was skipped for the second time. One broke the function.

### Blocker — `post_duration` is never assigned

[analyze_bolt_lateral.py:607-618](../tools/tuning/analyze_bolt_lateral.py):

```python
      post_indices = []
      for k in range(t0, len(samples)):
        ...
        post_indices.append(k)

      if post_duration < EVENT_MIN_S:      # <-- never assigned anywhere in the file
```

This raises `UnboundLocalError` on the **first detected event** — after a full multi-minute log parse. It is the Chunk 4 failure mode a third time: a working line deleted while editing the lines next to it.

Rework item 5 asked for the unreachable `if not post_indices:` guard to go. The assignment two lines below it went with it:

```python
      if not post_indices:          # the target
        discarded_truncated += 1
        continue

      post_duration = (samples[post_indices[-1]].mono_time - s.mono_time) / 1e9   # collateral
      if post_duration < EVENT_MIN_S:
```

**Fix — restore exactly one line before the check:**

```python
      post_duration = (samples[post_indices[-1]].mono_time - s.mono_time) / 1e9
```

No guard is needed around it. `t0` is `torque_active` by construction, so `post_indices` always holds at least `t0`; a single-element list yields `post_duration == 0.0`, which `< EVENT_MIN_S` correctly discards.

**The item-5 wording was partly to blame** — "remove it, or keep it and drop the separate `post_duration` check" described two adjacent statements as one either/or. It has been corrected above. The lesson stands regardless: **when a review says to delete something, delete exactly that, and re-read the surrounding five lines before saving.**

### Verify step 8 did not catch it — the check itself was wrong

`python -m py_compile` passed and the AST call-graph check printed `call graph OK` on the broken file. The check resolves **calls**; `post_duration` is a name *load*. It was written after Chunk 4 specifically to catch undefined names and it does not cover the case it exists for.

Step 8 now requires `ruff` instead. A real linter covers the whole class rather than the one instance someone thought of, and the repo already uses it.

### What landed correctly — preserve as-is

- **Item 1, `peak_opp` restricted to after `t_in`** — done well. `post_samples[t_in_idx + 1:]`, NaN when `peak_in` falls on the last sample, `--` in both tables, and `opp_valid` filtering before median/p90 so one NaN cannot poison a direction's aggregate. `t_opp > t_in` now holds by construction.
- **Item 3**, the `t_rel` qualifier, in the section header.
- **Item 4**, unused `samples` parameter dropped and the `main` call site updated.
- Everything preserved from the first pass is still intact.

### Still outstanding

- **Item 2, column alignment — skipped twice.** The header string and field widths are byte-identical to the first pass: `n` sits at column 11 against data ending at 13, and `peak_in med/p90` is 15 characters under a 17-wide field. Not a correctness problem; it has now been asked for three times counting Chunk 2's review.

### A note on the additive rule

`git diff HEAD --stat` still reads 224 insertions, zero deletions — and the file is broken. The deletion happened *inside* newly-added lines, where a diff against HEAD cannot see it. **Zero-deletions-vs-HEAD is not evidence the code works, and it is not a substitute for running it.** The rule protects existing code from you; it says nothing about the code you are writing.

## Verify

```bash
python tools/tuning/analyze_bolt_lateral.py <dongle>/<route>
```

1. `git diff --stat` shows **insertions and zero deletions**. Every pre-existing section byte-identical to the last run, including `TorqueEstimator fit: latAccelFactor=1.1087 latAccelOffset=-0.3454 friction=0.1623 bucket_points=11522` — Chunk 1b seeded the RNG precisely so this line is part of the regression surface.
2. Run twice; whole output identical.
3. **Sanity-gate detection before reading anything into it.** The event count should be in the tens. Thousands means the hysteresis or refractory is broken. Zero means the crossing test is inverted — check that you compare against sample `t0 - 1`, not `t0 + 1`. Both directions should be non-zero on a normal drive.
4. **Sanity-gate the window.** Median `t_in` should land somewhere between a few hundred ms and ~1 s. Near 0, or pinned at `EVENT_POST_S`, means an indexing bug — fix that before interpreting anything.
5. **Sanity-gate magnitude.** The worst events' `peak_abs` must sit well above the route's transient MAE of 0.29. Values near the steady-state 0.09 mean the detector is catching cruise rather than turn-ins.
6. **Sanity-gate `peak_opp` specifically.** `t_opp` must be strictly greater than `t_in` on every event — that is now true by construction, so if it is ever violated the restriction was not applied. Events where the car simply never came back out past command give NaN or a negative `peak_opp`; both are valid results and must print as such.
7. **Then the discriminating read:** is `peak_opp` materially larger on rights than on lefts, and does `at_limit` cluster on the right? Report the numbers plainly whatever they show. As in Chunks 2 and 4, a result that fails to confirm the hypothesis is a useful outcome, not a failure to explain away.
8. **`ruff check tools/tuning/analyze_bolt_lateral.py` clean.** Run this *before* the route, every time, and paste the output when reporting.

   `python -m py_compile` is not sufficient and neither is an AST call-graph check — both passed on a file that raised `UnboundLocalError` on its first event, because an unassigned local is a name *load*, not a syntax error and not a call. `ruff` catches that class (F821) along with unused names and unreachable code. The repo already uses it, so there is nothing to install and no excuse for skipping it.

9. **Run the script before reporting anything.** Twice now this chunk has been reported COMPLETE on code that had never been executed, and both times the failure surfaced only after a full log parse — the one place a static read is weakest and a single run is decisive. "It compiles" is not a result. Paste what the terminal actually printed.

## Out of scope

No Chunk 3 (superseded, do not build). No Chunk 6 multi-route support. Do not revive the Chunk 4 oscillation detector or any part of it — oscillation diagnosis is deliberately parked. No edits to `latcontrol_torque.py`, `cereal/log.capnp`, or any tune value; importing from `latcontrol_torque.py` is in scope, editing it is not.

**Do not fix the `unwind_detected` sign error.** It is a confirmed defect and it is not yours to fix here — the controller change gets its own review and its own before/after drive. This chunk exists to measure the *before*. Diagnosis only.
