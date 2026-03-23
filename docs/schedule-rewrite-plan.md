# Schedule Feature Rewrite Plan

Read complete. This is planning only; no code changes were made.

## 1) Current-State Findings
1. Schedule UI/workflow is explicitly movie-only: menu button text, callback action, and prompts all assume "Schedule Movie" and unreleased movies only in [handlers.py#L50](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/workflows/tracking_workflow/handlers.py#L50), [handlers.py#L59](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/workflows/tracking_workflow/handlers.py#L59), [handlers.py#L154](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/workflows/tracking_workflow/handlers.py#L154).
2. Tracking workflow state is tied to movie title input (`await_movie_title`) in [handlers.py#L37](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/workflows/tracking_workflow/handlers.py#L37) and [state.py#L6](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/workflows/tracking_workflow/state.py#L6).
3. Tracking domain type hardcodes `target_kind: Literal["movie"]` in [types.py#L75](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/domain/types.py#L75).
4. Manager API is movie-specific (`create_movie_tracking_item`) and generates `movie:...` identity keys in [manager.py#L131](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/tracking/manager.py#L131), [manager.py#L167](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/tracking/manager.py#L167), [manager.py#L175](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/tracking/manager.py#L175).
5. Persistence normalizer forces `target_kind = "movie"` and fallback identity prefix `movie:` in [persistence.py#L21](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/tracking/persistence.py#L21), [persistence.py#L69](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/tracking/persistence.py#L69), with current schema `version = 1` in [persistence.py#L10](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/tracking/persistence.py#L10).
6. Scheduler hardcodes movie search/query/result wiring: parsed info `{"type":"movie"}` and `orchestrate_searches(..., "movie", ...)` in [scheduler.py#L69](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/tracking/scheduler.py#L69) and [scheduler.py#L162](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/tracking/scheduler.py#L162).
7. Auto-selection logic is movie-preference-specific (`preferences.movies.resolutions`) in [selection.py#L41](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/tracking/selection.py#L41), [selection.py#L43](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/tracking/selection.py#L43).
8. Metadata candidate resolution is movie-only (`MovieTrackingResolution`, `find_movie_tracking_candidates`) in [movie_release_dates.py#L44](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/tracking/movie_release_dates.py#L44), [movie_release_dates.py#L552](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/tracking/movie_release_dates.py#L552).
9. User-facing help text still advertises schedule as unreleased movies only in [command_handlers.py#L29](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/handlers/command_handlers.py#L29).
10. Download completion handshake is already generic by `tracking_item_id` (good reuse point for TV) in [lifecycle.py#L49](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/download_manager/lifecycle.py#L49), [lifecycle.py#L115](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/download_manager/lifecycle.py#L115).
11. Collection aggregation is Wikipedia-based today, via franchise lookup flow in [movie_collection_flow.py#L143](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/workflows/search_workflow/movie_collection_flow.py#L143), [movie_collection_flow.py#L311](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/workflows/search_workflow/movie_collection_flow.py#L311), [scraping_service.py#L247](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/scraping_service.py#L247).
12. TMDB collection tooling is currently probe/diagnostic tooling, not workflow source-of-truth, in [probe_tmdb_collection_details.py#L29](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/utility_scripts/probe_tmdb_collection_details.py#L29).

## 2) Target Architecture
1. Introduce a unified tracking model with `target_kind` (`movie|tv`) and `schedule_mode` (`future_release|ongoing_next_episode`), plus shared lifecycle/status fields and a `target_payload` block for media-specific metadata.
2. Replace media-specific manager entrypoints with a generic `create_tracking_item(...)` and target adapters keyed by `target_kind`.
3. Add adapter interface boundaries:
`resolve_candidates_from_user_input`, `refresh_target_metadata`, `build_search_request`, `select_candidate`, `on_queue_success`, `on_queue_failure`, `on_fulfillment_success`.
4. Keep scheduler core generic:
due-item scan, lock/in-progress guard, adapter dispatch, persistence, retry policy.
5. Generalize selection logic to `resolve_top_resolution_tiers(search_config, media_type)` and keep existing movie behavior unchanged; add tv path using `preferences.tv.resolutions`.
6. Keep workflow UI simple and single TV mode:
`Schedule Movie (Future Release)` and `Schedule TV Show (Ongoing Next Episodes)` only.
7. Preserve source boundaries:
movie collection aggregation remains Wikipedia-based, while TV scheduling metadata is TMDB-first (series/season/episode release data).

Proposed item shape (v2):

```json
{
  "id": "trk_xxxx",
  "chat_id": 123,
  "target_kind": "movie|tv",
  "schedule_mode": "future_release|ongoing_next_episode",
  "target_identity": "stable-identity-string",
  "display_title": "User-facing title",
  "status": "awaiting_metadata|awaiting_window|searching|waiting_fulfillment|fulfilled",
  "next_check_at_utc": "ISO|nil",
  "last_checked_at_utc": "ISO|nil",
  "created_at_utc": "ISO",
  "fulfilled_at_utc": "ISO|nil",
  "linked_download_message_id": 0,
  "target_payload": {},
  "retry": {"consecutive_failures": 0, "last_error": null}
}
```

## 3) TV Ongoing/Next-Episode Algorithm (TMDB-first)
1. On schedule creation, resolve and validate show identity from TMDB (stable `series_id`), and store `target_payload` with TMDB identifiers plus `episode_cursor` (last fulfilled/known episode).
2. On each due tick, refresh season/episode metadata from TMDB (`tv details`, `tv season details`, and episode-level `air_date` fields as needed), then merge with local-availability facts (`get_existing_episodes_for_season`) to avoid duplicate downloading.
3. Build an ordered episode stream `(season, episode)` above cursor.
4. Choose "next episode" as the first episode above cursor that is released (`release_date <= local_today`) and not already present/fulfilled.
5. If no released episode exists but a future `release_date` exists, set `status=awaiting_window` and `next_check_at_utc` to local noon on earliest future date (same noon-gate behavior as movies).
6. If no reliable date exists in TMDB, set `status=awaiting_metadata` and weekly metadata retry.
7. When next released episode exists, run TV search query `"{show} SxxEyy"` with `media_type="tv"` and existing search/scoring orchestration.
8. Select candidate using top-tier resolution policy for TV; if none, keep active and retry hourly.
9. On queue success, set `waiting_fulfillment`, persist `pending_episode` and linked message id.
10. On successful post-processing, advance `episode_cursor` to `pending_episode`, clear pending marker, and immediately/short-delay re-enter metadata phase to find the next episode.
11. On queue/post-process failure, keep same `pending_episode` and retry hourly; never advance cursor on failure.

## 4) Migration Strategy (`tracking_state.json`)
1. Add schema `version=2`; keep loader backward-compatible with `version=1`.
2. On load v1 item:
map movie fields into new generic shape, set `target_kind=movie`, `schedule_mode=future_release`, and migrate status enum mappings (`pending_date`, `waiting_release_window`, `watching_release`, `waiting_fulfillment`, `fulfilled`).
3. Preserve old timestamps/IDs exactly where valid; coerce invalid fields defensively and skip malformed items with clear logging.
4. Before first v2 write, create one backup file (`tracking_state.v1.bak`) for rollback safety.
5. Save path remains same file (`tracking_state.json`) to avoid operational drift; only payload version changes.
6. Add migration tests for:
empty file, valid v1 movie item, mixed malformed entries, and roundtrip write/read v2.

## 5) Backward Compatibility and Risk Analysis
1. Preserve behavior outside Schedule by limiting edits to tracking workflow/service paths and shared text only where schedule copy changes.
2. Keep movie scheduling parity by first porting movie logic behind adapter without changing business rules (noon gate, weekly metadata, top-tier-only).
3. Keep movie collection aggregation unchanged and Wikipedia-first; do not wire TMDB collection service into movie collection workflow as part of this rewrite.
4. TV release scheduling uses TMDB as the source of truth for future episode dates; optionally allow a manual override path for one-off bad upstream metadata.
5. Risk: duplicate TV downloads when filesystem metadata lags.
Mitigation: check local episode presence + `pending_episode` before queueing.
6. Risk: schedule stalls in `waiting_fulfillment`.
Mitigation: add fulfillment timeout watchdog that reverts to searching if post-processing never confirms success.
7. Risk: migration regressions.
Mitigation: versioned loader tests + backup file + conservative coercion.
8. Risk: scheduler load increase from ongoing TV items.
Mitigation: per-item cadence, due-time gating, and in-progress guards already present.

## 6) Test Strategy
1. Unit tests:
adapter behavior, status transitions, resolution-tier selection by media type, migration coercion/mapping, TV next-episode resolver edge cases (future-only, unknown dates, season rollover).
2. Workflow tests:
movie scheduling unchanged, TV schedule creation flow (single mode only), mixed review/cancel list behavior for movie+tv items.
3. Scheduler tests:
movie parity matrix retained, plus TV matrices for `awaiting_metadata`, `awaiting_window`, `searching`, queue success/failure, and cursor advancement only on fulfillment.
4. Persistence tests:
v1 load, v2 save, mixed-item roundtrip, malformed-item resilience.
5. Integration tests:
download lifecycle updates tracking correctly for movie and TV ongoing entries via `tracking_item_id`.
6. Regression tests:
home menu/callback routing/search/delete/link untouched.

## 7) Phased Rollout (with checkpoints and rollback points)
1. Phase 0: Characterization baseline.
Checkpoint: existing schedule tests green.
Rollback: none (test-only).
2. Phase 1: Schema + migration layer (read v1/write v2) behind no behavior changes.
Checkpoint: persistence/migration tests green, existing movie workflow still green.
Rollback: revert migration commit; restore backup file.
3. Phase 2: Generic scheduler/manager with movie adapter parity only.
Checkpoint: movie scheduler parity suite green.
Rollback: keep legacy movie path behind temporary flag and revert adapter switch.
4. Phase 3: TV adapter + next-episode resolver in service layer (no UI exposure yet).
Checkpoint: TV unit/scheduler tests green with feature flag off by default.
Rollback: disable TV adapter registration flag.
5. Phase 4: Tracking workflow UI for TV ongoing mode (single mode only), review/cancel mixed items.
Checkpoint: workflow tests green and manual smoke.
Rollback: hide TV schedule button/callback, keep service code dormant.
6. Phase 5: Cleanup and hardening.
Checkpoint: full test suite, pre-commit, docs updates.
Rollback: revert cleanup commit only; preserve working feature commits.

## 8) Exact Files/Modules Likely to Change
1. [telegram_bot/domain/types.py](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/domain/types.py)
2. [telegram_bot/services/tracking/manager.py](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/tracking/manager.py)
3. [telegram_bot/services/tracking/scheduler.py](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/tracking/scheduler.py)
4. [telegram_bot/services/tracking/persistence.py](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/tracking/persistence.py)
5. [telegram_bot/services/tracking/selection.py](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/tracking/selection.py)
6. [telegram_bot/services/tracking/movie_release_dates.py](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/tracking/movie_release_dates.py)
7. [telegram_bot/workflows/tracking_workflow/handlers.py](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/workflows/tracking_workflow/handlers.py)
8. [telegram_bot/workflows/tracking_workflow/state.py](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/workflows/tracking_workflow/state.py)
9. [telegram_bot/handlers/command_handlers.py](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/handlers/command_handlers.py)
10. [telegram_bot/ui/home_menu.py](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/ui/home_menu.py)
11. [telegram_bot/handlers/callback_handlers.py](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/handlers/callback_handlers.py)
12. [telegram_bot/services/download_manager/lifecycle.py](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/services/download_manager/lifecycle.py)
13. [telegram_bot/utility_scripts/simulate_tracking_scheduler.py](C:/Users/Ryan/Desktop/plex-o-tron/telegram_bot/utility_scripts/simulate_tracking_scheduler.py)
14. [tests/workflows/test_tracking_workflow.py](C:/Users/Ryan/Desktop/plex-o-tron/tests/workflows/test_tracking_workflow.py)
15. [tests/services/test_tracking_scheduler.py](C:/Users/Ryan/Desktop/plex-o-tron/tests/services/test_tracking_scheduler.py)
16. [tests/services/test_tracking_manager.py](C:/Users/Ryan/Desktop/plex-o-tron/tests/services/test_tracking_manager.py)
17. [tests/test_state.py](C:/Users/Ryan/Desktop/plex-o-tron/tests/test_state.py)
18. New modules likely: `telegram_bot/services/tracking/targets/base.py`, `telegram_bot/services/tracking/targets/movie.py`, `telegram_bot/services/tracking/targets/tv_ongoing.py`, `telegram_bot/services/tracking/tv_next_episode.py`, and migration-focused tests.

## Recommended implementation order
1. Add v2 tracking schema + migration loader/writer and tests.
2. Refactor movie tracking into adapter-based generic scheduler with strict parity.
3. Generalize selection policy by media type without changing movie behavior.
4. Implement TV next-episode TMDB metadata resolver and scheduler adapter.
5. Wire TV ongoing mode into tracking workflow UI (single mode only).
6. Integrate download lifecycle cursor advancement and failure retry for TV.
7. Update simulator/tests/docs/help text and run full validation.
