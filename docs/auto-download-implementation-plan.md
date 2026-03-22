# Auto-Download Tracking Implementation Plan

## Scope

This plan implements the V1 proposal in [auto_download_requirements.md](../auto_download_requirements.md):

- support unreleased movies only
- schedule future-release checks until fulfillment or manual cancellation
- do not search torrent sites until an earliest confirmed streaming or Blu-ray/DVD date exists
- use the existing movie search scoring behavior
- auto-download only from the highest configured movie resolution tier
- mark the schedule fulfilled only after file organization completes successfully
- persist schedule state in a dedicated JSON file separate from `persistence.json`

The plan is intentionally structured so the shared scheduler core can later support movie collections, TV episodes, TV seasons, and recurring targets without being rewritten around movie-only assumptions.

## Existing Reuse Points

The current codebase already contains the main seams this feature should build on:

- [__main__.py](../__main__.py): app startup and handler registration
- [telegram_bot/state.py](../telegram_bot/state.py): startup/shutdown lifecycle hooks
- [telegram_bot/ui/home_menu.py](../telegram_bot/ui/home_menu.py): home menu rendering
- [telegram_bot/handlers/callback_handlers.py](../telegram_bot/handlers/callback_handlers.py): inline callback routing
- [telegram_bot/handlers/message_handlers.py](../telegram_bot/handlers/message_handlers.py): DM text routing by active workflow
- [telegram_bot/workflows/navigation.py](../telegram_bot/workflows/navigation.py): top-level workflow state
- [telegram_bot/services/search_logic/orchestrator.py](../telegram_bot/services/search_logic/orchestrator.py): concurrent torrent search orchestration
- [telegram_bot/services/download_manager/queue.py](../telegram_bot/services/download_manager/queue.py): queue management
- [telegram_bot/services/download_manager/lifecycle.py](../telegram_bot/services/download_manager/lifecycle.py): success, failure, cancellation, and finalization
- [telegram_bot/services/media_manager/processing.py](../telegram_bot/services/media_manager/processing.py): file organization and Plex scan
- [telegram_bot/workflows/search_workflow/movie_collection_flow.py](../telegram_bot/workflows/search_workflow/movie_collection_flow.py): current release-date extraction patterns
- [telegram_bot/services/scrapers/wikipedia/dates.py](../telegram_bot/services/scrapers/wikipedia/dates.py): reusable date parsing helper

## Important Gaps To Fix First

Two current seams are not strong enough for unattended scheduling:

### 1. Queueing is callback-driven

`add_download_to_queue(...)` currently depends on a callback flow and `context.user_data["pending_torrent"]`. The scheduler should not fake a user confirmation path.

Recommendation:

- extract a reusable queue primitive such as `queue_download_source(...)`
- let both the existing manual confirmation flow and the new scheduler call that shared primitive

### 2. Fulfillment is not machine-readable

`handle_successful_download(...)` currently returns a formatted message string even when post-processing fails. That is fine for chat output, but the scheduler needs a structured success signal.

Recommendation:

- introduce a structured result for download finalization, for example `PostProcessingResult`
- include:
  - `succeeded`
  - `final_message`
  - `destination_path`
  - `media_type`
  - `title`
  - `year`
- keep Telegram formatting separate from fulfillment state decisions

Without this refactor, the scheduler would be forced to infer success by parsing human-facing message text.

## Proposed Module Layout

### Workflow layer

Add a new workflow package:

```text
telegram_bot/workflows/tracking_workflow/
    __init__.py
    handlers.py
    movie_flow.py
    state.py
```

Responsibility:

- launch and route the tracking workflow
- collect a movie title
- show candidate future releases
- confirm schedule creation
- list active scheduled items
- handle cancellation from the review UI

### Service layer

Add a new service package:

```text
telegram_bot/services/tracking/
    __init__.py
    manager.py
    persistence.py
    scheduler.py
    movie_release_dates.py
    selection.py
```

Responsibility split:

- `manager.py`: create, list, cancel, fulfill, and reset tracking items
- `persistence.py`: load and save `tracking_state.json`
- `scheduler.py`: background loop, due-item dispatch, next-check calculations
- `movie_release_dates.py`: resolve canonical movie identity and earliest confirmed availability date
- `selection.py`: determine the highest configured resolution tier and choose the best eligible search result

### Shared types

Extend [telegram_bot/domain/types.py](../telegram_bot/domain/types.py) with TypedDicts for:

- `TrackingItem`
- `TrackingStateFile`
- `PostProcessingResult`

This keeps shared record shapes in the same place as the existing queue and batch metadata types.

## Recommended Persistent Model

Use a dedicated versioned file:

- filename: `tracking_state.json`

Recommended shape:

```json
{
  "version": 1,
  "items": {
    "trk_01": {
      "id": "trk_01",
      "chat_id": 7772565881,
      "target_kind": "movie",
      "status": "pending_date",
      "title": "Example Movie",
      "year": 2026,
      "canonical_title": "Example Movie",
      "release_date_status": "unknown",
      "availability_date": null,
      "availability_source": null,
      "next_check_at_utc": "2026-03-26T19:00:00Z",
      "last_checked_at_utc": null,
      "created_at_utc": "2026-03-19T22:30:00Z",
      "fulfilled_at_utc": null,
      "linked_download_message_id": null,
      "tracking_notes": null
    }
  }
}
```

Notes:

- persist UTC timestamps only
- keep runtime-only values out of the file
- keep `target_kind` even though V1 is movie-only so the schema does not need to be redesigned later

### Runtime-only application state

Store runtime state in `application.bot_data`, for example:

```python
{
    "tracking_items": {...},
    "tracking_loop_task": <Task>,
    "tracking_in_progress_ids": set(),
}
```

The JSON file stores only durable schedule state. The runtime store owns live tasks and concurrency guards.

## Tracking Item Lifecycle

Recommended statuses:

- `pending_date`: the movie is valid, but no earliest availability date is confirmed yet
- `waiting_release_window`: an earliest availability date is confirmed, but the first noon check has not arrived yet
- `watching_release`: the movie is eligible for hourly torrent-site checks
- `waiting_fulfillment`: a candidate has been queued and the tracker is waiting for download plus file organization to complete
- `fulfilled`: terminal success state
- `cancelled`: terminal user action state

Rules:

1. Creation starts in `pending_date` or `waiting_release_window`, depending on whether an availability date is already known.
2. `pending_date` items perform weekly metadata-only checks.
3. Once an earliest availability date is confirmed, compute the first due time as local noon on that date and convert it to UTC.
4. When the item becomes due, move it into `watching_release`.
5. A successful auto-queue moves the item into `waiting_fulfillment`.
6. Download failure, cancellation, or post-processing failure moves it back to `watching_release` with the next hourly due time.
7. Post-processing success moves it into `fulfilled`.

## Scheduling Strategy

Use a single background asyncio loop, not one task per item and not PTB `JobQueue`.

Why:

- the app already uses long-lived asyncio tasks in startup and download management
- persistent `next_check_at_utc` timestamps make a single scheduler loop easy to reason about
- this avoids a large number of dynamic job registrations
- it keeps restart recovery simple

Recommended behavior:

1. `post_init(...)` loads `tracking_state.json` and starts one scheduler task.
2. The scheduler wakes on a short cadence, such as once per minute.
3. On each tick, it finds due items whose ids are not already in `tracking_in_progress_ids`.
4. Due items are processed sequentially in V1 to keep network usage and debugging simple.
5. Every state mutation is persisted immediately.
6. `post_shutdown(...)` cancels the scheduler task, waits for it to stop, and saves the final snapshot.

### Due-time calculation

Use the operating-system timezone first.

Recommended helper behavior:

- derive local timezone from `datetime.now().astimezone().tzinfo`
- if that is unusable for future-date scheduling, add a normal config-backed fallback such as `host.timezone`
- always convert computed due times to UTC before persistence

Due-time rules:

- unknown date: next metadata check = now + 7 days
- known future date before noon local: next search = release date at 12:00 PM local
- known future date after noon local: next search = next hourly boundary after startup or state transition
- known past date loaded from disk on startup: search on the next scheduler tick

## Movie Release Metadata Service

Do not keep release-date discovery buried inside collection-specific workflow code.

Extract and generalize the reusable pieces from [telegram_bot/workflows/search_workflow/movie_collection_flow.py](../telegram_bot/workflows/search_workflow/movie_collection_flow.py):

- canonical movie-title lookup
- release-date extraction from Wikipedia infobox HTML
- current-year ambiguity handling
- earliest-date classification rules

`movie_release_dates.py` should expose one high-level function, for example:

```python
async def resolve_movie_tracking_target(title: str) -> TrackingTargetResolution:
    ...
```

It should return:

- canonical title
- canonical year when known
- whether the movie is already released
- whether the earliest confirmed availability date is known
- the earliest confirmed availability date
- whether that date came from streaming or Blu-ray/DVD

V1 rule:

- reject schedule creation if the earliest confirmed availability date is today or earlier
- if the date is unknown, allow creation but keep the item in metadata-only mode

## Auto-Selection Rules

Selection should reuse the existing movie scoring behavior but enforce a stricter eligibility filter for unattended downloads.

Recommended flow:

1. Run `orchestrate_searches(...)` with the canonical movie title and year.
2. Determine the highest configured movie resolution tier from `SEARCH_CONFIG.preferences.movies.resolutions`.
3. Normalize synonymous labels such as `4k` and `2160p` into the same tier.
4. Filter results to candidates in that top tier only.
5. Keep existing score ordering.
6. Select the highest scored eligible item.
7. If no top-tier candidate exists, do not queue anything and schedule the next hourly search.

This logic belongs in `services/tracking/selection.py`, not inside workflow code or the scheduler loop.

## Queue Integration

Refactor queue creation so scheduler-driven downloads do not depend on workflow session state.

Recommended extraction from [telegram_bot/services/download_manager/queue.py](../telegram_bot/services/download_manager/queue.py):

```python
async def queue_download_source(
    application,
    *,
    chat_id: int,
    source_dict: SourceDict,
    message_id: int,
    save_path: str | None = None,
) -> bool:
    ...
```

Then:

- `add_download_to_queue(...)` becomes a thin adapter from callback data to `queue_download_source(...)`
- the scheduler can build a `SourceDict` directly from the selected result and call the same queue path

To connect a queued download back to a tracking item, add a new optional field to `SourceDict`:

- `tracking_item_id`

## Fulfillment Hook

Tracking completion must happen only after file organization succeeds.

Recommended implementation:

1. Refactor `handle_successful_download(...)` to return `PostProcessingResult`.
2. In [telegram_bot/services/download_manager/lifecycle.py](../telegram_bot/services/download_manager/lifecycle.py), inspect `source_dict["tracking_item_id"]` after post-processing finishes.
3. On success:
   - mark the linked tracking item `fulfilled`
   - set `fulfilled_at_utc`
4. On post-processing failure:
   - return the tracking item to `watching_release`
   - schedule the next hourly retry
5. On explicit cancellation or download failure:
   - also return the tracking item to `watching_release`

This keeps fulfillment aligned with the product rule instead of with queue placement.

## Workflow And UI Changes

### Home menu

Add a new home-menu action:

- callback: `home_track`

Required updates:

- [telegram_bot/ui/home_menu.py](../telegram_bot/ui/home_menu.py)
- [telegram_bot/handlers/callback_handlers.py](../telegram_bot/handlers/callback_handlers.py)
- [telegram_bot/handlers/command_handlers.py](../telegram_bot/handlers/command_handlers.py)

### Navigation state

Extend the navigation state in [telegram_bot/workflows/navigation.py](../telegram_bot/workflows/navigation.py):

- add `"track"` to `NavigationState`
- add tracking-session cleanup in `clear_all_workflow_state(...)`
- route idle recovery the same way as the existing top-level workflows

### Message routing

Extend [telegram_bot/handlers/message_handlers.py](../telegram_bot/handlers/message_handlers.py) to route text input into the tracking workflow when `active_state == "track"`.

### Review and cancel UI

V1 does not need a complex management dashboard. A simple paginated inline list is enough:

- button from home menu to open tracking workflow
- tracking workflow menu with:
  - `Schedule Movie`
  - `Review Scheduled Items`
- review list entries show:
  - title
  - year
  - status
  - next check time
- each item exposes a cancel action with confirmation

## Recommended Implementation Sequence

### Phase 1. Shared primitives

- add tracking TypedDicts to `domain/types.py`
- add `TRACKING_STATE_FILE = "tracking_state.json"` to config constants
- create `services/tracking/persistence.py`
- create `services/tracking/manager.py`
- extract `queue_download_source(...)`
- introduce `PostProcessingResult`

### Phase 2. Release-date resolution

- create `services/tracking/movie_release_dates.py`
- move shared release-date helpers out of collection-specific workflow code as needed
- add unit tests for:
  - already released movie rejection
  - unknown date acceptance
  - earliest availability choice between streaming and physical

### Phase 3. Scheduler runtime

- create `services/tracking/scheduler.py`
- wire startup and shutdown in `state.py`
- add runtime bot_data stores
- add tests for:
  - weekly metadata checks
  - noon local first search
  - hourly retry after release date
  - no duplicate concurrent checks for the same item

### Phase 4. Workflow and UI

- create `workflows/tracking_workflow/`
- add `home_track`
- add review and cancellation UI
- extend navigation and DM routing
- add workflow tests for:
  - schedule creation
  - review list rendering
  - cancellation
  - stale session recovery

### Phase 5. Scheduler-to-download integration

- create `selection.py`
- call `orchestrate_searches(...)`
- queue auto-selected results through the shared queue primitive
- attach `tracking_item_id` to queued sources
- update lifecycle hooks to mark fulfillment or reset retries
- add integration tests covering:
  - top-tier-only eligibility
  - no queue when only lower-tier matches exist
  - success after file organization
  - retry after post-processing failure

## Test Plan

Add new tests:

- `tests/services/test_tracking_persistence.py`
- `tests/services/test_tracking_scheduler.py`
- `tests/services/test_tracking_selection.py`
- `tests/services/test_movie_release_dates.py`
- `tests/workflows/test_tracking_workflow.py`

Update existing tests:

- [tests/test_state.py](../tests/test_state.py) for startup/shutdown scheduler bootstrap
- [tests/workflows/test_navigation.py](../tests/workflows/test_navigation.py) for `"track"` navigation state
- any queue or lifecycle tests impacted by the extracted queue primitive or structured post-processing result

## Guard Rails

- keep all persisted timestamps in UTC
- do not persist runtime `Task` objects
- do not parse Markdown text to determine success
- do not let unknown-date items search torrent sites
- do not downgrade below the highest configured movie resolution tier
- do not duplicate collection-specific date logic inside the new scheduler

## Deferred Work

These should stay out of the first implementation unless they fall out naturally from the shared design:

- RSS ingestion
- movie collections
- TV episodes, seasons, and shows
- recurring targets
- quality-upgrade tracking
- Plex-based duplicate suppression beyond existing file-organization behavior
