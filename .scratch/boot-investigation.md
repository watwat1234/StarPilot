# StarPilot doesn't wake on OBDII power (only true ignition) — investigation

*Cold-start note: this file is self-contained. If context was reset, just read
this file top to bottom — no need to also find the `/plan` mode plan file.*

*This copy lives inside the `StarPilot` repo (`.scratch/boot-investigation.md`
on branch `wat-boot-update`) so it travels with the branch across machines.
The canonical/master copy is at
`C:\Users\pancake\Documents\python\projects\starpilot\.scratch\boot-investigation.md`
on the Windows machine, in a separate root-level git repo that also tracks
this file — that's where investigation updates should be made and then
re-copied here if they diverge.*

## Status
**Fix confirmed working end-to-end.** Steps 1-2b done (source fix, firmware
rebuilt and committed as `1839c9c72`, not yet pushed to `origin`). Step 3
(reflash) has also happened (undocumented in this file as it happened — details
like firmware version/rollback point were not recorded here). Steps 4/5
(bench + in-car validation) done via a simpler real-world test than originally
planned: with the car off, unplugging and re-plugging the OBDII cable made the
comma boot immediately, with no car ignition involved — confirming the
GPIOC11/DC_IN bootkick fix works. Remaining: Step 6 (regression check on normal
ignition-on/off flows) and Step 7 (report upstream to StarPilot) are still
open. See "Fix — discrete steps" below for the full breakdown.

*(Older status text below, from before build/flash/test happened on the WSL
machine — kept for history, superseded by the paragraph above.)*

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

## Fix — discrete steps

Each step below is a standalone checkpoint. Stop after each one; do not proceed
to the next until explicitly told to.

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

- **Steps 4/5 — Bench + in-car test. DONE, via a simpler real-world test than
  originally planned.** Rather than a separate bench test (12V applied
  directly to the OBDII tap, no car) and in-car test (let the car sleep, then
  unlock the door), the actual test that confirmed the fix was: get in the
  car, unplug the OBDII cable, then plug it back in — the comma booted
  immediately, with no car ignition involved. This is a real-world version of
  the OBDII-power-appearing trigger the bench test was meant to isolate, done
  directly in the car. **Fix validated.**

- **Step 6 — Regression check.** Confirm true ignition-on/off flows (drive
  start/stop, and normal offroad shutdown after `DELAY_SHUTDOWN_TIME_S`) still
  behave normally after the firmware change — no regression to the
  onroad/offroad state machine, reboot-deferral behavior, etc. from the
  general boot-timing differences noted above.

- **Step 7 — Report upstream.** Since this looks like a missed-merge gap in
  StarPilot's FrogPilot-descended lineage rather than a deliberate change, it
  will likely get silently reintroduced on a future StarPilot update/rebase
  unless flagged. File an issue or PR against StarPilot with this finding
  (root cause + the 2-line fix) so it survives future updates instead of only
  living on this local build.

## Handoff to WSL machine (for Step 2)

This machine is Windows with no ARM toolchain, so panda firmware can't be
built here. Plan: transfer this file to the WSL machine, run Claude there, and
have it pick up at Step 2. That Claude instance should be self-sufficient from
this file alone (see the cold-start note at the top) — no need to re-derive
the investigation.

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
