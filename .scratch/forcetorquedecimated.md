# Add a "Force Faster Torque Convergence" toggle

## Preconditions
- Repo: `C:\Users\pancake\Documents\python\projects\starpilot\StarPilot` (the `StarPilot` checkout specifically — this directory tree has sibling nested git repos, e.g. `sunnypilot/`, which are NOT this one).
- Branch: `wat-ioniq-tuning`. `git branch --show-current` should report this before making changes; if not, `git checkout wat-ioniq-tuning` first. (Verified present as of this plan's last review.)
- All line-number anchors below were verified fresh against this branch's current state; re-verify with a quick `grep -n` before editing in case the branch has moved since — anchors are named by surrounding code (e.g. "after `ForceAutoTuneOff`"), not just line numbers, specifically so they survive minor drift.
- Read this whole document before making any edit. It is written as one strictly-ordered implementation sequence (Steps 1-6 below) — **make the code edits in that order**, but note Step 2 (torqued.py) references `starpilot_toggles.force_torque_decimated`, an attribute that Step 4 (starpilot_variables.py) is what actually adds — `starpilot_toggles` is a plain `SimpleNamespace` (`starpilot_variables.py:454`), so that attribute genuinely does not exist until Step 4 runs. This is fine for applying all six steps' edits before running anything, but **do not try to run or unit-test `torqued.py` in isolation after Step 2 alone** — it will raise `AttributeError` on that line until Step 4 is also applied. Apply all six steps first, then move to Testing. Rationale, risk analysis, and decision history live in the "Background" appendix at the end; it's not required reading to execute correctly, but read it if anything in the steps seems surprising or under-justified.

## Context
The Ioniq 6 torque tuner (`torqued`) is close to `liveValid=True` but blocked by a couple of thin/empty steer buckets (per the `tools/tuning/inspect_torque_buckets.py` investigation on `wat-ioniq-tuning`). `torqued.py` already has a relaxed ("decimated") convergence mode — 10x lower per-bucket point minimums, 600 vs 4000 total points required — but it's only ever engaged automatically, once, at daemon startup, based on whether the *previously cached* `LiveTorqueParameters.liveValid` was true. There is no user-facing way to force it on.

On top of that, one specific bucket (Bucket 0 / Hard Left) sits at 0/10 points even under relaxed thresholds — see "Known limitation" in Background for why the qualifying maneuver essentially never occurs for this car's driving pattern. Relaxing point-count criteria alone can't converge a bucket with zero samples in it.

**This plan ships three things, all gated behind one new toggle plus one independent escape-hatch button:**
1. **`ForceTorqueDecimated` toggle** — forces `torqued` into decimated (relaxed-threshold) mode regardless of cache state.
2. **Empty-bucket exclusion** — when the same toggle is on, a bucket that is *still completely empty* (not just under-threshold) no longer blocks convergence at all. A bucket with *some* data still has to clear the real (decimated) threshold — only a literally empty bucket is excused.
3. **"Clear Learned Torque Data" button** — a separate, always-available action that deletes the cached `LiveTorqueParameters` param, because (2) means the toggle can produce a persisted fit that partially survives turning the toggle back off (see Background → "Residual risk after disabling"); this button is the deliberate, explicit way to undo that.

Plus a read-only dashboard addition (Step 6): surface `torqued`'s convergence status (`liveValid`/`useParams`/`calPerc`) in the_galaxy web dashboard, mirroring the existing steer-delay status card.

This touches settings/toggle plumbing, the `torqued` daemon's estimator-construction and bucket-gating logic, and one read-only dashboard function. It does not touch the actual least-squares fit math itself.

## What ships — checklist
Six files change. Do them in this order:
1. `selfdrive/locationd/helpers.py` — `PointBuckets`: add `allow_empty_buckets`, change `is_calculable()`/`is_valid()`/`get_valid_percent()`.
2. `selfdrive/locationd/torqued.py` — thread `allow_empty_buckets` through `TorqueEstimator`, wire `main()`'s bootstrap check to the new toggle.
3. `common/params_keys.h` — new persistent bool key `ForceTorqueDecimated`.
4. `starpilot/common/starpilot_variables.py` — resolve `toggle.force_torque_decimated`.
5. `selfdrive/ui/layouts/settings/starpilot/lateral.py` — new toggle row (`ForceTorqueDecimated`) + new action row (`ResetTorqueLearning`) + handler (`_clear_torque_learning`).
6. `starpilot/system/the_galaxy/the_galaxy.py` — new `_get_live_torque_status()`, registered into `_get_troubleshoot_learned_values()`.

Then: new + existing automated tests (see "Testing"), then manual/on-device checks, then the tiered review process in "Review checkpoints".

---

## Step 1: `selfdrive/locationd/helpers.py` — `PointBuckets`

Current code (`helpers.py:70-92`):
```python
class PointBuckets:
  def __init__(self, x_bounds: list[tuple[float, float]], min_points: list[float], min_points_total: int, points_per_bucket: int, rowsize: int) -> None:
    self.x_bounds = x_bounds
    self.buckets = {bounds: NPQueue(maxlen=points_per_bucket, rowsize=rowsize) for bounds in x_bounds}
    self.buckets_min_points = dict(zip(x_bounds, min_points, strict=True))
    self.min_points_total = min_points_total

  def __len__(self) -> int:
    return sum([len(v) for v in self.buckets.values()])

  def is_valid(self) -> bool:
    individual_buckets_valid = all(len(v) >= min_pts for v, min_pts in zip(self.buckets.values(), self.buckets_min_points.values(), strict=True))
    total_points_valid = self.__len__() >= self.min_points_total
    return individual_buckets_valid and total_points_valid

  def get_valid_percent(self) -> int:
    total_points_perc = min(self.__len__() / self.min_points_total * 100, 100)
    individual_buckets_perc = min(min(len(v) / min_pts * 100 for v, min_pts in
                                      zip(self.buckets.values(), self.buckets_min_points.values(), strict=True)), 100)
    return int((total_points_perc + individual_buckets_perc) / 2)

  def is_calculable(self) -> bool:
    return all(len(v) > 0 for v in self.buckets.values())
```

`is_calculable()` is the actual hard gate, not `MIN_BUCKET_POINTS`/decimated — it runs before `is_valid()` (called at `torqued.py:215`) and unconditionally requires every bucket to have ≥1 point, independent of any threshold relaxation. Confirmed `PointBuckets`/`is_calculable` has exactly one consumer in the whole tree (`TorqueBuckets` in `torqued.py`) — no other estimator (e.g. lag/delay) uses it, so this class can be changed without cross-estimator blast radius.

**Change**: add `allow_empty_buckets: bool = False` to `__init__`, store as `self.allow_empty_buckets`, and update all three methods that touch per-bucket status:

```python
class PointBuckets:
  def __init__(self, x_bounds: list[tuple[float, float]], min_points: list[float], min_points_total: int, points_per_bucket: int, rowsize: int, allow_empty_buckets: bool = False) -> None:
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

**The `self.__len__() >= 3` clause is load-bearing, not a stylistic touch — do not drop it.** Without it, `all(len(v) > 0 or self.allow_empty_buckets for v in ...)` is unconditionally `True` whenever `allow_empty_buckets` is on, *regardless of point count* — every term in the `all()` becomes `True or True`. That includes the very first `torqued` iteration after a fresh boot, when every bucket has 0 points. `get_msg()` (`torqued.py:215-216`) calls `estimate_params()` whenever `is_calculable()` is true, with no other guard, and `estimate_params()` (`torqued.py:147-159`) runs `np.linalg.svd(points, ...)` then indexes `v.T[0:2, 2]` — which raises an **uncaught `IndexError`** (not `np.linalg.LinAlgError`, the only exception the `try`/`except` there catches) when `points` has fewer than 3 rows. Since `main()`'s loop (`torqued.py:274-275`) calls `get_msg()` unconditionally on `sm.frame % 5 == 0` — i.e. on the very first iteration, before any point could possibly have been collected — enabling `ForceTorqueDecimated` without this floor makes `torqued` **crash on every startup and crash-loop indefinitely**, since a fresh boot always starts at 0 accumulated points. The toggle would never actually function. `3` is the minimum for the SVD slice `v.T[0:2, 2]` to have enough rows/cols to not raise; it's deliberately *not* tied to `min_points_total` (600 decimated / 4000 default) — this floor exists purely to prevent a crash on the calculation path, not to gate whether the fit is trustworthy (that's `is_valid()`'s job, unchanged). In the real target scenario (7 buckets fully filled, only Bucket 0 empty) this floor is satisfied thousands of points over, so it changes nothing about the feature's actual behavior — it only prevents the degenerate cold-start crash.

Key semantics:
- A bucket with **zero** points is excused from both `is_calculable()` and `is_valid()` when `allow_empty_buckets` is on, but `is_calculable()` still requires ≥3 points *total* across all buckets combined (the crash-prevention floor above) — this is new in this plan, not present in the pre-existing `is_calculable()`.
- A bucket with **some but under-threshold** data (1 to min_pts-1 points) is **not** excused — it still has to clear the real (decimated) minimum in `is_valid()`. Only a literally empty bucket gets the pass.
- `get_valid_percent()`'s fix (the `bucket_percs` filtering) is not optional cosmetic polish — without it, an excused-empty bucket contributes `0/min_pts*100 = 0` to the `min()`, so `calPerc` would report **0% forever** even once `is_valid()`/`liveValid` goes `True`. That directly contradicts Step 6's dashboard card, which shows `calPerc` as a progress readout right next to `liveValid`. The `if bucket_percs else 100` guard handles the all-buckets-excused case — note this isn't a rare degenerate case, it's the routine state on every fresh boot with the toggle on (before Bucket 0 or any other bucket has real data), and `get_valid_percent()` is called unconditionally every frame (`torqued.py:239`) regardless of `is_calculable()`'s point-count floor, so this branch fires often; it just needs to not raise on an empty sequence, which it doesn't.
- `get_points()` (SVD fit input via `vstack`) and `is_valid()`'s existing `total_points_valid = self.__len__() >= self.min_points_total` check are **unchanged**, and don't need any edit — an empty `NPQueue` is `np.empty((0, rowsize))`, which `vstack`s and divides safely (no div-by-zero: `buckets_min_points` denominators stay at their decimated values, e.g. 10 for bucket 0, never 0). Note `min_points_total` (600 decimated) is *not* what prevents the `is_calculable()` crash above — `is_valid()` and its total-points check run only *after* `is_calculable()` already gated whether `estimate_params()` gets called at all, so the separate `>= 3` floor in `is_calculable()` is the thing actually standing between this change and a startup crash-loop.
- Default behavior (`allow_empty_buckets=False`, the parameter's default) is byte-for-byte identical to today for all three methods — every new clause either evaluates to `False` (the `or self.allow_empty_buckets` terms) or is trivially satisfied (`is_calculable()`'s `self.__len__() >= 3`, implied by the pre-existing "every bucket has ≥1 point" requirement across 8 buckets).

`TorqueBuckets` (`torqued.py:47`) is a direct subclass of `PointBuckets` with no `__init__` override, so it inherits the new parameter automatically — no edit needed there.

---

## Step 2: `selfdrive/locationd/torqued.py` — thread the flag through, wire the bootstrap

**2a. `TorqueEstimator.__init__` and `reset()`** (currently `torqued.py:56` / `:136`):

Current signature: `def __init__(self, CP, decimated=False, track_all_points=False):`

Change to: `def __init__(self, CP, decimated=False, track_all_points=False, allow_empty_buckets=False):`, and store `self.allow_empty_buckets = allow_empty_buckets` **before** the `self.reset()` call at line 85 (the existing `__init__` already calls `self.reset()` partway through, and `reset()` is what constructs the `TorqueBuckets` — the new attribute must exist before that call, not after).

`reset()` (`torqued.py:136-144`) constructs `TorqueBuckets(x_bounds=..., min_points=..., min_points_total=..., points_per_bucket=..., rowsize=3)` — add `allow_empty_buckets=self.allow_empty_buckets` to that call.

**2b. `main()`'s decimated bootstrap check** (currently `torqued.py:261`) — **this references `starpilot_toggles.force_torque_decimated`, which Step 4 is what adds; if applying steps out of order, do Step 4 before testing this one**:

Current code: `estimator = TorqueEstimator(messaging.log_from_bytes(params.get("CarParams", block=True), car.CarParams), decimated=True)`, guarded by `if not starpilot_toggles.liveValid:`.

Change to:
```python
force_decimated = starpilot_toggles.force_torque_decimated
if not starpilot_toggles.liveValid or force_decimated:
  estimator = TorqueEstimator(
    messaging.log_from_bytes(params.get("CarParams", block=True), car.CarParams),
    decimated=True, allow_empty_buckets=force_decimated,
  )
```

**Critical scoping decision — read carefully, this is the highest-stakes line in the whole plan**: `allow_empty_buckets` is wired to `starpilot_toggles.force_torque_decimated` (the explicit user toggle from Step 4), **not** to the `decimated` flag itself, and not to the `not starpilot_toggles.liveValid` cold-boot condition. Concretely: a car cold-booting with no cached `liveValid` (today's existing automatic behavior, every car, no toggle involved) still gets `decimated=True, allow_empty_buckets=False` — unchanged, still requires ≥1 point in all 8 buckets. Only when the user explicitly flips `ForceTorqueDecimated` on does `allow_empty_buckets=True` kick in. This keeps the empty-bucket-exclusion behavior fully opt-in: every car that isn't this Ioniq 6 with this toggle enabled sees zero behavior change from this plan.

---

## Step 3: `common/params_keys.h`

Struct shape (`common/params.h:41-53`): `ParamKeyAttributes = {flags, type, default_value, stock_value, tuning_level, settings_tier}` (last field optional, defaults to `SETTINGS_ADVANCED`).

Add, immediately after the existing `ForceAutoTuneOff` line (`common/params_keys.h:382`):
```cpp
{"ForceTorqueDecimated", {PERSISTENT, BOOL, "0", "0", 3}},
```
This is `ForceAutoTune`'s exact tuple shape (`{PERSISTENT, BOOL, "0", "0", 3}` — default off, stock value `"0"`, `tuning_level=3`, tier defaulted to `SETTINGS_ADVANCED`) — copy verbatim, confirmed correct against the struct definition above, not just "looks like the pattern."

---

## Step 4: `starpilot/common/starpilot_variables.py`

In `update()`, insert after the `toggle.force_auto_tune_off = ...` line (`starpilot_variables.py:777`), before `toggle.flm_active_profile_id = ...` (line 778):

```python
toggle.force_torque_decimated = self.get_value(
  "ForceTorqueDecimated", condition=advanced_lateral_tuning and is_torque_car and not is_angle_car,
)
```

Gated the same way as `ForceAutoTune`/`ForceAutoTuneOff` (needs `AdvancedLateralTune` parent + torque-tuned, non-angle car — `advanced_lateral_tuning`, `is_torque_car`, `is_angle_car` are all already in scope in this function, same as they are for the neighboring `force_auto_tune`/`force_auto_tune_off` lines). Deliberately **without** `ForceAutoTune`'s extra `not has_auto_tune` guard — this toggle is about convergence speed, not "force auto-tune on when it wouldn't otherwise run," and stays meaningful even on auto-tune-eligible cars that haven't converged yet.

---

## Step 5: `selfdrive/ui/layouts/settings/starpilot/lateral.py`

**5a.** Add `"ForceTorqueDecimated"` to `_ADVANCED_LATERAL_KEYS` (`lateral.py:34`, currently `["ForceAutoTune", "ForceAutoTuneOff"]`) so it participates in parent-toggle sync via `_sync_parent`. (`ResetTorqueLearning`, added in 5c below, does **not** go in this list — it's an action button with no persisted bool state of its own, nothing to sync.)

**5b.** Insert a new `SettingRow` right after the existing `ForceAutoTuneOff` row (`lateral.py:277`, right before `UseAutoSteerDelay`):

```python
SettingRow(
  "ForceTorqueDecimated", "toggle", tr_noop("Force Faster Torque Convergence"),
  subtitle=tr_noop(
    "Relaxes auto-tune's convergence requirements to reach a valid result sooner, including letting "
    "a torque range with no real samples influence the fit. Also widens how far learned values may "
    "drift before being clamped. Turning this off stops it from affecting new learning, but does not "
    "undo an already-converged result."
  ),
  get_state=lambda: p.get_bool("ForceTorqueDecimated"),
  set_state=lambda s: (
    _confirm_reboot_toggle(p, "ForceTorqueDecimated", s) if s else p.put_bool("ForceTorqueDecimated", False),
    s and p.put_bool("ForceAutoTuneOff", False),
    _sync_parent(p, "AdvancedLateralTune", _ADVANCED_LATERAL_KEYS),
  ),
  enabled=lambda: cs.isTorqueCar and not cs.isAngleCar,
  disabled_label=tr_noop("Not Available"),
  visible=alt_on,
),
```

Notes on this row:
- **Reboot-confirm on enable only, not disable**: `decimated`/`allow_empty_buckets` are baked into `TorqueEstimator` once at `torqued` process start, not re-evaluated in its hot loop, so the toggle needs a daemon restart to take effect. Uses the existing `_confirm_reboot_toggle` pattern (already used for `AlwaysOnLateral`, `lateral.py:21-29`), prompted only when turning the toggle **on** — matches the ternary in `set_state` above (`if s else p.put_bool(...)`).
- **`enabled=` deliberately does NOT include `not cs.hasAutoTune`**, unlike `ForceAutoTune`'s row. `cs.hasAutoTune` is set from `liveTorqueParameters.useParams` (`selfdrive/ui/lib/starpilot_state.py:227`) — a flag about whether cached/default params are currently trusted enough to drive on, independent of `liveValid`/convergence. The Ioniq 6 this plan targets has `useParams=True` and `liveValid=False` simultaneously — copying `ForceAutoTune`'s condition verbatim would render this row "Not Available" on exactly the car it's meant for. The condition above matches `starpilot_variables.py`'s gating (Step 4) exactly: `is_torque_car and not is_angle_car`, no `hasAutoTune` involvement. This matches the existing `ForceAutoTuneOff` row's condition (`lateral.py:274`), not `ForceAutoTune`'s.
- Mutually exclusive with `ForceAutoTuneOff` (clears it on enable, matching how `ForceAutoTune`/`ForceAutoTuneOff` already clear each other) since `ForceAutoTuneOff` locks vehicle-model params and would directly block the convergence this toggle is meant to accelerate.
- Subtitle intentionally flags both the sanity-clamp widening and the empty-bucket-fit risk, without spelling out the exact `FACTOR_SANITY_QLOG`/`FRICTION_SANITY_QLOG` numbers, and intentionally does **not** say "turn off once satisfied" — see Background → "Residual risk after disabling" for why that phrasing would be misleading.

**5c.** Immediately below that row, add a second row — the manual escape hatch for the risk described in Background → "Residual risk after disabling." Modeled on the existing `ResetCurve`/`_reset_curve_data` pattern (`selfdrive/ui/layouts/settings/starpilot/longitudinal.py:697-701` row, `:1031-1040` handler): an `"action"`-type `SettingRow` behind a `ConfirmDialog`, not a toggle.

```python
SettingRow(
  "ResetTorqueLearning", "action", tr_noop("Clear Learned Torque Data"),
  subtitle=tr_noop(
    "Permanently deletes all learned steering torque data (all 8 buckets) and restarts learning from "
    "scratch on the next drive. Use this to force a fresh, fully-verified fit instead of keeping a result "
    "that was reached with Force Faster Torque Convergence enabled."
  ),
  action_text=tr_noop("Clear"),
  action_danger=True,
  on_click=lambda: _clear_torque_learning(p),
  enabled=lambda: cs.isTorqueCar and not cs.isAngleCar,
  visible=alt_on,
),
```

Handler — a top-level function alongside `_confirm_reboot_toggle` (`lateral.py:21`), not a class method, matching that function's shape (takes `params` explicitly):

```python
def _clear_torque_learning(params):
  from openpilot.selfdrive.ui.ui_state import ui_state
  from openpilot.system.ui.widgets.confirm_dialog import ConfirmDialog

  def on_close(res):
    if res != DialogResult.CONFIRM:
      return
    params.remove("LiveTorqueParameters")
    if ui_state.started:
      gui_app.push_widget(ConfirmDialog(
        tr("Reboot required. Reboot now?"), tr("Reboot"), tr("Cancel"),
        callback=lambda res2: HARDWARE.reboot() if res2 == DialogResult.CONFIRM else None,
      ))

  gui_app.push_widget(ConfirmDialog(
    tr("This permanently deletes all learned torque data, including any hard-left/-right coverage collected "
       "while Force Faster Torque Convergence was on. torqued will cold-start from stock values on the next "
       "restart. This cannot be undone."),
    tr("Clear Data"), tr("Cancel"), callback=on_close,
  ))
```

Notes:
- **Chaining two `ConfirmDialog`s** (delete-confirmation, then reboot-confirmation, pushed from inside the first's callback) is a precedented pattern in this codebase — e.g. `system_settings.py` routinely pushes a follow-up `alert_dialog`/widget from inside a `ConfirmDialog` callback. Not a novel construct.
- **Why the reboot prompt**: `torqued` only reads `LiveTorqueParameters` from the cache at process `__init__` (`torqued.py:99-122`) — clearing the param while `torqued` is already running has no effect on the live `filtered_params` until the daemon restarts, the same "bakes in at start" constraint that makes the toggle itself need `_confirm_reboot_toggle`. This reuses the identical reboot-dialog snippet inline rather than factoring it out, to avoid coupling this plan to a refactor of `_confirm_reboot_toggle`'s signature (which currently only handles a single param-set, not an arbitrary side effect).
- **Scope**: this button is independent of the `ForceTorqueDecimated` toggle's current state — available (and useful) whether the toggle is on or off, since its job is undoing accumulated cache state, not controlling future fitting mode. It does not touch `ForceTorqueDecimated`/`ForceAutoTuneOff`'s bool params.
- Only removes `"LiveTorqueParameters"` — does not touch `"CarParamsPrevRoute"` (used by `torqued.py:109`'s restore-key check for an unrelated purpose — comparing car fingerprint across boots) or any other param.

---

## Step 6: `starpilot/system/the_galaxy/the_galaxy.py` — dashboard status card

Independent, read-only addition: surface whether `torqued` is converged in the_galaxy (the Flask-based web dashboard, `the_galaxy.py`, ~10.4k lines, serving both API routes and the `assets/` frontend). This is a separate file from Steps 1-5's UI (`selfdrive/ui/layouts/settings/starpilot/` is the in-car Python UI; the_galaxy is a browser-facing Flask app reached only via REST polling, no push/websocket) — **do not skip this step because it looks cosmetic; it's still part of this change set.**

There's an existing, directly-mirrorable pattern for a sibling learner (steer delay/`lagd`): `_get_steer_delay_learned_text()` (`the_galaxy.py:4090-4123`) reads the persisted Params key `"LiveDelay"` via `_safe_params_get_live_raw`, decodes with `messaging.log_from_bytes(bytes, log.Event).liveDelay`, and formats a `"Learning {calPerc}% (...)"`-style string. It's registered into `_get_troubleshoot_learned_values()` (`the_galaxy.py:4125-4128`) under key `"SteerDelay"`, which feeds the existing `/api/troubleshoot` route and is rendered generically by `assets/components/tools/troubleshoot.js` (badge styling via `statusBadgeClass()`) — no route or frontend changes needed for a new entry in that dict, it's already rendered generically.

`torqued.py:280` already persists what's needed every ~60s onroad (`params.put_nonblocking("LiveTorqueParameters", msg.to_bytes())`). `starpilot_variables.py:736-743` shows the correct decode pattern for this specific param — **note it differs from the `LiveDelay` pattern above**: `LiveTorqueParameters` decodes directly as `log.LiveTorqueParametersData` (not wrapped in `log.Event`):
```python
msg_bytes = self.params.get("LiveTorqueParameters")
if msg_bytes:
  LTP = messaging.log_from_bytes(msg_bytes, log.LiveTorqueParametersData)
  # LTP.useParams, LTP.liveValid, LTP.calPerc
```

**Add** `_get_live_torque_status()` in `the_galaxy.py` next to `_get_steer_delay_learned_text()` (~line 4090), same shape: read Params `"LiveTorqueParameters"` via `_safe_params_get_live_raw`, decode with `log.LiveTorqueParametersData` (per the snippet above, not `log.Event`), and format a string/severity from `liveValid` (ok/not-yet), `useParams` (whether the car is actually driving on learned vs. stock values), and `calPerc` (progress, 0-100). Handle the "no cached param yet" case gracefully (return `"Unavailable"`, mirroring `_get_steer_delay_learned_text`'s own `if not live_delay_bytes: return "Unavailable"` guard).

**Register** it into `_get_troubleshoot_learned_values()` (`the_galaxy.py:4125-4128`) alongside `"SteerDelay"`, as a new `"LiveTorque"` entry:
```python
def _get_troubleshoot_learned_values():
  return {
    "SteerDelay": _get_steer_delay_learned_text(),
    "LiveTorque": _get_live_torque_status(),
  }
```

That's the entire Step 6 diff — one new function, one new dict entry. No new route, no frontend changes.

**Explicitly out of scope for this card**: showing whether `torqued` is currently running in "decimated" mode. That flag isn't in the `liveTorqueParameters` message — it's an internal runtime decision baked in at `torqued` process start (`torqued.py:258-261`). Surfacing it accurately would require adding a field to the cereal message (a schema change, bigger blast radius) rather than inferring it client-side, which would just re-derive the same startup heuristic and could visibly disagree with what the daemon actually did if it crashed/restarted under different conditions. Leave this out unless explicitly asked for later (tracked as open item in Background).

---

## Testing

**Existing tests to run first (baseline — confirm nothing regresses before adding new ones)**:
- `selfdrive/locationd/test/test_torqued.py` — has `test_cal_percent()`, exercising `TorqueEstimator`/`filtered_points` with default (`allow_empty_buckets=False`) construction; should be unaffected by Steps 1-2 but is the natural home for new tests.
- `starpilot/common/tests/test_starpilot_variables.py` — has the existing `test_get_starpilot_toggles_*` suite (e.g. `test_get_starpilot_toggles_uses_persisted_force_torque_request`), showing the monkeypatch pattern used to test toggle resolution; mirror it for `force_torque_decimated`.
- `starpilot/system/the_galaxy/tests/test_dashboard_stats.py` — existing dashboard-stat tests using a `monkeypatch`-based pattern; add a test for `_get_live_torque_status()`/its entry in `_get_troubleshoot_learned_values()` here.

Run via `python -m pytest selfdrive/locationd/test/test_torqued.py starpilot/common/tests/test_starpilot_variables.py starpilot/system/the_galaxy/tests/test_dashboard_stats.py -v` (adjust invocation to however this repo normally runs pytest, e.g. via `uv run` — check for a `pyproject.toml`/`Makefile` target first).

**New tests to add**:
- In `test_torqued.py`: **first, the crash-regression test for the `is_calculable()` floor** — construct `TorqueEstimator(car.CarParams(), allow_empty_buckets=True)` with all buckets left empty (the actual fresh-boot state), and assert `get_msg()` does not raise (this is the direct regression test for the `IndexError` bug caught during review — construct it, call `get_msg()` immediately with zero points added, before anything else, and confirm no exception). Then, separately: construct `TorqueEstimator(car.CarParams(), allow_empty_buckets=True)` (or however the param threads through — see Step 1/2), leave one bucket completely empty and another partially filled below its threshold (with total points across all buckets well above 3), assert `is_calculable() is True`, `is_valid() is False` until the partial bucket clears its (decimated) threshold, and that `get_msg()`/`get_valid_percent()` don't raise. **Also assert `get_valid_percent()` does not count the excused-empty bucket toward `individual_buckets_perc`** — e.g. fill every other bucket to 100% and total points past the floor, leave the target bucket empty, assert `get_valid_percent() == 100` (not dragged down by a 0-point bucket). Also assert the *default* (`allow_empty_buckets=False`) path still requires every bucket `> 0` for both `is_calculable()` and `get_valid_percent()`, unchanged from today.
- In `test_starpilot_variables.py`: mirror the existing `force_auto_tune`-style test to assert `toggle.force_torque_decimated` resolves correctly under `AdvancedLateralTune`/torque-car gating, and is `False` when the parent toggle or torque-car condition isn't met.
- In `test_dashboard_stats.py`: assert `_get_live_torque_status()` correctly reflects `liveValid`/`calPerc`/`useParams` from a synthesized `LiveTorqueParameters` param blob, and handles the "no cached param yet" case gracefully (mirror however `_get_steer_delay_learned_text()` is tested, if it has coverage — check first).

## Manual/on-device checks (after automated tests pass)

1. `python -m py_compile` the changed Python files as a fast syntax sanity check before running the real test suite.
2. Enable the toggle in Settings → Lateral → Advanced, confirm the reboot prompt appears, reboot, then re-run `tools/tuning/inspect_torque_buckets.py <route> --data-dir /data/media/0/realdata --decimated` (per-bucket `Required` column should show the 10x-lower decimated thresholds, `Total Points required` should read 600 instead of 4000) to confirm `torqued` is running in decimated mode. Note the inspect script itself doesn't know about `allow_empty_buckets` — it's a `torqued.py`/`helpers.py`-internal mechanism, not exposed as a script flag — so the script's own `is_calculable()`/`is_valid()` will still report the empty bucket as blocking; the real check is whether the live `liveTorqueParameters` topic/cached param shows `liveValid=True` despite an empty bucket, not what the offline script reports.
3. Confirm `liveTorqueParameters.liveValid` flips true with Bucket 0 still empty once the toggle is on and all other buckets/total points clear the decimated thresholds (check via `cabana`/`plotjuggler` on the live `liveTorqueParameters` topic, or the Step 6 the_galaxy status card — load `/api/troubleshoot` or the corresponding page in a browser and confirm the new card shows `liveValid=True`/`calPerc=100`, not 0% — this is the direct on-device check for the `get_valid_percent()` fix in Step 1).
4. Turn the toggle back off and confirm no reboot prompt fires (disable path applies immediately) and that a subsequent `torqued` restart reverts to requiring real coverage in all 8 buckets **for future fitting** — then separately check `liveTorqueParameters.latAccelFactorFiltered`/`frictionCoefficientFiltered`/`latAccelOffsetFiltered` after that restart and confirm they still reflect the extrapolated fit (expected — see Background → "Residual risk after disabling"; this is accepted behavior, not a bug). Don't assume disabling reverted steering behavior just because `liveValid` reads `False` again.
5. Tap "Clear Learned Torque Data", confirm the deletion dialog and the follow-up reboot dialog both appear (onroad), reboot, and confirm `liveTorqueParameters` cold-starts from stock values with all 8 buckets empty again — i.e. the extrapolated fit from check 3 is actually gone this time, unlike the toggle-disable check in 4. Also confirm it does *not* fire a reboot prompt when tapped offroad (mirrors `_confirm_reboot_toggle`'s `ui_state.started` guard) and that `CarParamsPrevRoute` is untouched (spot-check via `params.get("CarParamsPrevRoute")` before/after).

---

## Review checkpoints
Given this touches a live control-adjacent daemon (`torqued`) as well as read-only UI/dashboard code, review effort is split by blast radius:

- **Step 1 (`helpers.py`) + Step 2 (`torqued.py`)** — the highest-stakes files in this whole plan: together they decide whether a fitted torque model with zero real coverage in some torque range gets applied to actual steering. **Manual (human) review required** before merge, specifically re-checking: (a) `allow_empty_buckets` is wired to `starpilot_toggles.force_torque_decimated` only, never to the automatic cold-boot `decimated` bootstrap (so unrelated cars/fresh installs are unaffected); (b) a bucket with *partial* (nonzero but under-threshold) data still enforces the real minimum, only a truly empty bucket is excused; (c) **`is_calculable()`'s `self.__len__() >= 3` floor is present and not accidentally dropped or refactored away** — its absence makes `torqued` crash-loop on every fresh boot with the toggle on (caught during adversarial review of this plan itself; verified by tracing `get_msg()` → `estimate_params()` → an uncaught `IndexError` from `np.linalg.svd`/`v.T[0:2, 2]` on <3 points — the `except np.linalg.LinAlgError` there does not catch it); (d) `get_valid_percent()`/`get_points()`/`vstack` behave correctly with a genuinely empty bucket (division-by-zero, shape mismatches) — confirmed safe via `NPQueue`'s `(0, rowsize)` empty-array shape, but re-verify against the actual diff; (e) the `or` logic in `main()` doesn't accidentally widen sanity clamps or drop bucket requirements when the toggle is off. Run an automated review pass first (`/code-review` at default effort) to catch mechanical issues, but do not treat it as sufficient sign-off for these files alone — **the `IndexError` bug above was not caught by an earlier version of this plan's own reasoning about "safe because vstack/division is safe," so don't assume prose-level safety claims in this doc are exhaustive either; re-derive from the actual diff.**
- **Step 3 (`params_keys.h`) + Step 4 (`starpilot_variables.py`)** — mechanical, follows an exact existing pattern (`ForceAutoTune`/`ForceAutoTuneOff`). Automated subagent review (`/code-review`) is sufficient; spot-check manually only if the automated pass flags something.
- **Step 5a/5b (`lateral.py`, `ForceTorqueDecimated` toggle row)** — UI/cosmetic, no control logic. Automated review sufficient.
- **Step 5c (`lateral.py`, `ResetTorqueLearning` action row + `_clear_torque_learning`)** — destructive (irreversibly deletes cached learned data) but not a live-control-logic change and easy to reason about in isolation (single `params.remove()` call gated behind a confirm dialog, mirrors the existing `_reset_curve_data` pattern exactly). Automated review sufficient, but spot-check manually that it only removes `"LiveTorqueParameters"` and nothing else.
- **Step 6 (the_galaxy dashboard addition)** — read-only display of already-persisted data, no write path, no control logic. Automated review sufficient; no manual gate needed.
- **Before opening a PR / merging to a shared branch**: one final `/code-review` pass (high effort) across the full diff, plus a manual pass specifically re-reading the Step 1-2 diff in isolation since it's the only part with real safety/behavior surface.

### Escalation criteria — when an "automated is sufficient" tier stops being sufficient
The per-step tiers above are defaults, not a free pass. Escalate to manual (human) review, regardless of which file the finding is in, if any of the following happen during an automated `/code-review` pass:
- **Any `CONFIRMED`-severity finding** (not `PLAUSIBLE`) in Steps 3, 4, 5, or 6 — a confirmed bug in "mechanical" plumbing still needs a human to judge whether it's actually mechanical or whether it's masking a real behavioral issue (e.g. a wrong `condition=` gate that silently changes when `force_torque_decimated` becomes true).
- **Any finding that touches or references `torqued.py`, `helpers.py`, `PointBuckets`/`TorqueBuckets`, `is_calculable`/`is_valid`, or the `decimated`/`allow_empty_buckets` flags**, even if the finding's own file is nominally in an "automated sufficient" tier (e.g. a Step 4 finding about how `force_torque_decimated` is computed) — anything that traces back to the convergence-gating logic inherits the manual-review requirement of Steps 1-2.
- **Any finding categorized as `correctness` or `efficiency`-with-behavior-impact** (as opposed to pure style/simplification) in any file touched by this plan.
- **The automated review fails to run, times out, or returns no output** for any file in the diff — treat as "not yet reviewed," not as an implicit pass. Don't merge on the assumption that no output means no findings.
- **Any change outside the six files listed in "What ships"** (scope creep during implementation) — re-classify it against this same tier system before merging; don't assume it's automated-sufficient by default just because it wasn't planned as safety-relevant.
- **Two or more independent `PLAUSIBLE` findings that describe the same underlying mechanism** (even if neither alone is `CONFIRMED`) — treat as a signal worth a human look rather than dismissing each individually.

If none of the above trigger, the automated pass stands as sufficient for that tier and no additional manual gate is needed beyond what's already specified per step.

---

## Background (rationale and decision history — read if something above seems under-justified; not required for correct execution)

### Known limitation (confirmed via on-device test on `wat-ioniq-tuning`)
Ran `inspect_torque_buckets.py --decimated` against the current Ioniq 6 route: every bucket is at 100% fill *except* Bucket 0 (Hard Left), which sits at **0/10 — completely empty**, even under relaxed thresholds. Relaxing the point-count criteria cannot converge a bucket with zero samples in it. The real-world blocker is behavioral: `torqued.py:200`'s point-acceptance filter requires, simultaneously: `lat_active` for the full 2s pre-buffer, `not any(steer_override)` (driver touching the wheel voids the whole window), `vego > MIN_VEL` (15 m/s / 33.5 mph — low-speed turns never count, override or not), and `abs(lateral_acc) <= 1.0 m/s²`. Bucket 0 needs steer torque in [-0.5, -0.3] under those conditions — a sustained, hands-off, highway-speed left curve with real steering effort but a moderate resulting curve. After days of driving without landing this combination, the decision was made to just drop the bucket-0 requirement instead of continuing to chase the maneuver — hence Step 1/2's empty-bucket exclusion, folded into the same toggle rather than shipped as a separate control.

### Why lowering the point-count bar alone doesn't work
See Step 1 — `is_calculable()` is the actual hard gate, not `MIN_BUCKET_POINTS`. This was the reasoning that led to the empty-bucket-exclusion design rather than just widening `MIN_BUCKET_POINTS` further.

### Restated risk — this is the part that actually changes what ships to the wheel
With the toggle on and Bucket 0 excused, `latAccelFactor`/`friction`/`offset` get fit and applied to real steering with **zero empirical samples** in whichever torque range stays empty (here, hard-left). The fit is a pure extrapolation from center/moderate/right data into that range. Given the already-observed left/right asymmetry (fitted factor 4.61 left vs 5.68 right), this is exactly where extrapolation is least trustworthy. Accepted reasoning: requiring even 1 point doesn't practically help since the qualifying maneuver hasn't occurred in days of driving anyway, so a partial floor buys no safety margin over a full drop for this actual situation — a deliberate tradeoff, not an oversight.

### Residual risk after disabling (confirmed via code trace, not just theoretical)
"Turning the toggle back off returns to full 8-bucket coverage requirements on the next `torqued` restart" is true for *future* fitting, but does **not** undo an already-blended extrapolated fit. Traced concretely:
- `controlsd.py:488`: `use_live_params = ... and (torque_params.useParams or force_auto_tune)` — the actual lateral controller (`get_torque_control_params`, `controlsd.py:353-367`, consuming `latAccelFactorFiltered`/`latAccelOffsetFiltered`/`frictionCoefficientFiltered`) is gated on `useParams`, **never on `liveValid`**.
- `useParams` (`torqued.py:77`: `self.use_params = CP.brand in ALLOWED_CARS and CP.lateralTuning.which() == 'torque'`) is a static, CP-derived eligibility flag — true for this Ioniq 6 regardless of convergence state, toggle state, or session history.
- `torqued.py:99-122` restores `filtered_params` (the actual smoothed values fed to the controller) from the cached `LiveTorqueParameters` param on every `torqued` boot, if the cache's `liveValid` was `True` when it was last written. Once the toggle-assisted fit reaches `is_valid()` even once (with Bucket 0 still empty), that blended value gets persisted (`torqued.py:280`, every ~60s onroad) and becomes the seed for every future boot — **including boots after the toggle is turned back off**.
- After disabling, strict mode (`allow_empty_buckets=False`) keeps `is_calculable()` false (Bucket 0 still empty), so `update_params()` never runs again to move the filter away from the seeded value — it stays frozen there and keeps being fed to the live controller indefinitely, because consumption depends on `useParams` (always true), not on whether this session's `liveValid` is true or false.

**Net effect**: "temporary" and "turn off once satisfied" describe the *UI toggle state* accurately, but not the *steering behavior* — the car keeps driving on the extrapolated fit after disabling, until either (a) the toggle is turned back on and Bucket 0 eventually gets real data under relaxed criteria, or (b) Step 5c's "Clear Learned Torque Data" button is used.

**Resolved**: chose not to auto-clear the cache on disable — losing the 7 already-well-filled buckets (reverting to offline/stock-baseline values until a full re-learn) is worse than keeping an extrapolated-but-converged fit, and doing it automatically on every disable would surprise a user who just wanted to pause, not reset. Step 5c's explicit button is the deliberate, opt-in alternative: disabling the toggle stops *future* extrapolated fitting; clearing the cache (separately, explicitly) undoes the *already-persisted* one. The old "turn off once satisfied" subtitle phrasing would have been misleading (implies disabling reverts behavior, which it doesn't) — Step 5b's subtitle states the persistence plainly instead.

### Decisions made (kept for rationale — not open questions at execution time)
1. **UI copy**: the exact subtitle text is in Step 5b/5c above — use it verbatim, it already includes the sanity-clamp, empty-bucket-fit, and persistence caveats.
2. **Reboot-confirm on enable only, not disable**: encoded in Step 5b's `set_state` lambda — disabling applies immediately, enabling prompts via `_confirm_reboot_toggle`.
3. **`params_keys.h` trailing tuple fields**: resolved by reading `common/params.h:41-53` directly — `{flags, type, default_value, stock_value, tuning_level, settings_tier}`. See Step 3.
4. **Gated under `AdvancedLateralTune`**: encoded in Step 4's `condition=` and Step 5b/5c's `visible=alt_on` — requires the parent toggle, matching `ForceAutoTune`/`ForceAutoTuneOff`.
5. **No cache-clear on toggle disable, but an explicit clear button instead**: see "Residual risk after disabling" above and Step 5c.

### Genuinely open (execution should sanity-check, not block on)
- Whether the_galaxy card (Step 6) should also attempt to surface "decimated"/"empty-bucket-drop" runtime mode — current recommendation is no (see Step 6's "Explicitly out of scope" note), but flagging in case it's wanted after seeing the card without it.
