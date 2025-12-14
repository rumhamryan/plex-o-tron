# Search Workflow Enhancement Plan

## Overview
- Consolidate the ad-hoc `context.user_data` keys the search workflow relies on into a typed session object so every handler reads/writes state consistently.
- Support advanced user input (e.g., `S02E05`, "Season 2", "Episode 5") to skip redundant prompts when the query already contains a season or episode hint.
- Cache Wikipedia lookups (years, season counts, episode titles) to eliminate duplicate network calls during a single run and provide clearer fallbacks when configuration data is missing.
- Let users refine the aggregated search results (pagination, resolution toggles, seed/size sorts) without rerunning every scraper, using richer callback actions on the existing inline keyboards.

## Initiative 1 – Typed Search Session State (✅ Complete – 2025‑12‑13)
### Goals
- Replace the fragile stringly-typed `next_action` + scattered `context.user_data[...]` mutations with a `SearchSession` dataclass (or `TypedDict`) that tracks the current step, media type, resolution, and TV-specific attributes in one place.
- Reduce the amount of "context lost" error handling by gating every handler with `session.require(...)` helpers and storing the session under a single key (e.g., `context.user_data["search_session"]`).

### Implementation Steps
1. Create `telegram_bot/workflows/search_session.py` with:
   - `Enum class SearchStep` (TITLE, YEAR, RESOLUTION, TV_SEASON, TV_SCOPE, TV_EPISODE, CONFIRMATION, COMPLETE).
   - `@dataclass SearchSession` fields: `step`, `media_type`, `title`, `resolved_title`, `season`, `episode`, `resolution`, `tv_scope`, `season_episode_count`, `existing_episodes`, etc., plus helper methods `advance(step)`, `set_title(...)`, `to_user_data()`, `from_user_data()`.
   - Validation helpers (e.g., `require_title()` raises a descriptive exception or returns a user-facing error string).
2. Update `handle_search_workflow` and `handle_search_buttons` to `session = SearchSession.from_user_data(context.user_data)` (create a new one on `/search` start).
3. Refactor each handler (`_handle_movie_title_reply`, `_handle_tv_season_reply`, `_handle_resolution_button`, `_perform_tv_season_search_with_resolution`, etc.) to read/write through the session instead of raw dict keys, and to call `session.advance(...)` when the step changes.
4. Replace `_clear_search_context` with a helper that simply removes `"search_session"`, and ensure cancellation handlers reset the session.
5. Extend tests (e.g., `tests/workflows/test_search_workflow.py` or add a new module) covering:
   - Session serialization/round-trips in user_data.
   - Guard clauses when a handler is invoked out of order (should surface a friendly error rather than crashing).

### Considerations
- Ensure the session object remains JSON-serializable (only use primitives/tuples) because `context.user_data` persists via PTB persistence.
- Keep backwards compatibility for any code paths that still check `"active_workflow"`; either store that flag in the session or add a shim that mirrors the old keys during the transition.

## Initiative 2 — Fast-Path Parsing for TV Queries (✅ Complete – 2025‑12‑13)
### Goals
- When the user includes a season/episode hint in the initial title message (`"The Bear S02E05"`, `"Severance season 1"`, `"Episode 3"`), skip redundant prompts and drive the workflow directly to the appropriate step.
- Maintain the existing guided flow for users who prefer step-by-step prompts.

### Implementation Steps
1. Add a parser utility (new function in `telegram_bot/utils.py` or `workflows/search_parser.py`) that extracts:
   - `SxxEyy` tokens (case-insensitive) → both season and episode.
   - `Season <n>` optionally followed by `Episode <m>`.
   - `Sxx` without episode (season-only fast path).
   - Trailing years (reuse existing regex) so movies keep working.
2. Call this parser from `_handle_tv_title_reply` before triggering any Wikipedia lookups:
   - If both season & episode are found, populate `session.season`, `session.episode`, set `session.tv_scope = "single"`, advance directly to `_prompt_for_resolution` using the composed `SxxEyy` title.
   - If only season is found, set `session.tv_scope = None` and ask only for scope selection (skip the explicit season prompt).
   - If only episode is found (rare), treat it as `season=1` default and still confirm with the user via a quick inline keyboard.
3. Update user-facing prompts to acknowledge detected context (e.g., "Detected Season 2 Episode 5 from your message. Want to continue with that?"). Provide a "Change" button that falls back to the manual flow.
4. Write unit tests covering the parser and the early-exit flow (mock `scraping_service` so tests remain offline).

### Considerations
- Ensure Markdown escaping still works when we reuse the user’s raw title.
- Watch for conflicts with movie detection—if the user enters "Dune Part 2", we must not misinterpret "2" as a season number. Prefer explicit prefixes (`S`, `Season`, `Episode`).

## Initiative 3 — Wikipedia Lookup Caching & Fallbacks (✅ Complete – 2025‑12‑13)
### Goals
- Avoid repeated HTTP requests to Wikipedia for the same title/season/episode data during a single bot process lifetime.
- Surface clearer messaging when `SEARCH_CONFIG` is absent (current logic silently disables the year picker).

### Implementation Steps
1. Introduce a lightweight async-safe cache layer inside `telegram_bot/services/scraping_service.py`:
   - A `WikiCache` object stored in `context.application_data` (or module-level) with TTL + max size.
   - Cache keys per fetch type: `("movie_years", title_lower)`, `("season_count", title_lower)`, `("episode_titles", title_lower, season)`.
2. Wrap the existing `fetch_movie_years_from_wikipedia`, `fetch_total_seasons_from_wikipedia`, `fetch_season_episode_count_from_wikipedia`, and `fetch_episode_titles_for_season` functions to check the cache before making HTTPX calls and to store successful responses (including `corrected_title`).
3. Provide instrumentation logs indicating cache hits/misses to help operators understand behavior.
4. When `SEARCH_CONFIG` is missing, fall back to cached data if available, otherwise immediately move to the preliminary search fallback with a user-visible notice ("Search configuration unavailable; skipping Wikipedia hints") instead of silently presenting no year options.
5. Tests:
   - Unit tests for `WikiCache` eviction/TTL behavior.
   - Integration-style tests ensuring the first call hits the network mock and subsequent calls return cached data without re-invoking the HTTP client.

### Considerations
- Respect existing exception handling—cache should store failures briefly (negative caching) to avoid spamming Wikipedia when a title does not exist.
- Keep cache size reasonable (e.g., 100 entries) and provide a helper to clear it for test isolation.

## Initiative 4 — Search Result Refinement & Pagination (✅ Complete – 2025‑12‑14)
### Goals
- Let users view more than five results and toggle filters (resolution, size, seeders) without re-scraping.
- Reduce re-run latency by keeping the aggregated results in memory and manipulating them via callback queries.

### Implementation Steps
1. Adjust `_present_search_results` to store the full unfiltered results list on the session (e.g., `session.results = [...]`) along with pagination metadata (`page_index`, `active_filter`). Reserve the filtered top-five view for initial presentation.
2. Define new callback prefixes:
   - `search_results_page_<n>` → paginate in chunks of five.
   - `search_results_filter_resolution_<1080p|720p|2160p|all>` → reuse `_filter_results_by_resolution` but without re-scraping.
   - `search_results_sort_seeders`, `..._size`, etc.
3. Update the inline keyboard builder to include "Next/Prev", "Toggle Resolution", and "Sort" buttons that mutate the stored filter state, rebuild the keyboard, and refresh the message in-place.
4. Ensure `search_results` entries retain the full metadata required for filtering (codec, size_gb, seeders, source) and guard against stale sessions (if the user waits too long, gracefully expire the results).
5. Tests:
   - Add workflow tests simulating pagination/filter callbacks.
   - Verify size filtering respects the global `MAX_TORRENT_SIZE_GB` but allows overrides (e.g., 4K movies) when the user selects the appropriate filter.

### Considerations
- Keep callback payloads short (Telegram limit) by encoding state ids rather than full JSON.
- When results expire, show a friendly message instructing the user to rerun `/search`.

## Suggested Rollout Order
1. Implement the SearchSession refactor (Initiative 1) to stabilize state management first.
2. Layer in the fast-path parser (Initiative 2) because it builds on the new session fields.
3. Add caching (Initiative 3) to reduce regression risk while iterating on UX features.
4. Deliver the result refinement UI (Initiative 4) once session storage can safely hold larger payloads.

Each milestone should land with targeted unit tests and manual smoke-testing (`/search` movie + TV flows) before moving to the next.

## Post-Refactor Enhancements (Messaging, Consistency, Collections)

### Initiative 5 - Harmonize Download Success Messaging
#### Goals
- Ensure the success toast sent after `handle_successful_download` mirrors the richer confirmation used in the delete workflow so users receive consistent, information-rich feedback.

#### Implementation Steps
1. Extract the `_format_item_line` concept from `telegram_bot/workflows/delete_workflow.py` into a shared helper (e.g., `telegram_bot/ui/messages.py::format_media_summary`) that accepts title, size, destination label, and optional icons.
2. In `telegram_bot/services/media_manager.py`, capture the final destination path + computed size (using `format_bytes`) when moving files and pass them to the helper alongside a new prefix such as "✅ *Successfully Added to Plex*".
3. Update both the single-file and season-pack branches to call the helper and append the Plex scan status, keeping MarkdownV2 escaping identical to the delete workflow.
4. Add regression tests (can live in `tests/services/test_media_manager.py`) that assert the helper output for representative movie/tv payloads, including verifying icon usage and escaped characters.
5. Update existing tests or snapshots that expect the previous "Renamed and moved" verbiage.

#### Considerations
- Preserve the ability to omit the Plex scan blurb when `plex_config` is missing while still using the unified format.
- Ensure the helper is safe to call from other workflows later (delete workflow can adopt it too for symmetry).

### Initiative 6 - Consistent Episode Selection When No Season Packs Exist
#### Goals
- When `_perform_tv_season_search_with_resolution` falls back to individual episodes, prefer torrents that are nearly identical in size (≈1 GB for 1080p/720p) and ideally come from the same uploader/seeder so the season is uniform.

#### Implementation Steps
1. Extend scraper result objects (if needed) to include a stable release identifier such as uploader/author or info hash. Update the YAML/HTML scrapers plus `search_logic.orchestrate_searches` to preserve these fields.
2. Modify the per-episode loop to collect the top N (e.g., 3) candidates per episode instead of only the first match, storing size, seeders, and uploader metadata.
3. Introduce a scoring routine that:
   - Builds clusters by `(source, uploader)` and measures variance of `size_gb`.
   - Rewards clusters whose average size is closest to the 1 GB target (or dynamically adjusts for 4K/2160p ~4–6 GB).
   - Penalizes episodes that require mixing uploaders.
4. After all episodes are processed, select the highest-scoring cluster and emit warnings for any episodes filled by fallback sources.
5. Update `_present_season_download_confirmation` to summarize the achieved consistency (e.g., "Episodes match uploader `SceneGroup` • avg size 0.98 GB").
6. Add tests that stub search results with multiple uploaders/sizes to ensure the scorer chooses the consistent set, plus regression tests covering 4K seasons where the acceptable average increases.

#### Considerations
- Keep a timeout/escape hatch so we still deliver results if no cluster meets the threshold; in that case, fall back to the current "best available" behavior but log the inconsistency.
- Ensure we still respect `season_missing_episode_numbers` so owned episodes are skipped during scoring.

### Initiative 7 - Movie Collection Workflow & Storage
#### Goals
- Add first-class support for downloading curated movie collections: allow the user to queue multiple individual movies under a collection name, store them under `movies_save_path/<Collection Name>/`, and automatically create/update the matching Plex collection.

#### Implementation Steps
1. UX updates in `search_workflow.py`:
   - Before the search begins, it must mimic the tv show path format of button options: single movie, collection, cancel
   - For single movie, nothing about the current workflow must change
   - If the user selects collection add prompts to capture the collection name (validate filesystem-safe) and desired number of movies. Allow the user to keep adding movies until they select "Finish Collection".
   - Track collection context inside the `SearchSession` (fields for `collection_name`, `collection_members`, `target_resolution`).
2. Enforce "no packs" by filtering movie search results to single-file torrents only; reuse `_filter_results_by_resolution` and add a size guard (≈2 GB target for 1080p, configurable upper bound for 4K such as 12 GB) with preference for torrents sharing the same uploader or media group.
3. Enhance `media_manager._get_final_destination_path` to detect when `parsed_info` contains `collection_name` and create `<movies_save_path>/<collection>/<Movie Title (Year)>`, ensuring names are sanitized.
4. Extend `media_manager.handle_successful_download` to accept a `collection_context` payload so it can place each movie correctly and optionally defer Plex scans until the collection run finishes.
5. Add Plex integration helpers in `telegram_bot/services/plex_service.py`:
   - `ensure_plex_collection(collection_name)` to create the collection if missing via Plex API.
   - `add_movie_to_collection(rating_key, collection_name)` to associate downloaded movies after the Plex scan (may require querying Plex for the new item by title/year).
6. After every collection download completes, trigger the Plex scan once, then call the helper to attach all members and send a summary message (list movies, sizes, and collection path).
7. Tests:
   - Workflow tests for the new collection prompts and state machine.
   - Service tests using PlexAPI mocks to confirm collection creation and membership calls.
   - Media manager tests verifying directories and metadata for collection downloads.

#### Considerations
- Respect existing quota/size guards; when the user selects 4K, widen the average-size threshold but still enforce an upper limit per movie to prevent bloated torrents.
- Decide how collection sessions expire (e.g., timeout after N minutes of inactivity) and ensure cancellation cleans up partial state.
- Document the new behavior in README/config templates once implemented.
