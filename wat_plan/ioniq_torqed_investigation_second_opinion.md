# Second Opinion: Review of `ioniq_torqued_investigation.md`

Reviewed against the source tree on branch `Dom_wat_analyzer_tuning`.

**Status:** Round 7. The plan's §2 has been rewritten from a per-vehicle hardcoded ladder into a
**generic dynamic auto-scaling architecture**, and the stated goal is for it to be generic. That
changes what this review should be about, so §1 is now a **design analysis of the generic
architecture** rather than a defect list. §2 carries the open items against the current draft;
§3 what has closed; §4 the measurement reference; §5 verified facts; §6 sequencing.

The normalized-ratio observation underpinning the new design is correct and worth stating plainly:
`[-1.0, -0.6, -0.4, -0.2, 0, 0.2, 0.4, 0.6, 1.0] × 0.50` reproduces openpilot's stock ladder exactly,
term for term. The architecture is built on a real invariant.

---

## 1. Design analysis of the generic architecture

### 1.1 — Fleet reality: the clip binds for half the fleet

`params.toml` carries 82 cars with a usable `LAT_ACCEL_FACTOR`, spanning **0.77 to 3.85**. Running
`tau_envelope = clip(1.6 / factor, 0.20, 0.80)` across them:

| `1.6 / factor` lands | Cars | Condition |
|---|---|---|
| **> 0.80 → pinned at the HIGH clip** | **41 (50%)** | factor < 2.0 |
| 0.50 – 0.80 | 37 (45%) | |
| < 0.50 (narrower than stock) | 4 (5%) | factor > 3.2 |
| < 0.20 → LOW clip | **0** | factor > 8.0 |

Three consequences:

**Half the fleet is not dynamic — it is hardcoded at 0.80.** For 41 cars the formula is inert and
they all receive the same ladder, 60% wider than the 0.50 they run today. The formula only varies
across the 45% in the middle. Lowering the numerator does not fix this: at `1.45` the clip still
binds for ~34 cars (41%).

**That is not a bug, but the clip is doing the wrong job under the wrong name.** For a factor-0.77
car, `1.6 / 0.77 = 2.08` — a ladder that would run to ±2.08 when `steer_max` is 1.0. The real
constraint on those cars is **actuator range, not lateral acceleration**, and 0.80 is standing in for
"don't push the outer bin past 80% of what the actuator can command." Say that:

```python
MAX_ENV_FRACTION = 0.8            # of steer_max — outer bin must stay commandable
MIN_ENV          = 0.20           # below this the lever arm for the slope fit collapses
tau_envelope = clip(ENVELOPE_ACCEL / base_factor, MIN_ENV, MAX_ENV_FRACTION * steer_max)
```

Then it is clear that for half the fleet the ladder is actuator-limited by design, and for the rest
it is accel-limited. Both are correct; today only one of them is explained.

**The low clip is dead code.** Nothing in the fleet reaches factor 8.0. It exists only for the
Ioniq 6 post-rebaseline, which clears it anyway (`1.6/5.64 = 0.284`).

> **Sanity note, not a blocker.** `5.64` would be the highest factor in the fleet by 46% — the
> current maximum is 3.85. Either this rack is genuinely exceptional, or the 82 fleet values were
> derived by a method that reads systematically lower than this SVD pipeline does. The measurement is
> well-supported (§4), so proceed — but if a second high-assist EV also lands near 5–6 under the same
> tooling, that is a systematic finding worth knowing about.

---

### 1.2 — The bootstrap problem, and why its failure mode is asymmetric

The ladder is sized from `base_factor`, which is the quantity the estimator exists to measure. The
dynamic formula does not remove that dependency — it only makes a good seed produce a good ladder.
The two ways a seed can be wrong are **not** symmetric:

| Seed | Envelope | Outcome |
|---|---|---|
| **Too low** (3.0 vs 5.64) | too **wide** (0.53 vs 0.28) | outer bins unreachable → deadlock → never learns |
| **Too high** (say 8.0 vs 5.64) | too **narrow** (0.20) | bins fill fast, but `add_point` **silently discards** everything outside the bounds → short lever arm → noisy, biased slope, and it never sees the data that would correct it |

Both are self-reinforcing: a wrong seed stays wrong. The first case is precisely the Ioniq 6 bug this
whole document is about. The second is quieter and arguably worse, because it converges confidently
on the wrong answer instead of stalling visibly at 50%.

This is the structural reason post-drive analysis currently feels inevitable for a new platform.

---

### 1.3 — Closing the loop: re-derive the envelope from the estimate

**Re-derive `tau_envelope` from the *estimated* factor once `is_calculable()` is true, not from CP.**

This works specifically because `is_calculable()` needs only 4 non-empty buckets and 50 points. It
fires **even in the deadlock case** — the deadlock is about *outer* bins, and the raw fit is
therefore available exactly when it is needed to escape.

`PointBuckets.load_points()` re-buckets an existing point list into new bounds
([`helpers.py:103-105`](../selfdrive/locationd/helpers.py#L103)), so a re-size does not have to throw
away the drive: points inside the new ladder are re-binned, only out-of-range ones drop.

Guards worth having:
- re-size only when the implied envelope differs from the current one by more than ~25%
- require the raw fit to be stable over N seconds first
- cap re-sizes per drive (2–3) and `cloudlog` each one with old/new envelope
- persist the converged envelope alongside the params so the next boot starts correct

**This would have resolved the Ioniq 6 from the stock 3.0 seed with no post-drive analysis at all.**
It changes post-drive analysis from a prerequisite into an accelerator: still the right tool for
bootstrapping a genuinely novel platform quickly, but no longer the only escape from a bad seed.

---

### 1.4 — Mount tolerance: derive it, don't pick it

An outer bin survives an offset as long as its margin stays positive. From
`τ = (latAccel − offset)/factor + friction·sign(τ)` and outer bound `±E` where
`E = ENVELOPE_ACCEL / factor`:

$$\text{margin}_\pm = \frac{g - A \mp \text{offset}}{\text{factor}} + \text{friction}$$

with `g` the accel gate and `A` the envelope constant. Both sides stay positive while:

$$|\text{offset}| < (g - A) + \text{friction} \cdot \text{factor} = (g - A) + \text{fricAmp}$$

For the Ioniq 6: `(1.8 − 1.6) + 0.055×5.64 = 0.2 + 0.31 =` **0.51 m/s² ≈ 2.9° of roll.**

**Note that lands on top of `MAX_LAT_ACCEL_OFFSET = 0.5`.** Those two constants were chosen
independently and they describe the same physical quantity. They should be derived from one another,
not both guessed — otherwise a later edit to one silently breaks the other.

The design lever is the **gap between the gate and the envelope constant**, currently an implicit
`1.8 − 1.6 = 0.2`. Make it the named input:

```python
LAT_ACC_GATE    = 1.8
MOUNT_TOLERANCE = 0.35                            # ≈2° roll — the design target
ENVELOPE_ACCEL  = LAT_ACC_GATE - MOUNT_TOLERANCE  # 1.45, replaces the magic 1.6
MAX_LAT_ACCEL_OFFSET = MOUNT_TOLERANCE + 0.15     # clamp derived from the same budget
```

Guaranteed tolerance becomes `MOUNT_TOLERANCE + fricAmp`, with friction as free headroom (0.66 total
on this car). Cost is a ~9% narrower ladder (`0.284 → 0.257`) and a correspondingly shorter lever
arm — not free, given how leverage-sensitive the fit is (§4.1), so there is no reason to push much
past 2°.

**And it does not need to be wide, if §1.3 lands.** Once the ladder can re-center on the *learned*
offset, mount tolerance only has to survive until first convergence — not forever. That is what lets
you keep it at 2° and keep the lever arm, instead of trading fit quality for a mount range you only
need for the first ten minutes. It also restores the `+0.027` centering the current draft dropped,
as a learned per-device value rather than a hardcoded one.

---

### 1.5 — Override surface (lower priority)

The plumbing already exists — `_flm_vehicle_knob` and the Galaxy param system. Two things worth
exposing:

- **A multiplier on `tau_envelope`, not an absolute.** A scalar in `[0.5, 2.0]` composes with the
  dynamic value instead of replacing it, so an override survives a later change to the formula.
- **The accel gate**, since it is the other half of the tolerance budget (§1.4).

And on the analyzer side, a `--seed-factor` flag so a candidate baseline can be replayed against
existing logs *before* it goes into code. Combined with the existing `TUNING RECOMMENDATION` block —
extended to emit a copy-pasteable seed line — that is the new-platform workflow made repeatable
rather than manual.

---

## 2. Open items against the current draft

### X1 — The generic rollout is not yet validated on a non-Ioniq car
**Severity: High — reframed, since generic is the intent**

Nothing in Step 2 is fingerprint-gated any more, so four changes reach Toyota, Honda, Rivian and the
Bolt simultaneously:

| | Stock | New | Effect |
|---|---|---|---|
| Ladder (Corolla, factor 2.5) | ±0.50 | **±0.64** | 28% wider bins |
| `MIN_BUCKET_POINTS` | `[100,300,500,500,500,500,300,100]` | `[50,200,400,500,500,400,200,50]` | outer halved; sum 3200 → 2300 |
| Deadband (Corolla) | 0.020 | **0.0256** | 28% wider |
| `lat_acc_threshold` | 1.0 | **1.8** | see X5 |

The `min_bucket_points` change is the one that matters: it feeds `is_valid()` → `liveValid` → live
control params, so it is not diagnostic. It lowers the calibration bar on cars that currently
converge correctly.

Given the goal, the ask is not "gate it" — it is **validate it**. Replay at least one non-Ioniq route
(the Bolt is available) and confirm the new ladder still fills and converges to something close to
what that car produces today. §1.1's clip finding means a low-factor car is the more informative
test than a mid-factor one.

### X2 — Dropping the shift is defensible, but the stated justification is not
**Severity: Medium**

§2.2 argues both outer buckets fill because the margins clear (0.068 / 0.121). **Reachability is not
fill rate** — the distinction was worked through in rounds 3–4 and is why the shift existed. Under
the unshifted ±0.28 ladder with the real −0.15 offset:

| Bucket | Range | Lateral accel required |
|---|---|---|
| 0 | `(-0.28, -0.168)` | **1.10 – 1.73 m/s²** |
| 7 | `(0.168, 0.28)` | **0.80 – 1.43 m/s²** |

Bucket 0 needs ~37% more lateral acceleration than bucket 7 for the same 50 points.

Dropping the shift is nonetheless the right call for a *generic* design — the estimator cannot know
the neutral point at boot, so nothing static can center on it. Frame it as an accepted trade-off with
a known cost, and note §1.3/§1.4 as the path that recovers centering as a learned value. In
particular, "the dynamic ladder is robustly self-centering" is not accurate: a ladder symmetric about
0 does not center on a neutral point of +0.027. The *reachability* robustness claim does hold — I
checked the +0.25 counter-example and `[-0.419, +0.330]` clears ±0.28 as stated.

### X3 — §2.1 and §2.3 contradict each other
**Severity: Medium**

§2.1 computes the Corolla envelope as **0.64**. §2.3 then says "On Corolla (τ_envelope = 0.50):
`0.04 × 0.50 = 0.020` (reproduces stock exactly)." Using §2.1's own number the Corolla deadband is
`0.04 × 0.64 = 0.0256`, which does not reproduce stock. Two sections apart, same document.

### X4 — The rest of the document still describes the abandoned design
**Severity: Medium**

§2 was rewritten; nothing downstream followed.

- Executive-summary items 1–3 still describe the `+0.027` shift and neutral-centred deadband.
- §2.4 still assigns `self.neutral_steer` and `self.bucket_cls`; neither exists in the new Step 2.
- The §4 verification table still uses shifted bounds throughout — bucket 0 `[-0.323, -0.183]`,
  bucket 3 `[-0.043, +0.027]`, "0.025 margin", "+0.027-centered deadband". Under the new architecture
  those are `(-0.28, -0.168)`, `(-0.056, 0)` and 0.068.

That table is the pass criteria for the replay, so it has to match what the code will produce.

### X5–X7 — Lower

- **X5:** `lat_acc_threshold = 1.8 if CP.lateralTuning.which() == 'torque' else LAT_ACC_THRESHOLD` is
  effectively unconditional — `use_params` already requires torque tuning, so every car this
  estimator serves takes 1.8. It reads like a guard but is not one.
- **X6:** keep `STEER_MIN_THRESHOLD` as a module constant even though `torqued` no longer uses it —
  the analyzer imports it at line 17 and, per W3, deliberately still uses it in the post-processing
  path.
- **X7:** two sections numbered "3." in §2; Step 4.3's heading line is duplicated.

---

## 3. Closed

| Round | Items | Status |
|---|---|---|
| 1 | multiplier double-count · negative sanity clamp · bucket shadowing · `calPerc`/`liveValid` · `helpers.py` · global threshold | Closed |
| 2 | R1 · R2 · R3 · R4 · R5 · R7 · R8 | Closed. **R6 retracted** (round 5) |
| 3 | S1 – S8 | Closed |
| 4 | T1 – T8 | Closed. T6/T7 were low-value in hindsight |
| 5 | offset clamp · offset scoping · analyzer nits · cosmetic | Closed |
| 6 | W1 – W6 | Closed |

**W1's fix is correct and now subsumes U2 properly.** The rejection chain is intact, the appends sit
inside the `else`, and the three windows are collected at fixed 1.0/1.5/2.5 independent of the replay
gate. Traced: `standard` = ≤1.0, `relaxed` = ≤1.5, `all` = ≤2.5 — identical to the current sets, so
§4's published numbers will reproduce. W3's deliberate asymmetry is commented in place. W2's range
note, W4 (resolved to Option A, the recommendation), W5 and W6 all landed.

---

## 4. Measurement reference

### 4.1 — Window sweep

| Window | Route A (20 min) | n | Route B (1 hr) | n |
|---|---|---|---|---|
| Standard `≤1.0` | 4.8922 | 3,758 | 6.0743 | 19,533 |
| Relaxed `≤1.5` | 5.3512 | 3,968 | 5.9046 | 20,241 |
| All curve `≤2.5` | **5.6401** | 4,223 | **5.6409** | 20,770 |

Two independent drives converge on **5.640** at the widest window — four significant figures.
`6.0743` is the least reliable of the six, being the narrowest window of one route.

This falsified a round-1 hypothesis of mine predicting monotonic downward drift as the window
widened. Route A moves the other way: the behaviour is **sampling instability**, not truncation bias.

**Leverage warning.** Route A gains 465 points (+12%) from `≤1.0` → `≤2.5` yet moves +15%; Route B
gains 1,237 (+6%) and moves −7%. A small edge population sets the fit — which is why §1.4 warns
against shrinking the lever arm, and why §2's W2 expects the bucket-sampled ≤1.8 estimator fit to
land at **5.6–5.9** rather than 5.640 exactly.

### 4.2 — Offset

| Window | Route A | Route B |
|---|---|---|
| `≤1.0` | −0.1614 | −0.1520 |
| `≤1.5` | −0.1514 | −0.1509 |
| `≤2.5` | −0.1413 | −0.1497 |

Six fits, all inside −0.14 to −0.16, window- and route-independent. That stability is the evidence
that it is mount-dominated rather than road-crown-dominated — crown reverses with direction of travel
and varies by road. ≈0.876° of roll. Basis for the Option A decision and for §1.4.

### 4.3 — Friction

| Window | Route A | Route B | A fricAmp | B fricAmp |
|---|---|---|---|---|
| `≤1.0` | 0.0610 | 0.0470 | 0.2982 | 0.2857 |
| `≤1.5` | 0.0673 | 0.0503 | 0.3604 | 0.2972 |
| `≤2.5` | 0.0727 | 0.0548 | 0.4102 | 0.3090 |

Rises with window width in both routes; in lateral-accel units the routes *diverge* as the window
opens. Spread 0.047–0.073, no convergence. Stock 0.09 too stiff by ~1.5×; **~0.055** is right.
`fricAmp ≈ 0.31` is the free headroom term in §1.4's tolerance budget.

### 4.4 — Directional split (contaminated as originally computed)

| | Route A | Route B |
|---|---|---|
| Left (`latAccel > +0.05`) | 5.7227 (n=491) | 5.6040 (n=5,036) |
| Right (`latAccel < −0.05`) | 4.8299 (n=3,141) | 6.6033 (n=13,471) |

Flips sign between routes, so not a rack property — the ±0.05 threshold is one third of the offset,
so straight-line points land in "right curves." **Root Cause 2** (left-turn feedforward trims causing
the bucket asymmetry) remains unsupported; the offset explains it alone.

---

## 5. Verified-facts appendix

| Claim | Verification |
|---|---|
| Stock ladder is exactly `ratios × 0.50` | Verified term for term against `STEER_BUCKET_BOUNDS`, `torqued.py:33` |
| **82 cars in `params.toml`; factor spans 0.77–3.85; 41 pin at the 0.80 clip, 0 at the 0.20 clip** | Basis for **§1.1** |
| `override.toml:115` → `[3.0, 3.0, 0.09]` | Legend `["LAT_ACCEL_FACTOR", "MAX_LAT_ACCEL_MEASURED", "FRICTION"]`; no offset column |
| Raw outputs read 0.0 because the SVD is never invoked | `torqued.py:215-219` |
| `calPerc = (total% + worst-bucket%)/2 → 50` | `helpers.py:85-89`. Matches the observed 50% |
| `is_calculable()` gates only the diagnostic `*Raw` fields | `torqued.py:215-221` — `update_params()` is gated separately by `is_valid()`. Why **§1.3** can use the raw fit to escape a deadlock |
| **`load_points()` re-buckets an existing point list** | `helpers.py:103-105` — a re-size need not discard the drive |
| `latAccelOffset` was passed to `update_params` unclipped | `torqued.py:228-230`, in `get_msg()` |
| PID limits are self-consistent across any factor | `interfaces.py:327-335` — basis for the R6 retraction |
| IONIQ 6 tapers are multiplicative on `output_torque` | `latcontrol_torque.py:582` — invariant under the factor change |
| `latAccelOffset` derives from **device** roll | `torqued.py:199`, uncalibrated for mount |
| The estimator fits bucket-sampled points | `torqued.py:148` → `get_points(fit_points)`, bins capped at `POINTS_PER_BUCKET = 1500` |
| Analyzer appends live inside a 5-deep `if/elif` chain | `inspect_torque_buckets.py:176-196` |
| `IONIQ_6_CARS` has exactly one member | `latcontrol_vehicle_tunes.py:116-118` |
| Stage 1 write ordering is correct | `configure_torque_tune` at `hyundai/interface.py:253`, IONIQ_6 block at `:312` |
| Galaxy UI slider bounds | `starpilot_variables.py:758` — `latAccelFactor * 0.5 … * 1.5` |
| Decimated mode is currently active | `starpilot_variables.py:703-710` ⇒ `torqued.py:260` |
| lagd gates | `lagd.py:28-32` |

---

## 6. Sequencing

1. **Resolve X3 and X4** — the document currently describes two incompatible designs, and the
   verification table is the pass criteria.
2. **Analyzer (Step 4)** with W1/W3 applied. Re-run both routes and confirm §4.1 reproduces the
   published numbers before touching `torqued.py` — that is the check that W1 has not moved them.
3. **Offset sanity clamp**, derived from `MOUNT_TOLERANCE` per §1.4 rather than picked independently.
4. **Step 1 — canonical `CarParams`**: factor 5.64, friction 0.055, offset 0.0; delete the 1.22
   multiplier.
5. **Step 2 — dynamic ladder**, with the clip re-expressed against `steer_max` (§1.1) and the
   envelope derived from the tolerance budget (§1.4).
6. **Step 3 — `controlsd.py` offset decoupling.**
7. **Then §1.3 — envelope re-derivation from the raw fit.** This is the piece that makes the
   architecture actually generic; it is separable from everything above and worth its own change.

### Verification

- **Offline replay**, both routes, standard and decimated. All 8 bins non-empty, `is_calculable()`
  and `is_valid()` True, `calPerc == 100`. Check specifically that **bucket 0 and bucket 7 fill at
  comparable rates** — X2 predicts bucket 0 lags on the unshifted ladder, and that gap is the
  measurable cost of dropping the shift.
- **Replay at least one non-Ioniq route** (X1). A low-factor car is the more informative test, since
  §1.1 shows the clip binds for half the fleet.
- **Record the replayed estimator fit as a measurement**, expected 5.6–5.9. Whatever it returns is
  what the car will converge toward.
- **Unit tests:** `test_torqued.py`, `test_torqued_lat_accel_offset.py`, `test_hyundai.py`. Expect
  churn; review each failure rather than re-baselining. `test_torqued_lat_accel_offset.py` builds
  points directly across `STEER_BUCKET_BOUNDS`, so it exercises the new ladder.
- **Road test.** Feel for the **uniform ~35% reduction in commanded torque** — vaguer steering and
  later turn-in, not instability. Then `latAccelFactorRaw` settling in the replay-predicted range
  after early wander, `latAccelOffsetRaw` near this device's mount roll, `liveValid` True,
  `calPerc` 100.
- Then, separately, whether `liveDelay.calPerc` starts accumulating.
