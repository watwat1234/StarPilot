# Sentry history: refresh fix, pagination, infinite scroll, date filter, bulk delete

Branch: `sentry-history-pagination` (merged into `wat-bolt-tuning`, pushed to `origin` and `github`).
All paths below are under `starpilot/system/the_galaxy/`.

## Status
The mobile paginated list, infinite scroll and paging state described below were later removed: "View history" now opens the scrubber directly, seeded to today. See `sentry-history-viewer-migration.md`. The backend `limit`/`offset` pagination and the desktop Load more list are unchanged.

Later work on this page (timelapse from the retained images, kind badges, gap pacing) is documented in `sentry-timelapse.md`. The user has since confirmed the Sentry page is working well on the device. The paragraph below is the original status when this doc was written and is kept for the record.

**None of this had been run when this was written.** No `node` on the dev box, no `pytest` (`python3 -m unittest tests.test_ui_vue_frontend` errored and was not investigated), and no browser or device test. Only `python3 -m py_compile the_galaxy.py` was run (passes). The only device-side confirmation so far: the refresh fix worked after the mobile change was deployed (user report). Everything after that (infinite scroll, date filter, bulk delete) is untested.

## The problem
The Sentry event viewer refreshed every 5 seconds, so history could not be browsed. The 5s status poll also reloaded the whole history list and swapped it for a "Loading Sentry history…" placeholder, which reset scroll and tore down the DOM.

## Two UIs (this cost us a round trip)
- **Desktop UI:** `assets/components/tools/sentry.js` (arrow-core). Being **deprecated**. Fixed for the refresh bug and paginated with a Load more button (commit `721dd0718`). **Not** given infinite scroll, date filter or bulk delete, and will not be.
- **Mobile / responsive UI:** `assets/mobile/js/views/Sentry.js` (Vue, capital S) with `assets/mobile/js/api.js`. **This is the one the user's browser actually loads.** All later work is here only.

First attempt only touched the desktop UI, so the user still saw the bug. The mobile caller was spotted in a grep at the time and only protected from breaking (see "Backward compat"), not fixed. Lesson: grep for every caller of an endpoint and check which UI the user is actually loading before declaring a fix done. A capital-S `Sentry.js` in the browser's Sources tab was the giveaway.

## What changed

### Backend: `the_galaxy.py`
- `GET /api/sentry/events`
  - `limit` (clamped 1-50) and `offset` (min 0) are **opt-in**. With no `limit` it returns the full list, as before. Non-numeric values fall back to defaults.
  - Response now includes `total` and `hasMore` (plus `offset` and `limit` when paginated).
  - `since` / `until` (ISO 8601 timestamps, `until` **exclusive**) filter by `detectedAt` before pagination, so `total` / `hasMore` describe the filtered set. A malformed value returns 400. Events with an unparseable `detectedAt` never match a date filter.
- `DELETE /api/sentry/events` (new)
  - Only while parked (`IsOffroad`), otherwise 409, same rule as single-event delete.
  - With `since` / `until`: deletes events in that range. With neither, it refuses (400) unless `all=1`.
  - Removes each event directory, rewrites `events.json`, and repoints or clears the `SentryModeLastEvent` param.
  - Returns `{"deleted": n, "failed": m}`. Runs synchronously in one request.
- New helpers: `_parse_sentry_instant`, `_filter_sentry_events_by_time`, `_sentry_time_filter_from_request`, `_delete_sentry_event_storage`. The existing single-event `DELETE /api/sentry/events/<id>` was left alone (it does not use `_delete_sentry_event_storage`, so a little logic is duplicated).

### Mobile UI: `assets/mobile/js/views/Sentry.js`, `api.js`
- **Refresh fix:** `loadStatus()` (5s poll) no longer reloads history. It sets `newEvents` when the latest event id differs from `history[0]`, and a "New event - refresh" button reloads from the top. Not flagged while a date filter is active. The loading placeholder only shows when the list is empty.
- **Pagination + infinite scroll:** history loads 10 at a time (`HISTORY_PAGE_SIZE`). A sentinel `<div ref="sentinel">` is watched by an `IntersectionObserver` (`rootMargin: 400px 0px`).
  - The observer is re-armed whenever `history` or `historyHasMore` changes, so a short page that leaves the sentinel on screen keeps loading.
  - On a failed load, `historyError` stops auto-loading and shows a "Retry loading more" button, which avoids a request loop.
  - Appends de-duplicate by `eventId` because newest-first offsets shift when a new event arrives.
  - The observer is disconnected on hide and on `beforeUnmount`.
- **Date range:** From / To `<input type="date">`. Local calendar days are converted to UTC instants (`since`; `until` = end date + 1 day, exclusive). Start after end shows a snackbar and does not load. Clear resets. A `historyRequestId` counter discards stale responses when the filter changes mid-flight.
- **Delete all / Delete matching:** red button next to the date pickers (visible when `historyTotal > 0`). Label changes with an active filter. `GalaxyConfirm` shows the count. Calls `api.deleteSentryEvents({...range, all: !scoped})`, then reloads history and status.
- `api.getSentryEvents({limit, offset, since, until})` builds a `URLSearchParams` query. `api.deleteSentryEvents({since, until, all})` is new.

### Desktop UI: `assets/components/tools/sentry.js` (commit `721dd0718` only)
Same refresh fix, Load more button, `stopPolling()`. The router has no unmount hook, so the interval stops itself when `.sentry-page` is no longer in the DOM.

## Backward compat
The mobile API is the only other caller of `/api/sentry/events`. Pagination is opt-in specifically so that callers without `limit` still receive every event. A default limit of 10 would have silently truncated the old mobile view to 10 events.

## Storage layout (for manual cleanup)
- `/data/media/0/sentryd/<eventId>/` (images) and `/data/media/0/sentryd/events.json` (index). On a PC dev box there is also `<comma_home>/starpilot/data/sentryd`.
- Param `SentryModeLastEvent` holds the latest event.
- `_sentry_event_catalog()` re-adds directories missing from the index (legacy discovery) and re-inserts `SentryModeLastEvent` if it is not in the index. So a manual purge must remove **all three**: the event directories, `events.json`, and the param (`/data/params/d/SentryModeLastEvent`, path unverified). The `live` directory is not an event.

## Commits
| Change | Feature commit | Merge into `wat-bolt-tuning` |
|---|---|---|
| Desktop refresh fix + backend pagination | `721dd0718` | `849c41c36` |
| Mobile refresh fix + Load more | `f4fca169b` | `014f2058a` |
| Mobile infinite scroll | `60ea7d150` | `4d9066f9d` |
| Date filter + bulk delete (backend + mobile) | `860631df9` | `701253f97` |

`wat-bolt-tuning` is checked out in a separate worktree (`starpilot-wat-bolt-merge`), so the merges were done there. The feature branch lives in `starpilot-wat-bolt-analysis`. Both remotes (`origin` = git.waffle, `github`) got both branches each time.

## Deploying
Pushing does not update a running device. Pull `wat-bolt-tuning` on the device and **restart Galaxy**. The bulk-delete route is backend code, so a page reload alone is not enough. Then hard-reload the browser (JS modules are imported without version query strings and can be cached).

## Test checklist (nothing below has been run)
1. Open Sentry, View history, scroll. Wait 10s+: no list rebuild, no loading flash, scroll position stable.
2. Network tab: only `/api/sentry/status` every 5s. `/events` requests carry `?limit=10&offset=N`, one per page, offsets increasing, no bursts or repeats.
3. Turn the network off mid-scroll: "Retry loading more" appears and no request loop starts.
4. Create a new event while browsing: "New event - refresh" appears, nothing shifts until clicked, then no duplicate cards.
5. `curl '/api/sentry/events?limit=2&offset=0'`, `offset=2`, `limit=-1`, `offset=abc` (clamped, no 500), and `since=garbage` (400).
6. Date range covering a few events: list and count update, Clear restores. Range with no events shows the empty message. Start after end shows a snackbar.
7. "Delete matching" on a small range: only those events go, count and latest-event card update. Try it while driving/onroad and expect the 409 message.
8. "Delete all" last. Confirm no phantom event reappears (the `SentryModeLastEvent` fix-up).
9. Time zones: check the boundary. An event just before local midnight should land on the right day in the filter.

## Known gaps and follow-ups
- Nothing has been run (see Status). No unit tests were added for the new endpoints or helpers.
- `tests/test_ui_vue_frontend.py` asserts `Sentry.js` has no `fetch(` and no `GalaxyEmbed`. Grep says that still holds. The test itself was not run.
- Bulk delete is synchronous. If it times out with a very large history, delete in ranges or use the SSH purge.
- `until` uses local-midnight conversion in the browser. Events whose `detectedAt` has no timezone are treated as UTC server-side.
- Desktop UI is untouched beyond the first two fixes and is deprecated.
- The single-event delete could reuse `_delete_sentry_event_storage`. Left as is to limit the diff.
