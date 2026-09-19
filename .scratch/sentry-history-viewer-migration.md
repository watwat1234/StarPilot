# Make the scrubber the default Sentry history viewer

Progress tracker. Tick items as they land and add a line to the log at the bottom.

## Context
The "old history viewer" is the paginated "View history" card list in the mobile Sentry view
(`starpilot/system/the_galaxy/assets/mobile/js/views/Sentry.js`, ~614 lines). The scrubber
(`components/SentryScrubber.js`) is opt-in: the user opens history, then presses "Open viewer" / "View range".
`.scratch/sentry-timelapse.md` already lists this removal as next-step #2 and says to do it as a
separate, revertable commit. The scrubber has been tested on a real device (mobile and desktop browsers) and works well.

Scope: mobile only. The desktop UI (`assets/components/tools/sentry.js`) is left alone. Its route,
sidebar entry and CSS stay, and it keeps using `limit`/`offset`.

Commit rules: one separate, revertable commit. Ask before committing.

## Tasks

### 1. Repoint state that reads the old list (do first)
- [x] `historyTotal` (Delete all / Delete matching visibility and confirm text in `deleteAllHistory`) -> derive from `viewerEvents`
- [x] `history[0]` in `loadStatus` ("New event - refresh" flag, `newEvents`) -> compare against `viewerEvents`
- [x] "Open viewer" button's `history.length` guard

### 2. "View history" opens the scrubber directly
- [x] `toggleHistory` / `openViewer`: opening history loads `viewerEvents` and shows the scrubber
- [x] Drop the separate "Open viewer" / "View range" button
- [x] Keep date filter (From/To, `applyFilter`), Delete matching/all, refresh, MP4 export
- [x] Empty state for zero events

### 2b. Default the range to the current day
- [x] Check how From/To become `since`/`until` (epoch vs date string, browser vs device timezone) so "today" is the device's local day
- [x] Seed From/To to today on every open (no persistence) and load with `since`/`until`
- [x] Empty range shows "No events in this range" with one-tap "Show all" / "Last 7 days"
- [x] Bulk button reads "Delete matching" while a range is active. "Delete all" needs an explicit clear-filter step first
- [x] "New event - refresh" compares against the current range's events

### 3. Delete the old list from `Sentry.js` (~100 lines)
- [x] Remove `HISTORY_PAGE_SIZE`, `history`, `historyHasMore`, `historyError`, `historyBusy`, `historyRequestId`
- [x] Remove `stopObserving`, `observeSentinel`, the IntersectionObserver sentinel and "Retry loading more"
- [x] Remove `loadHistory({append})` and its watchers (~L61-70)
- [x] Remove the card-list template (~L578-605)
- [x] Keep `deleteEvent`, `makeTimelapse`, camera/pacing pickers

### 4. Small parity additions
- [x] Tap the scrubber image to open full size (old cards used `target=_blank`)
- [x] Thumbnail strip: skipped on purpose, only if requested

### 5. Keep (no change)
- `api.js` `getSentryEvents` with `limit`/`offset`
- `GET /api/sentry/events` pagination on the backend (used by hardwared, wheel_controlsd, sentryd and the desktop UI)

### 6. Docs
- [x] Update `.scratch/sentry-history-pagination.md` and `.scratch/sentry-timelapse.md` (status and next steps) to say the list was removed

## Risks
- Events seconds apart are under a pixel apart on the timeline. Step buttons and arrow keys are the fallback.
- Speed and camera choices are not remembered between opens (optional).

## Verification
- [x] `tests/test_ui_vue_frontend.py` passes. Its asserts (`/sentry` route, no `fetch(` in `Sentry.js`, api method names, Cameras hub tabs) don't touch the list.
- [x] Add a frontend test: old list markers (`HISTORY_PAGE_SIZE`, `observeSentinel`) are gone from `Sentry.js`, and "View history" renders `SentryScrubber`
- [x] jsdom integration checks against the real `Sentry.js`/`SentryScrubber.js` with a fake backend: `.venv/harness/run.sh` (15 passed). Covers each item below except real browser rendering, touch and timelapse download.
- [ ] Manual check in a real browser (`/cameras` -> Sentry Mode):
  - [ ] View history opens the scrubber
  - [ ] Date filter narrows events
  - [ ] Delete one event
  - [ ] Delete matching
  - [ ] Refresh after a new event
  - [ ] Timelapse export
  - [ ] Empty history / empty range
- [ ] Commit (ask first)

## Log
- 2026-09-19: Plan written. Desktop UI stays as is. Range defaults to today.
- 2026-09-19: Implemented in `views/Sentry.js` (old list removed, today default, empty-range buttons, `viewerLatestId` for the new-event flag) and `SentryScrubber.js` (tappable image). Added `test_ui_sentry_history_opens_the_scrubber_on_today`. `test_ui_vue_frontend.py`: 28 passed, 4 skipped (run with `--noconftest`, the repo conftest needs native extensions that are not built here). No node on this box, so the JS was only parse-checked with esprima, not run. Timezone: the range already used the browser's local days, and that is kept. Not done: browser walkthrough, commit.
- 2026-09-19: Set up a persistent JS test env in this worktree (git-ignored, under `.venv/`): node from `nodejs-wheel-binaries`, plus Vue 3 and jsdom in `.venv/harness/`. Run `.venv/harness/run.sh`. Repo tests with node on PATH (`source .venv/harness/env.sh`): `test_ui_vue_frontend.py` + `test_frontend_module_graph.py` = 43 passed, 0 skipped. jsdom checks: 15 passed. Two early failures were harness bugs (scrubber has its own "Delete" button; "Show all" only exists on the empty state), not component bugs.
