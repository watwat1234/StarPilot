# StarPilot boot/wake-on-power investigation (two related fixes, one file)

*Cold-start note: this file is self-contained. If context was reset, just read
this file top to bottom — no need to also find the `/plan` mode plan file.*

*This file now covers **two** separate fixes for related "StarPilot doesn't
wake like sunnypilot does" symptoms on the same hardware. Fix 1 (GPIOC11/
DC_IN bootkick) is built, flashed, and partially validated but has an open
question (see below). Fix 2 (CAN/SBU stop-mode wake) has its source edit,
build, and regenerated-firmware commit done and pushed — it's the one
actively being worked, next up is reflashing and the real hardware test.
Each has its own "Fix — discrete steps" section further down, each
restarting its own Step 1/2/3/etc. — don't confuse the two when a step
number is mentioned out of context.*

## Status (read this first)

**Fix 1 — GPIOC11/DC_IN bootkick (unplug/replug-OBDII wake).** Steps 1-3
(source edit, build, reflash) done. **Steps 4-6 downgraded from DONE to
NEEDS RE-VALIDATION (2026-09-11)** — see "Reopened" below for why. Step 7
(report upstream) explicitly deferred by the user, unaffected either way.
Not currently being worked — superseded in priority by Fix 2, since the
actual real-world trigger the user cares about (door-unlock) turned out to
depend on Fix 2's mechanism, not Fix 1's.

**Fix 2 — CAN/SBU stop-mode wake (the actual door-unlock trigger).**
Root cause identified, feasibility independently verified against source,
one independent-review pass completed and its corrections folded in
(F4-build-break fix, safety-comment reframing, two added regression checks,
HKG-remote-start mechanism fully understood — no code risk, but confirmed
relevant to the user's actual usage so it stays in as a live Step 5 check).
**Status: Steps 1-2b DONE.** All blocking open questions were answered
(see "Open questions / decisions needed" below), branch `wat-can-sbu-wake`
was created off `wat-boot-update`, and the source edit (STM32H7-guarded
`enter_stop_mode()` ported into `power_saving.h`, gated call site added
in `main.c`'s power-save loop) is committed as `9a6fbe937`. WSL machine
re-check (item 4) confirmed the toolchain works (root-level `.venv` in
this worktree already had `scons`/`arm-none-eabi-gcc`, no `setup.sh`
needed). Build (Step 2a) succeeded on 2026-09-11: 72 files changed in
`panda/board/obj/`, H7 variants grew as expected, F4 variants came out
byte-identical apart from `gitversion.h`/`version` metadata — confirming
the `#ifdef STM32H7` guard works. Regenerated binaries committed (Step
2b) as `0d4605485` and **pushed to `origin`** (this WSL clone's remote
name for `git.waffle`) on 2026-09-11. **Next: Step 3 — reflash the panda
and record a rollback point**, then Step 4 (the real door-unlock
validation test) — both need the actual comma-four hardware.

## Open questions / decisions needed from the user before Fix 2 can start (2026-09-11)

Collected here so a fresh session can see at a glance what's blocking
progress, instead of having to infer it from scattered notes below.

**Resolved (2026-09-11):**

1. **Branch: fresh branch off `wat-boot-update`.** Not continuing directly
   on `wat-boot-update` — Fix 2 gets its own branch (name not yet chosen;
   pick something like `wat-stopmode-wake` when Step 1 actually starts).
   Keeps Fix 2's history separable from Fix 1's for independent
   revert/upstream decisions later.
2. **HKG remote-climate-wake: applies to the user, via the app.** User
   confirmed they use remote start "usually... from app," not the fob.
   Checked the actual commit that added this feature
   (`StarPilot` commit `5234a121f`, "Add HKG EV App Start Climate wake"):
   it specifically watches for the car's telematics/Bluelink **app**
   climate-active frame (CAN bus 1, addr `0x384`, byte 3 nonzero) — tested
   by StarPilot's authors on a Kia EV9 (byte 3 `0x01` when active) and Kia
   EV6 (byte 3 `0x0a` when active), both `0x00` when off/stopped. The
   commit message also explains why it was changed to "wake-only" after an
   earlier version caused partial-car fingerprinting and Dashcam Mode
   issues — consistent with what this file already found (it only feeds
   `started` for bootkick purposes, not `ignitionCan`). **This is directly
   relevant to the user's actual usage (app-based remote start) — Step 5's
   HKG check stays in as a live verification item, not skippable.**
3. **Test iteration speed for Step 4: keep at 1hr.** User declined
   shortening the shutdown timeout for faster iteration — will accept the
   ~1hr wait per real-world test attempt rather than temporarily changing
   the setting.
4. **WSL machine: re-checked and confirmed working (2026-09-11).** Built
   directly in the `starpilot-wat-boot` worktree (this is that worktree) —
   its root-level `.venv` already had `scons`/`arm-none-eabi-gcc`/`uv`
   available, so `panda/setup.sh` wasn't needed (its `sudo apt-get` step
   would have failed non-interactively anyway; turned out to be
   unnecessary since the deps were already present at the root-venv
   level). `panda/.venv` itself doesn't exist and isn't needed — build
   uses the root `.venv`.

**Still open (not yet asked / no explicit answer):**

5. **Rollback point for Fix 2's reflash (Step 3).** Fix 1's Step 3 reflash
   happened without recording a firmware version/rollback point (noted gap
   in that step). Fix 2's plan says to record one this time, given it
   touches MCU-level power state rather than just a GPIO pulse. **Default
   assumed unless the user says otherwise:** rely on the git commit hash of
   the currently-flashed `panda/board/obj/` binaries (already in git
   history, no extra work) — note that commit hash here in Step 3 when it
   happens. Flag if a more robust method (e.g. a physical note, in case
   rollback is ever needed away from git access) is wanted instead.
6. **Push scope for Fix 2's branch.** **Default assumed unless the user
   says otherwise:** same restriction as Fix 1 carries forward — only
   `custom_waffle` authorized, no push to `origin`/`custom_github` without
   a separate explicit go-ahead.

**Not blocking, but worth noting:** Step 1 (source edit only, no
build/flash) can start now — none of the still-open items (5, 6) or the
WSL re-check (4) block it. Only the branch needs creating first (per item
1, resolved above).

**Reopened (2026-09-11) — why Fix 1's Steps 4-6 are in question.**
Previously this file said Fix 1's fix was "confirmed working end-to-end"
based on the unplug/replug-OBDII-cable test (see Fix 1's Steps 4/5 below).
That test is now suspect as a stand-in for the real scenario:

- **New test (2026-09-11):** user set the device shutdown timeout to 1hr,
  locked the car, and walked away for **over an hour** (so the device had
  time to fully shut down per the timeout, then sit powered-off well past
  that). On returning and **unlocking the door**, the comma did **NOT** boot
  immediately — the original target behavior (sunnypilot boots instantly on
  door-unlock, per the "Observed behavior difference" section below) did not
  reproduce on the fixed StarPilot firmware.
- This directly contradicts the earlier conclusion that Fix 1's Steps 4/5
  were done. The unplug/replug-OBDII test confirmed GPIOC11 pulsing wakes
  the SOM when 12V is freshly (re)applied to the OBDII connector — but it
  does **not** confirm that a door-unlock event actually causes 12V to
  appear/toggle on the OBDII bus in the first place. Those are two
  different claims, and only the first was tested.
- Follow-up investigation (see "New root cause found for the door-unlock-
  wake gap" section below) explains *why*: door-unlock wakes the car's
  comfort/body CAN bus, not a fresh OBDII 12V edge — a completely different
  mechanism from what Fix 1 addresses. This is now understood, not an open
  mystery; it's what Fix 2 exists to address. Fix 1's Steps 4-6 stay
  downgraded pending re-validation, but the "why" is no longer unknown.
- Steps 1-3 of Fix 1 (source fix, rebuild, reflash) are still believed done
  as recorded below — nothing calls those into question, only whether
  Steps 4-6's validation actually covered the right scenario.

*(Older status text below, from before build/flash/test happened on the WSL
machine — kept for history, superseded by the paragraphs above.)*

Investigation complete, root cause confirmed. **Step 1 done** — the 2-line
source fix (3 lines incl. one comment) is committed. **Steps 2+ not yet done**
(build/flash/test). This machine (Windows, no ARM toolchain) cannot build panda
firmware, so Step 2 onward is meant to be picked up by a Claude instance running
on a separate WSL machine that has the toolchain. See "Handoff to WSL machine"
below for exactly what to do there.

### WSL machine update (2026-09-09)
Picked up here as planned. Confirmed toolchain present on this machine
(`arm-none-eabi-gcc`, `uv`, `scons`/`SCons` all already available in the main
clone's root `.venv`). Created a worktree at `../starpilot-wat-boot` off the
main WSL clone for the build, rather than building in the main clone directly
— the main clone was already being used for unrelated `Dom_wat_analyzer_tuning`
plotjuggler/tuning work, so it was left on its own branch instead of switching
it over.

**Remote-name note:** on this WSL machine the git.waffle remote referenced
below as `custom_waffle` is simply named `origin` (there is no separate
comma-upstream `origin` or `custom_github` remote configured on this clone at
all). Translate `custom_waffle` → `origin` in the commands below when running
them here.

**Per explicit user instruction, commits are their own checkpoint now — never
bundled with build/other work.** Step 2 below is split into 2a (build) and 2b
(commit); do not run `git commit` for 2b (or anywhere else in this plan)
without asking first, even though the original wording below groups them.

Step 2a and 2b both DONE as of this update: build succeeded (72 files
changed in `panda/board/obj/`, matching the expected H7-only functional
growth + version-embed churn everywhere else), and the regenerated
binaries were committed as `1839c9c72`. Not pushed to `origin` yet.

*(This update was made directly in this WSL-side copy rather than the
Windows-side canonical copy — per the note at the top of this file, reconcile
if they diverge.)*

### Work done so far
- New git repo initialized at the root dir (`C:\Users\pancake\Documents\python\projects\starpilot`,
  no `.git` there before) specifically to track this `.scratch/` file across
  sessions/machines. `.gitignore` excludes `StarPilot/` and `sunnypilot/` (each
  is its own separate git repo already — not submodules, just adjacent dirs).
  Root repo has one commit so far (`114997f`) adding `.gitignore` + this file.
- In the `StarPilot` repo: created branch `wat-boot-update` off
  `Dom_wat_analyzer_tuning`, made the Step 1 edit, committed it as `bd3bec540`
  ("panda: restore GPIOC11 DC_IN ship-mode wake on cuatro bootkick"), and pushed
  the branch to remote `custom_waffle` (`http://git.waffle/waffle/StarPilot.git`
  — the user's personal git instance). Other remotes on this repo (`origin` =
  comma's upstream, `custom_github` = user's GitHub fork) were NOT pushed to.
- Note: at the time of the Step 1 commit, the `StarPilot` working tree also had
  unrelated pending changes (two deleted `frpc_darwin_*` binaries under
  `starpilot/system/galaxy/bin/`, and untracked files under `tools/tuning/` and
  `wat_plan/`) — these were deliberately left uncommitted/unstaged and are
  unrelated to this investigation. Don't assume they need handling here.

### Key discovery affecting the build step
StarPilot does **not** build panda firmware on-device at runtime. It ships
**precompiled, git-committed** `.bin.signed` binaries in `panda/board/obj/`
(that dir is gitignored by default but explicitly un-ignored via
`!board/obj/` / `!board/obj/**` in `panda/.gitignore`, specifically to keep
"prebuilt device images" trackable). `selfdrive/pandad/pandad.py:21-29`
constructs a filename like `panda_h7_hkg_remote.bin.signed` from
hardware/param state and flashes straight from that directory — there is no
on-device scons invocation. **This means the source edit alone does nothing;
the committed binaries in `panda/board/obj/` must be regenerated and
committed too, or the device will keep flashing the old firmware.**

## Repo paths
- StarPilot: `C:\Users\pancake\Documents\python\projects\starpilot\StarPilot`
- sunnypilot: `C:\Users\pancake\Documents\python\projects\starpilot\sunnypilot`
- (Note: `C:\Users\pancake\Documents\python\projects\sunnypilot` is a separate,
  empty directory — not one of the two repos being compared.)
- Car: Hyundai Ioniq 6 (HKG CAN-FD platform). Hardware: comma 4 ("cuatro" board).

## Setup / context

- comma 4 wired to both the standard harness/mirror port **and** a direct OBDII-port
  power tap.
- Reason for the OBDII tap: the Ioniq 6 cuts power at the harness port immediately
  on ignition-off. The OBDII tap keeps the device powered so it can idle offroad
  instead of hard-cutting.
- Recently switched from sunnypilot to StarPilot on this same hardware and noticed
  a boot-timing/wake behavior difference relative to ignition.

## Observed behavior difference

- **sunnypilot**: if the device is fully powered off, it powers on and boots the
  instant 12V appears on the OBDII bus — including when the car merely "wakes up"
  on door-unlock, well before actual ignition.
- **StarPilot**: the device does **not** boot in that scenario. It only boots on
  true ignition.

Goal: make StarPilot behave like sunnypilot (boot on OBDII bus power appearing,
not just true ignition).

## Dead end tried

Enabled StarPilot's `HKGRemoteStartBootsComma` param toggle (Hyundai/Kia-specific
— relevant since the Ioniq 6 is an HKG platform), hoping it would fix this.
**It did not change the behavior.** Investigation below confirms it never could.

## Background: general boot/ignition differences between the forks

Before finding the actual root cause, a broader diff of the two forks' boot paths
turned up several real differences (kept here for reference, though none of these
turned out to be the actual cause of the OBDII-wake issue):

- Different target AGNOS versions: StarPilot `19.6.17` (`StarPilot/launch_env.sh:21`)
  vs sunnypilot `18.4` (`sunnypilot/launch_env.sh:16`).
- StarPilot defers reboot/shutdown while ignition is on
  (`StarPilot/system/manager/manager.py:172-174, 1219-1225`, `should_defer_reboot`);
  sunnypilot acts on `DoReboot`/`DoShutdown`/`DoUninstall` immediately regardless
  of ignition state.
- StarPilot's `launch_chffrplus.sh` does more per-boot work than sunnypilot's
  (SSH-restore-from-backup check, `launch_param_migrations.py`, venv setup) and
  already has built-in boot-timing instrumentation
  (`SP_BOOT_TIMING_LOG` → `/tmp/starpilot_boot_timing.log`).
- StarPilot's `manager_init()` runs a longer chain of one-time param migrations
  plus a full params/params_cache sync loop vs sunnypilot's shorter version.
- sunnypilot has boot-speed toggles (`QuickBootToggle`, `DeviceBootMode`/
  `OffroadMode`) with no StarPilot equivalent.

These are real, but they only affect timing/behavior of an *already-booting*
device — none of them explain the "won't wake from full power-off" issue, which
turned out to be a hardware/firmware-level problem (see below).

## Root cause (confirmed via code)

This is a **panda MCU firmware issue specific to the comma four ("cuatro") board**,
not an openpilot/SOM-side software or param issue.

The panda's STM32 MCU powers up directly off the harness/OBDII 12V rail,
independent of ignition and independent of which fork/software is on the SOM.
On power-up it immediately tries to "bootkick" (wake) the SOM via
`cuatro_set_bootkick()`:

- **sunnypilot** — `sunnypilot/panda/board/boards/cuatro.h:40-43`:
  ```c
  static void cuatro_set_bootkick(BootState state) {
    set_gpio_output(GPIOA, 0, state != BOOT_BOOTKICK);
    // DC_IN rising edge wakes SOM from ship mode
    set_gpio_output(GPIOC, 11, state != BOOT_BOOTKICK);
  }
  ```
  `GPIOC11` is wired to the SOM PMIC's `DC_IN_EN_N`/`DC_IN` detect pin
  (`cuatro_init`, same file, sets `OUTPUT_TYPE_OPEN_DRAIN` on it). Pulsing it
  produces the rising edge that wakes the PMIC out of hardware "ship mode" — a
  true powered-off state, not just a Linux-level offroad idle.

- **StarPilot** — `StarPilot/panda/board/boards/cuatro.h:40-42,53`:
  ```c
  static void cuatro_set_bootkick(BootState state) {
    set_gpio_output(GPIOA, 0, state != BOOT_BOOTKICK);
  }
  ```
  Only `GPIOA0` (the standard soft "power key" bootkick line) is pulsed.
  `GPIOC11` was repurposed in StarPilot's `cuatro_init()` for a different signal
  (`VBAT_EN`, now driven via GPIOC12) and no longer touches the PMIC wake pin at
  all. If the SOM is in true ship mode, pulsing `GPIOA0` alone does nothing.

This is baked into **every firmware variant StarPilot builds** — confirmed via
`StarPilot/panda/SConscript:171-182` and `StarPilot/panda/board/main.c`: none of
`RemoteStartBootsComma` (`-DPANDA_REMOTE_START`), `HKGRemoteStartBootsComma`
(`-DPANDA_HKG_REMOTE_START`), or `IgnoreIgnitionLine`
(`-DPANDA_IGNORE_IGNITION_LINE`) ever touch `cuatro_set_bootkick()`/
`cuatro_init()`. They only affect (a) which CAN transceiver stays powered during
power-save, and (b) how already-booted `pandad` interprets `ignitionLine`/
`ignitionCan` for the onroad/offroad state machine — both require the SOM to
already be running, so none of it can reach a ship-moded SOM. This is exactly why
the toggle produced no change.

Comma-four-specific confirmation: comma three's `tres.h` wires `GPIOC11` to
`I2C5` instead (no DC_IN-wake pin in that board file at all), and older boards
(`dos.h`, `red.h`) have their own simpler `set_bootkick` with no ship-mode-wake
GPIO either.

**Origin confirmed (checked upstream `commaai/panda` on GitHub, master branch)**:
stock `board/boards/cuatro.h` has the same GPIOC11/`DC_IN_EN_N` bootkick code as
sunnypilot. Sunnypilot is not adding a custom feature — it matches stock comma
behavior. **StarPilot lacks this stock code.**

**Why**: StarPilot is downstream of FrogPilot. Checked FrogPilot's `cuatro.h`
(`FrogPilot` branch, github.com/FrogAi/FrogPilot) — it's a notably older/simpler
version of this file: no dedicated `cuatro_set_bootkick()` at all (it calls the
generic `tres_set_bootkick()`), and GPIOC12 is used there for "SOM bootkick +
reset lines," not `VBAT_EN`. No GPIOC11/DC_IN concept exists in that version at
all. This strongly suggests upstream comma rewrote `cuatro.h` at some point —
adding the dedicated `cuatro_set_bootkick()` (GPIOA0) plus the newer GPIOC11
`DC_IN_EN_N` ship-mode wake, freeing up GPIOC12 in the process — *after*
FrogPilot's snapshot of this file. Sunnypilot rebases closely against current
upstream and picked up the rewrite intact. StarPilot, descending from FrogPilot's
older version, appears to have written its own `cuatro_init()`/
`cuatro_set_bootkick()` (adding its own GPIOC12 `VBAT_EN` label) without ever
merging forward the specific upstream commit that added GPIOC11/DC_IN — so this
looks like a missed upstream merge, not a deliberate removal.

Can't fully confirm at the commit level (all three repos have squashed history
for this file), but the structural evidence is strong. Practically this doesn't
change the fix, but it does mean the gap could resurface on a future StarPilot
rebase/merge from its lineage unless flagged — worth keeping the restored lines
clearly commented (they already are, via the "DC_IN rising edge wakes SOM from
ship mode" comment) so they're less likely to get silently dropped again.

Ruled out: AGNOS/systemd shutdown behavior is identical in both forks (both call
`sudo poweroff` — `StarPilot/system/hardware/tici/hardware.py:320-321` /
`sunnypilot/system/hardware/tici/hardware.py:248-249` — a real `poweroff.target`,
not a suspend), so this isn't an OS-level suspend-vs-poweroff difference.

**Unconfirmed / flagged**: the exact non-bootkick path by which true ignition
*does* wake the device on StarPilot wasn't identified in software — likely an
analog/hardware ignition-sense path upstream of the bootkick GPIOs.

## Blast radius of the fix (checked)

Grepped StarPilot's `cuatro.h` for every `GPIOC` reference — it configures pins
12 (`VBAT_EN`), 5, 2, 8, 0, but **never touches GPIOC11 anywhere**. The earlier
claim that GPIOC11 was "repurposed" was imprecise: it's simply left unconfigured
(hardware reset default), not driving some other StarPilot feature under a
different name. `VBAT_EN` is a separate, StarPilot-only addition on GPIOC12 that
sunnypilot doesn't have.

Repo-wide grep confirms GPIOC11 is used nowhere else in StarPilot's panda board
code except `tres.h` (comma three board, `GPIO_AF4_I2C5` — a different physical
board, not applicable to cuatro/comma four).

So restoring the GPIOC11/`DC_IN_EN_N` config + bootkick pulse should not collide
with anything else StarPilot does on this board — it's re-adding a dropped pin
config, not overloading a pin already in use. Residual risk: cannot verify from
software alone that GPIOC11 isn't wired to something unexpected on the physical
comma-four PCB (would need the schematic) — but since sunnypilot ships this exact
same physical board actively driving this pin this way, that risk is low.

## Fix — discrete steps (Fix 1: GPIOC11/DC_IN bootkick)

Each step below is a standalone checkpoint. Stop after each one; do not proceed
to the next until explicitly told to. (This is Fix 1's step sequence — Fix 2
has its own separate Step 1-6 further down, under "Fix — discrete steps
(Stop-mode / CAN+SBU wake port)". Don't confuse the two.)

**Checked in advance (already done, informs Step 1):** side-by-side diff against
sunnypilot's `cuatro.h` confirms GPIOC11 is untouched anywhere else in
StarPilot's file (repo-wide grep), and `VBAT_EN`/GPIOC12 is a separate
StarPilot-only addition on a different pin, not a renamed version of GPIOC11 —
so the edit below is a clean 2-line insertion with no conflict. Also confirmed
`bootkick_tick()`/`bootkick.h` is board-agnostic (calls
`current_board->set_bootkick()` via function pointer) and none of StarPilot's
custom ignition params (`RemoteStartBootsComma`, `HKGRemoteStartBootsComma`,
`IgnoreIgnitionLine`) touch `cuatro_set_bootkick()`/`cuatro_init()` — so this
change is isolated to the physical GPIOC11 pulse only.

- **Step 1 — DONE (commit `bd3bec540` on branch `wat-boot-update`, pushed to
  `custom_waffle`).** Edited `StarPilot/panda/board/boards/cuatro.h`:
  ```c
  // cuatro_set_bootkick (was lines 40-42):
  static void cuatro_set_bootkick(BootState state) {
    set_gpio_output(GPIOA, 0, state != BOOT_BOOTKICK);
    // DC_IN rising edge wakes SOM from ship mode
    set_gpio_output(GPIOC, 11, state != BOOT_BOOTKICK);   // added back
  }

  // cuatro_init (was lines 50-53):
  set_gpio_output_type(GPIOD, 3, OUTPUT_TYPE_OPEN_DRAIN);  // FAN_EN
  set_gpio_output_type(GPIOC, 11, OUTPUT_TYPE_OPEN_DRAIN); // DC_IN_EN_N — added back
  set_gpio_output_type(GPIOC, 12, OUTPUT_TYPE_OPEN_DRAIN); // VBAT_EN (unchanged)
  ```
  Source edit only, no build/flash performed on this (Windows) machine.

- **Step 2a — Build. DONE (built in the `../starpilot-wat-boot` worktree on
  this WSL machine).** See "Handoff to WSL machine" section below for exact
  commands used.
  Must rebuild via `scons` (not hand-pick individual variants) since
  `cuatro.h` is shared across all H7-target firmware builds and there's no
  compile-time board selection — `panda_h7`, `panda_h7_remote`,
  `panda_h7_hkg_remote`, and their `*_can_ignition_only` counterparts (6 H7
  variants total, per `panda/SConscript:171-190`) all need regenerating. F4
  variants (`panda`, `panda_remote`, etc.) don't include `cuatro.h` and
  shouldn't change, but the full `scons` build regenerates everything anyway so
  there's no need to filter.
  **Critical:** StarPilot ships precompiled `.bin.signed` binaries committed
  in `panda/board/obj/` (see "Key discovery" above) — `pandad.py` flashes
  straight from there, it does not build on-device. So this step isn't done
  until the regenerated binaries are committed back into `panda/board/obj/`
  (Step 2b), not just built locally. *Checkpoint: confirm the build succeeds,
  review `git status`/`git diff --stat` in `panda/board/obj/` (expect all
  H7-family `.bin.signed` + bootstub files to differ, plus
  `gitversion.h`/`version` since those embed a git-rev string).*

- **Step 2b — Commit the regenerated binaries. DONE — commit `1839c9c72`
  on `wat-boot-update` ("panda: regenerate firmware for cuatro GPIOC11 DC_IN
  bootkick fix"), committed only after explicit user permission per the
  updated instruction above. Not pushed to `origin` yet — push needs its own
  explicit go-ahead too.**

- **Step 3 — Reflash the panda. DONE.** Happened outside the tracking in this
  file — no record here of the firmware version/rollback point that was
  captured beforehand, or exactly when/how the flash was performed. Confirmed
  done only via the successful Step 4/5 test result below.

- **Steps 4/5 — Bench + in-car test. NEEDS RE-VALIDATION (was marked DONE,
  downgraded 2026-09-11).** The original plan was a bench test (12V applied
  directly to the OBDII tap, no car) plus an in-car test (let the car sleep,
  then unlock the door). What actually happened instead: get in the car,
  unplug the OBDII cable, then plug it back in — the comma booted
  immediately, with no car ignition involved. That confirms the GPIOC11 fix
  wakes the SOM when 12V is freshly reapplied to the connector, but it
  substitutes a manual power cycle for the actual target trigger
  (door-unlock). **2026-09-11: the real in-car door-unlock test was finally
  run (shutdown timeout 1hr, car locked, walked away >1hr so device fully
  shut down, then unlocked the door) and the comma did NOT boot
  immediately** — the original target scenario failed. Fix is not yet
  validated for the actual goal; only the narrower unplug/replug claim
  stands. See Status above for open questions before deciding next step.

- **Step 6 — Regression check. NEEDS RE-VALIDATION (was marked DONE,
  downgraded 2026-09-11).** Previously recorded as confirmed (onroad/offroad
  transitions on true ignition still normal, device still respects its
  shutdown timeout even with OBD power present past it) — that part isn't
  contradicted by the new test, but it was validated using the same
  unplug/replug session as Steps 4/5, not independently, so treat as
  unconfirmed pending the door-unlock question being resolved.

- **Step 7 — Report upstream. DEFERRED.** Since this looks like a missed-merge
  gap in StarPilot's FrogPilot-descended lineage rather than a deliberate
  change, it will likely get silently reintroduced on a future StarPilot
  update/rebase unless flagged. File an issue or PR against StarPilot with
  this finding (root cause + the 2-line fix) so it survives future updates
  instead of only living on this local build. Explicitly deferred for now —
  not being worked on until the user picks it back up.

## New root cause found for the door-unlock-wake gap (2026-09-11)

Follow-up on user's clarifying answers to the reopened test above:
- Checked immediately (waited 1-2 min before touching anything) — no boot.
  On sunnypilot, by contrast, it would already be booting before the user
  could even get in the car.
- After waiting, pressing the physical ignition button did trigger a boot
  normally — true ignition still works fine, consistent with everything
  else in this file.
- Confirmed still running the fixed firmware (checked live panda signature).

This rules out "wrong firmware" and "just needed longer" and confirms the
door-unlock-wake gap is real and distinct from the GPIOC11/bootkick issue
already fixed. Traced it to a **second, separate mechanism** — found by
diffing each fork's `main.c` power-save loop:

- **sunnypilot** (`panda/board/main.c:379-383` + `panda/board/sys/power_saving.h`):
  when power-save is enabled and the cuatro board's SOM-power GPIO reads
  "off," the panda's own STM32 MCU calls `enter_stop_mode()` — a real
  hardware STOP mode (SLEEPDEEP, clocks/ADCs/SRAM retention disabled). Before
  sleeping it explicitly reconfigures the SBU1/SBU2 harness pins and all
  three FDCAN RX pins as EXTI wake sources (rising+falling edge), then wakes
  via `NVIC_SystemReset()` on any of: SBU1/SBU2 toggling, or CAN RX activity
  on any bus. **This is almost certainly the actual door-unlock trigger**:
  unlocking the Ioniq 6 wakes its comfort/body CAN bus, which the panda MCU
  is listening for even while the SOM itself is fully off — no fresh 12V
  edge on OBDII needed at all. This reframes the earlier "12V edge" framing
  as incomplete: sunnypilot has *two* independent wake paths (DC_IN edge via
  GPIOC11, and CAN/SBU activity via stop-mode EXTI), and the door-unlock
  scenario the user actually cares about is driven by the second one, not
  the first.
- **StarPilot** (`panda/board/main.c:374-403` + `panda/board/power_saving.h`):
  has no `enter_stop_mode()` anywhere in the codebase (repo-wide grep is
  empty). Its power-save loop only ever does a light `__WFI()` — no EXTI
  reconfiguration, no SLEEPDEEP, no CAN/SBU wake path. So even though
  CAN traffic and SBU activity occur on door-unlock exactly as they do on
  sunnypilot's hardware, nothing in StarPilot's firmware is listening for
  them while the SOM is off. This fully explains the observed gap
  independent of the already-fixed GPIOC11 issue.

**Important complication — this looks like an intentional divergence, not
a missed merge (unlike GPIOC11).** StarPilot's `power_saving.h` carries its
own explicit warning:
> "WARNING: To stay in compliance with the SIL2 rules laid out in STM
> UM1840, we should never implement any of the available hardware low
> power modes. See rule: CoU_3"

sunnypilot's `power_saving.h` has the *same* rule citation (different STM
manual number, UM2331) but reaches the opposite conclusion:
> "Low power state 'stop mode' is only entered from SAFETY_SILENT when no
> safety function is active and exited via reset which is a safe state."

So sunnypilot's position is that entering stop mode is compliant specifically
*because* it only happens from `SAFETY_SILENT` (asserted via
`assert_fatal(current_safety_mode == SAFETY_SILENT, ...)` right before the
call) and always exits through a full MCU reset (a known-safe state), while
StarPilot's comment reads as a blanket "never do this." Whether that's a
deliberate, considered safety call by StarPilot's maintainers or just an
older/more conservative boilerplate comment inherited from further upstream
isn't known — but unlike the GPIOC11 gap, **this isn't obviously a bug to
just port over**. It's a real functional-safety tradeoff spelled out in
StarPilot's own source comments.

**Decision (2026-09-11): user wants to proceed.** After the plain-language
explanation of STM32 low-power modes and the IEC 60730 "Conditions of Use"
framing, the user's read is that this is a difference in philosophy
(inherited/conservative comment vs. a deliberately-scoped exception), not a
hard safety blocker. Decision: **port sunnypilot's CAN/SBU stop-mode wake to
StarPilot**, using the same `SAFETY_SILENT`-gated, reset-to-wake design
sunnypilot uses. This is a new fix, separate from the already-landed
GPIOC11/DC_IN bootkick fix (see "Fix — discrete steps" above, which stays
as-is/unaffected).

**Correction after independent review (2026-09-11): "boilerplate, not
deliberate" is not actually established.** `git log --follow` on
`StarPilot/panda/board/power_saving.h` shows only 2 commits ever — the
squashed `openpilot v0.10.3 release` import and an unrelated "Add HKG EV App
Start Climate wake" commit — neither touches or discusses the SIL2/CoU_3
comment. There is no commit-level evidence either way about whether a
StarPilot maintainer deliberately reasoned about this. The manual-number
mismatch (UM1840 vs. sunnypilot's UM2331) and the missing justification
sentence are suggestive that sunnypilot's authors edited this comment when
they added their exception and StarPilot's was never revisited, but that's
circumstantial, not proof. **Reframing the basis for proceeding:**
provenance of the comment is undetermined; the actual justification is the
independently-verified fact (see Feasibility check below) that StarPilot's
own `SAFETY_SILENT`-before-power-save-enable invariant genuinely holds in
its control flow — not a claim that the original warning was empty
boilerplate.

### Feasibility check (done, source-only, 2026-09-11)

Confirmed every building block `enter_stop_mode()` needs already exists in
StarPilot, mostly byte-identical to sunnypilot:

- `SAFETY_SILENT` + `assert_fatal(current_safety_mode == SAFETY_SILENT, ...)`
  precondition pattern — StarPilot already transitions to `SAFETY_SILENT` and
  enables power-save at the identical trigger point (heartbeat-loss handling
  in `main.c`, StarPilot line ~249 vs. sunnypilot line ~232) — structurally
  the same state machine, not just a look-alike name.
- `harness_config->GPIO_SBU1/2` + `pin_SBU1/2` for the cuatro board — checked
  `StarPilot/panda/board/boards/cuatro.h:104-118` vs.
  `sunnypilot/panda/board/boards/cuatro.h:103-117` — **identical values**
  (`GPIOC`/pin 4 for SBU1, `GPIOA`/pin 1 for SBU2, same relay pins, same ADC
  channel assignments).
- Board function-pointer table (`board_declarations.h`) has matching
  signatures for `set_bootkick(BootState)`, `read_som_gpio()`,
  `set_amp_enabled(bool)`, `enable_can_transceiver(uint8_t, bool)` — same as
  sunnypilot.
- `BootState` enum (`BOOT_STANDBY`/`BOOT_BOOTKICK`/`BOOT_RESET`) — identical.
- `register_set`/`register_set_bits`/`register_clear_bits` helpers in
  `drivers/registers.h` — identical signatures in both forks.
- Same STM32H7 chip headers (`stm32h725xx.h`/`stm32h735xx.h`) — so all the
  register/bit names `enter_stop_mode()` touches (`PWR_CPUCR_PDDS_D1/D2/D3`,
  `PWR_CR1_SVOS_0`, `PWR_CR1_FLPS`, `RCC_AHB2LPENR_SRAM1LPEN`, etc.) exist
  unchanged.
- `harness_check_ignition()` — present, same as sunnypilot's use of it inside
  `enter_stop_mode()` (resets immediately if ignition came on right before
  sleeping, rather than going to sleep at all).

**Net: this is a small, mechanical port** — copy sunnypilot's
`enter_stop_mode()` body into StarPilot's `power_saving.h`, plus one gated
call site in `main.c`'s power-save loop branch. Not a from-scratch
low-power-mode design.

### Two things that need adapting, not copy-pasting verbatim

1. **Naming: `power_save_status` (int) vs. `power_save_enabled` (bool).**
   StarPilot's `power_saving.h` uses
   `POWER_SAVE_STATUS_ENABLED`/`POWER_SAVE_STATUS_DISABLED` ints, not a plain
   bool. The stop-mode gate needs
   `power_save_status == POWER_SAVE_STATUS_ENABLED`, not sunnypilot's
   `power_save_enabled`. Cosmetic, not a logic change.
2. **StarPilot's HKG-remote-start CAN exception.** StarPilot's
   `set_power_save_state()` has a `#ifdef PANDA_HKG_REMOTE_START` branch that
   keeps a second CAN transceiver (`hkg_bus`) enabled during the *shallow*
   power-save state — presumably feeding the HKG remote-climate-wake feature
   from the earlier boot investigation
   (`hkg_remote_climate_wake`/`PANDA_HKG_REMOTE_START` in `main.c`).
   sunnypilot has no equivalent feature, so it has nothing to conflict with.

   **Pressure-tested by independent review (2026-09-11), then resolved
   further by the user (2026-09-11): the general wake mechanism supersedes
   the specific one, it doesn't "interact with" it.** The HKG carve-out
   only matters while `read_som_gpio()` still reads true (SOM idling
   offroad but not yet shipped out) — the new stop-mode gate only fires
   once the SOM is fully off, so nothing changes for HKG remote-start
   *until* that point; today's shallow-power-save behavior is untouched.

   Once the SOM is fully off, tracing the actual mechanism (not just
   assuming "decode should still work after reset") shows there's no real
   question to test:
   - `hkg_remote_climate_wake` is set by `ignition_can_hook()`
     (`StarPilot/panda/board/drivers/can_common.h:172-176`, matching
     `msg->bus == 1U && msg->addr == 0x384U`) — a **software** decode that
     only runs while the MCU's main loop is already executing and
     processing received frames. It was never a hardware wake source; it's
     a content check that requires the CPU to already be awake. Once
     `enter_stop_mode()` actually stops that main loop, this decode simply
     can't run — there's no "does the decode survive" question, because it
     doesn't execute during that window at all, by construction.
   - It doesn't need to. `bootkick_tick()`
     (`StarPilot/panda/board/drivers/bootkick.h:12,16-23`) has `boot_state`
     default to `BOOT_BOOTKICK` and nothing moves it away from that default
     on a fresh boot until either a real heartbeat resumes or an
     ignition/harness/wake edge fires — so on the very first tick after
     **any** MCU reset, cold power-on or a stop-mode-wake-triggered reset
     alike, `set_bootkick(BOOT_BOOTKICK)` fires unconditionally, with no
     check of *why* the reset happened. The reset itself is the trigger.
   - Net effect: once genuinely asleep, any activity on the HKG bus (one of
     the three physical FDCAN RX lines armed as EXTI wake sources) causes a
     reset, and the reset alone causes an unconditional bootkick — the
     specific "was this really a remote-start command" gate never gets a
     chance to matter, because the coarse mechanism already decided to boot
     before that check would run. This isn't a risk of breaking the
     feature; the general mechanism fully absorbs its job during the
     deep-sleep window. (It's also just the sunnypilot behavior being
     asked for: boot on any relevant activity, no selectivity.)

   **Practical effect on Step 5:** no separate "does HKG remote-start
   survive" test is needed. Just confirm the SOM wakes reliably on HKG-bus
   activity while fully off — the same generic door-unlock-class test
   already planned, not a distinct case.

### Blocking defect found by independent review (2026-09-11) — fixed in the plan below

The original feasibility check validated everything by diffing against
sunnypilot and missed that **the two forks' build matrices have diverged,
not just this one function**. Sunnypilot has dropped F4 support entirely —
zero F4 build targets in its `SConscript`, no F4 board files. StarPilot
still builds ~12 F4 firmware variants (`panda`, `panda_remote`,
`panda_hkg_remote`, `panda_can_ignition_only`, etc. — see
`StarPilot/panda/SConscript:170-190`) from the **same** `board/main.c` /
`power_saving.h` this fix touches.

`enter_stop_mode()` as written uses STM32H7-only registers/bitfields that
don't exist on F4 at all — confirmed directly against the CMSIS headers:
`PWR_CPUCR_PDDS_D1`, `PWR_CR1_SVOS_0`/`PWR_CR1_FLPS`,
`RCC_AHB2LPENR_SRAM1LPEN`, `RCC_AHB4LPENR_SRAM4LPEN`, `ADC_CR_DEEPPWD` all
have zero matches in `stm32f413xx.h` (they're H7's D1/D2/D3 power-domain
architecture, which F4 doesn't have). The planned `hw_type == HW_TYPE_CUATRO`
check is a **runtime** guard — it does nothing to stop the code from being
**compiled** into F4 firmware. As originally planned, this port would fail
to build every F4 variant. **This is now folded into Step 1 below as a
required `#ifdef STM32H7` guard, not just a flag.**

### Residual risk that can't be resolved from source alone

The CAN-wake mechanism depends on the physical CAN transceiver ICs still
passing bus activity through to the RX pin while software has "disabled"
them (a standby/silent mode many automotive CAN transceivers support for
exactly this wake-on-activity purpose) — that's a hardware behavior, not
verifiable by reading code. Since this is the *same physical comma-four
board* sunnypilot ships this feature on, it should transfer, but only an
actual bench/in-car test can confirm it works the same way once StarPilot's
firmware is doing the disabling.

## Fix — discrete steps (Fix 2: Stop-mode / CAN+SBU wake port)

Separate fix from Fix 1 (GPIOC11 bootkick) above, with its own Step 1-6 —
same checkpoint discipline applies (stop after each step; commits are their
own checkpoint, never bundled with build/other work, never committed
without asking first; no push to `origin`/`custom_github` without explicit
per-push authorization, only `custom_waffle` pre-authorized so far). **This
is the plan that's next up — see Status at the top of this file.**

- **Step 1 — DONE (commit `9a6fbe937` on branch `wat-can-sbu-wake`, off
  `wat-boot-update`, not pushed).** Source edit (this machine, no
  build/flash). In
  `StarPilot/panda/board/power_saving.h`: port sunnypilot's `enter_stop_mode()`
  function body (from `sunnypilot/panda/board/sys/power_saving.h`) in,
  adjusting the `power_save_status`/`power_save_enabled` naming difference
  noted above, **and wrapping the function definition (and its declaration
  in `power_saving_declarations.h`) in `#ifdef STM32H7` — required, not
  optional, per the blocking F4 build-break finding above.** In
  `StarPilot/panda/board/main.c`'s power-save loop branch (the
  `else { __WFI(); ... }` block, current lines ~401-403), add the
  sunnypilot-equivalent gate before the `__WFI()`, also guarded:
  ```c
  #ifdef STM32H7
  if ((hw_type == HW_TYPE_CUATRO) && !current_board->read_som_gpio()) {
    assert_fatal(current_safety_mode == SAFETY_SILENT, "Error: Entering low power mode while not in SAFETY_SILENT. Hanging\n");
    enter_stop_mode();
    assert_fatal(false, "Error: enter_stop_mode returned after system reset. Hanging\n");
  }
  #endif
  __WFI();
  SCB->SCR &= ~SCB_SCR_SLEEPDEEP_Msk;
  ```
  **Branch: DONE — `wat-can-sbu-wake`, created off `wat-boot-update`
  (2026-09-11)**, not continuing directly on it.
  Leave the `#ifdef PANDA_HKG_REMOTE_START` transceiver-keep-alive logic
  alone for now (Step 1 is source-only; the interaction with
  `enter_stop_mode()`'s blanket transceiver-disable gets validated in Step 4,
  not designed around speculatively here).

- **Step 2a/2b — Build + commit regenerated firmware (WSL machine). DONE
  (2026-09-11).** Built in this `starpilot-wat-boot` worktree using its
  existing root-level `.venv` (`scons`/`arm-none-eabi-gcc` already present
  — no `setup.sh` run needed). `scons -j$(nproc)` rebuilt all variants —
  72 files changed in `panda/board/obj/` (same count as the GPIOC11 fix).
  **F4 targets verified**: `panda`, `panda_remote`, `panda_hkg_remote`,
  `panda_can_ignition_only`, `panda_remote_can_ignition_only`,
  `panda_hkg_remote_can_ignition_only` all compiled and came out
  byte-identical to their pre-build versions (aside from
  `gitversion.h`/`version` metadata) — confirms the `#ifdef STM32H7` guard
  from Step 1 actually kept them unaffected, not just assumed. H7 variants
  (`panda_h7`, `panda_h7_remote`, `panda_h7_hkg_remote`, and their
  `*_can_ignition_only` counterparts) all grew in size as expected from the
  new `enter_stop_mode()` code. `panda_jungle_h7`/`body_h7` also rebuilt
  clean (not in the original variant list, included incidentally by the
  full `scons` build, unaffected since their board files don't touch
  `cuatro_set_bootkick`). Committed as `0d4605485`
  ("panda: regenerate firmware for cuatro CAN/SBU stop-mode wake port")
  after explicit go-ahead, and **pushed to `origin`** (`git.waffle` on this
  WSL clone) after a separate explicit go-ahead.

- **Step 3 — Reflash.** Record firmware version/rollback point this time
  (unlike the GPIOC11 fix, where this wasn't captured) — worth being able to
  cleanly roll back given this touches MCU-level power state, not just a
  GPIO pulse.

- **Step 4 — Validate the actual target scenario.** With the car parked,
  ignition off, and enough time elapsed for the device to fully shut down
  (SOM off) — **unlock the door and confirm the comma boots immediately**,
  the same test that just failed pre-fix. This is the real acceptance
  criterion, not the earlier unplug/replug-OBDII substitute.

- **Step 5 — Regression check.** Re-run the GPIOC11 fix's Step 6 checks
  (true ignition on/off transitions unaffected, device still respects its
  shutdown-timeout setting) **plus, specific to this fix**:
  - If the user uses the HKG remote-climate-wake feature
    (`HKGRemoteStartBootsComma`/`PANDA_HKG_REMOTE_START`), no separate test
    is needed — traced the mechanism (see Feasibility check above) and the
    coarse CAN/SBU wake fully absorbs this feature's job once the SOM is
    fully off (any reset unconditionally bootkicks, regardless of message
    content). Just confirm the SOM wakes reliably on HKG-bus activity while
    fully off, same as any other bus — the generic door-unlock-class test
    already covers it. Expect a full device power-cycle per wake versus
    today's shallow-power-save real-time response; that's expected, not a
    regression.
  - **Watch `pandad`/manager logs on the SOM across a stop-mode-triggered
    wake cycle** (added per independent review — the original plan only
    checked hardware-level bootkick behavior). Every stop-mode wake is a
    cold MCU reset, which presents to the SOM as a fresh USB
    device-enumeration event mid-session-boundary, not a continuous
    connection — confirm `pandad.py`/`manager.py` handle this cleanly
    (no error spam, no stuck state) rather than assuming it's transparent.
  - **Watch for multiple/rapid MCU resets during the door-unlock test
    specifically** (added per independent review) — body-CAN chatter can
    linger for several seconds after an unlock event, so if the heartbeat-
    loss timer re-fires before the SOM finishes booting from the first
    wake, the MCU could re-enter stop mode and get woken again, causing more
    than one reset per unlock. This risk is inherited from sunnypilot's
    identical design (not introduced by the port), but StarPilot hasn't
    exercised this pattern before, so it's worth watching for on the first
    real test rather than assuming it's fine.
  - Confirm 12V/battery draw while "asleep" is not worse than before (Stop
    mode should reduce draw relative to plain `__WFI()`, but worth a sanity
    check rather than assuming).
  - Confirm the device still boots normally on true ignition (a different
    wake path than either bootkick fix, shouldn't be touched, but cheap to
    re-check).

- **Step 6 — Decide on upstreaming.** Same open question as the GPIOC11
  fix's deferred Step 7 — whether/when to report this to StarPilot upstream.
  Not started; revisit once Steps 1-5 are done.

## Handoff to WSL machine (for Fix 1's Step 2 — historical, kept for reference)

This machine is Windows with no ARM toolchain, so panda firmware can't be
built here. Plan: transfer this file to the WSL machine, run Claude there, and
have it pick up at Step 2. That Claude instance should be self-sufficient from
this file alone (see the cold-start note at the top) — no need to re-derive
the investigation.

**This was Fix 1's handoff, kept for reference — a manual file transfer was
needed because the branch itself didn't carry the plan doc.** For Fix 2, the
file is now committed directly into the `StarPilot` repo (see next section),
so no manual transfer step is needed this time.

**The WSL machine already has a StarPilot clone from `git.waffle`** (separate
from this Windows machine's clone). Use a git worktree off that existing
clone rather than a fresh clone — isolates the build from whatever branch/work
is active in the main clone dir, while sharing its remote config/credentials
(no separate auth setup needed, since it's the same `.git`).

**Before building, on the WSL machine (from the existing StarPilot clone):**
```bash
git fetch custom_waffle wat-boot-update
git worktree add ../starpilot-wat-boot wat-boot-update
cd ../starpilot-wat-boot
```
Confirm the diff in `panda/board/boards/cuatro.h` there matches what's shown
in Step 1 above (sanity check the right commit came through) before building.

Note: no submodules to init — confirmed no `.gitmodules`; `opendbc` comes in
via `uv sync` as a pip package. `panda/pyproject.toml` pins
`requires-python = ">=3.11,<3.13"`, but `uv` manages its own interpreter, so
the machine's system Python version doesn't need to already match that.

**Build (per `panda/setup.sh` + `panda/test.sh`), inside the worktree:**
```bash
cd panda
./setup.sh          # installs arm-none-eabi-gcc, uv, syncs venv (Linux path in setup.sh uses apt)
source .venv/bin/activate   # if not already active after setup.sh
scons -j$(nproc)    # rebuilds ALL variants — see Step 2 above for why this matters
```
No need to run `test.sh`'s lint/pytest steps unless useful as an extra sanity
check — they're not required for this fix.

**After building (still inside the worktree):**
```bash
git status                              # expect changes under panda/board/obj/
git diff --stat panda/board/obj/        # sanity check which variants changed
git add panda/board/obj/
git commit -m "..."                     # describe as regenerated firmware for the cuatro.h fix
git push custom_waffle wat-boot-update
```
The worktree can be removed afterward with `git worktree remove
../starpilot-wat-boot` from the main clone once the branch is pushed and no
longer needed locally — not required, just cleanup.

**Then continue with Steps 3-7 above** (reflash, bench test, in-car test,
regression check, report upstream) — those need the actual comma-four
hardware, so confirm with the user whether that's available on/near the WSL
machine or needs to come back to this session.

**Do not** push `wat-boot-update` to `origin` (comma's upstream) or
`custom_github` at any point in this process unless explicitly asked — only
`custom_waffle` has been authorized so far.

## Handoff to WSL machine (for Fix 2's Step 2a — current)

**This plan file is now committed inside the `StarPilot` repo itself**, at
`wat_plan/boot-investigation.md` on branch `wat-can-sbu-wake` — a copy of
this exact file as of Step 1 being done. No manual transfer needed: fetching
the branch brings the plan with it. (This root-repo copy under `.scratch/`
remains the canonical one going forward — if the two diverge, e.g. because a
later status update only lands in one place, reconcile using this one.)

**On the WSL machine, from the existing StarPilot clone (`git.waffle`):**
```bash
git fetch custom_waffle wat-can-sbu-wake
git worktree add ../starpilot-wat-can-sbu-wake wat-can-sbu-wake
cd ../starpilot-wat-can-sbu-wake
cat wat_plan/boot-investigation.md   # confirms the plan traveled with the branch
```
Confirm the diff in `panda/board/power_saving.h` and `panda/board/main.c`
there matches Step 1 above (commit `9a6fbe937`) before building.

**Build (same flow as Fix 1, see the historical section above for the
`setup.sh`/`scons` commands) — except this time explicitly confirm the F4
variants (`panda`, `panda_remote`, `panda_hkg_remote`,
`panda_can_ignition_only`, etc.) still compile cleanly**, since Step 1's
`#ifdef STM32H7` guard is what's supposed to keep them building unaffected —
per Step 2a's instructions above, don't just assume the guard worked.

**After building:**
```bash
git status                                      # expect changes under panda/board/obj/
git diff --stat panda/board/obj/                # sanity check H7 AND F4 variants both present/unbroken
git add panda/board/obj/
git commit -m "..."                             # do NOT run this without asking first — commits are their own checkpoint
git push custom_waffle wat-can-sbu-wake         # only after separate explicit go-ahead
```

**Then continue with Steps 3-6 above** (reflash w/ rollback point recorded,
door-unlock validation, regression checks, upstreaming decision) — those need
the actual comma-four hardware.

**Do not** push `wat-can-sbu-wake` to `origin` (comma's upstream) or
`custom_github` at any point unless explicitly asked — only `custom_waffle`
is pre-authorized, and even that push still needs its own go-ahead each time.
