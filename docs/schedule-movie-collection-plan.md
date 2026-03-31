# Schedule Movie Collection Plan (Revised)

## Problem
The previous plan modeled collection scheduling as:

`Schedule -> Movie -> Yes/No collection intent -> single movie title`

That is the wrong shape. It did not follow the existing Movie -> Collection lookup sequence, and it did not center the scheduling decision on streaming release readiness per movie.

## Required Workflow Shape
Use this flow for collection scheduling:

`Schedule -> Movie -> Collection -> Send collection/franchise name -> Resolve collection titles -> Keep titles without a streaming release date -> Confirm -> Schedule`

Behavior rules:
- The collection path asks for a collection/franchise name, not a single seed movie title.
- Collection matching is Wikipedia-first and should mirror existing Movie -> Collection search behavior.
- After title resolution, streaming availability is resolved per title via TMDB.
- Only titles that do not yet have a streaming release date in the past are eligible for scheduling.

## Source-of-Truth Sequence To Reuse
The scheduling implementation should intentionally mirror this existing chain from search collection flow:

1. `telegram_bot/workflows/search_workflow/handlers.py::_handle_movie_scope_button`
2. `telegram_bot/workflows/search_workflow/movie_collection_flow.py::_start_collection_lookup`
3. `telegram_bot/services/scraping_service.py::fetch_movie_franchise_details`
4. `telegram_bot/services/scrapers/wikipedia/franchise.py::fetch_movie_franchise_details_from_wikipedia`
5. `telegram_bot/workflows/search_workflow/movie_collection_flow.py` normalization patterns (`sanitize_collection_name`, `_normalize_collection_movie_title`)

Streaming-date resolution for scheduling should reuse tracking-side release logic:
- `telegram_bot/services/tracking/movie_release_dates.py::resolve_movie_tracking_target`
- `telegram_bot/services/tracking/tmdb_release_service.py::resolve_tmdb_availability`

## Scope (V1)
- Add a Schedule Movie collection branch that prompts for collection name.
- Reuse existing Wikipedia franchise extraction path for title enumeration.
- Resolve and classify streaming availability per resolved title.
- Schedule only unresolved/future-streaming titles.
- Keep all current single-movie scheduling behavior intact.

Out of scope:
- Auto-downloading a whole collection immediately.
- New download queue orchestration for tracking.
- Breaking schema changes in persisted tracking items.

## Implementation Plan

### Phase 1: Tracking Workflow UX and State
Implementation:
- Add Movie scope selection in tracking workflow (Single vs Collection), consistent with current keyboard patterns.
- Add collection-specific next action key, for example `TRACKING_AWAIT_COLLECTION_NAME`.
- Prompt copy for collection path should explicitly request collection/franchise name.

Files:
- `telegram_bot/workflows/tracking_workflow/handlers.py`
- `telegram_bot/workflows/tracking_workflow/state.py`
- `telegram_bot/ui/keyboards.py` (reuse existing helpers only)

Acceptance criteria:
- User can explicitly enter a collection scheduling path from Schedule Movie.
- Existing TV scheduling path is unchanged.

### Phase 2: Collection Title Resolution Adapter (Wikipedia-first)
Implementation:
- Add a tracking-focused resolver module, for example `telegram_bot/services/tracking/collection_resolution.py`.
- Use `fetch_movie_franchise_details(...)` as the first-class resolver.
- Normalize collection name and movie titles using the same normalization approach used in Movie -> Collection flow.
- Return deterministic, minimal payload:
  - `collection_name`
  - `collection_source`
  - `movies` (title/year/identifier)

Important:
- Do not duplicate a separate franchise scoring system in tracking. Reuse the existing Wikipedia franchise resolver path.

Acceptance criteria:
- Known collections resolve to consistent title lists.
- No resolved collection causes a crash when metadata is partial.

### Phase 3: Streaming Availability Filtering Per Title (TMDB)
Implementation:
- For each resolved movie title, resolve availability metadata with tracking release services.
- Treat titles as schedulable when streaming date is unknown or in the future.
- Treat titles as already released when a streaming date exists and is `<= today`.
- Preserve source metadata (`streaming`, `physical`, `unknown`) for logs/debug and confirmation copy.

Files:
- `telegram_bot/services/tracking/movie_release_dates.py`
- `telegram_bot/services/tracking/tmdb_release_service.py`
- New collection tracking resolver module from Phase 2

Acceptance criteria:
- Collection candidate set is filtered down to titles that have not yet had a streaming release.
- TMDB errors or missing credentials degrade gracefully (no handler crash).

### Phase 4: Confirmation and Item Creation
Implementation:
- Show a confirmation summary with:
  - Collection name
  - Included titles to schedule
  - Optional skipped-count context for already-streaming titles
- Create one tracking item per selected title via `tracking_manager.create_movie_tracking_item(...)`.
- Store optional collection metadata in `target_payload` for traceability.

Files:
- `telegram_bot/workflows/tracking_workflow/handlers.py`
- `telegram_bot/services/tracking/manager.py`
- `telegram_bot/services/tracking/persistence.py`
- `telegram_bot/domain/types.py` (optional typed payload extension)

Acceptance criteria:
- Confirming a collection schedule creates per-title tracking entries.
- Persistence remains backward compatible.

## Test Plan

### Workflow tests
- Extend `tests/workflows/test_tracking_workflow.py`:
  - Movie scope selection routes correctly.
  - Collection-name prompt appears for collection branch.
  - Confirm flow schedules only unresolved/future-streaming titles.
  - Cancel clears collection-specific state.

### Service tests
- Add `tests/services/test_tracking_collection_resolution.py`:
  - Wikipedia collection resolution success.
  - Empty/ambiguous collection response handling.
  - TMDB streaming filter behavior (released/future/unknown).
  - TMDB failure path remains non-fatal.
- Extend `tests/services/test_tracking_movie_release_dates.py` and `tests/services/test_tmdb_release_service.py` as needed for streaming-classification edge cases.
- Extend `tests/services/test_tracking_manager.py` and `tests/services/test_tracking_persistence.py` for optional collection metadata compatibility.

## Manual Verification Checklist
1. Open tracking menu and choose `Schedule Movie`.
2. Choose `Collection` scope and send a known franchise name.
3. Verify resolved titles are shown and already-streaming titles are excluded.
4. Confirm schedule and verify multiple movie tracking items are created.
5. Restart bot and verify scheduled items reload correctly.
6. Repeat with TMDB credentials missing and verify graceful fallback messaging.

## Risks and Mitigations
- Risk: Wikipedia franchise mismatch returns wrong titles.
- Mitigation: reuse existing scored franchise extraction path and expose resolved title preview before confirm.

- Risk: TMDB metadata gaps cause false negatives.
- Mitigation: unknown streaming date remains schedulable and enters normal metadata refresh loop.

- Risk: UX complexity regresses current single-movie schedule flow.
- Mitigation: isolate collection branch state keys and keep single-movie branch behavior unchanged.

## Definition of Done
- Schedule Movie includes a collection branch that asks for collection name.
- Collection title resolution mirrors Movie -> Collection workflow sequence.
- Streaming-date classification is done per resolved title with TMDB-backed metadata.
- Only titles without past streaming release are scheduled.
- Tests cover workflow, resolver behavior, and persistence compatibility.
