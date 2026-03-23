# Telegram Keyboard Layout Reference

This document catalogs the inline keyboard layouts currently used by the bot.

Notation:
- Rows are shown top-to-bottom.
- Buttons on the same row are shown left-to-right.
- Dynamic buttons are shown with placeholders like `<year>` or `<result label>`.

## Reuse Summary

There is some reuse, but most keyboards are still defined inline in the workflow that uses them.

Shared builders currently in use:
- `telegram_bot/ui/home_menu.py::build_home_menu_markup`
- `telegram_bot/workflows/search_workflow/tv_flow.py::_build_tv_scope_keyboard`
- `telegram_bot/workflows/search_workflow/results.py::_build_results_keyboard`
- `telegram_bot/workflows/search_workflow/preferences.py::_render_search_preferences_prompt`

Repeated pattern without a shared helper:
- Single-button cancel prompt: `[ ❌ Cancel ]`

## Home Menu

### 1. DM Home Menu
Used in:
- `telegram_bot/ui/home_menu.py::build_home_menu_markup`

#### Current

```text
[ Search ] [ Delete ]
[ Link   ] [ Status ]
[ Restart] [ Help   ]
```

## Top-Level Workflow Launchers

### 2. Search Launcher
Used in:
- `telegram_bot/handlers/command_handlers.py::launch_search_workflow`

#### Current

```text
[ 🎬 Movie ] [ 📺 TV Show ]
[ ❌ Cancel ]
```

### 3. Delete Launcher
Used in:
- `telegram_bot/handlers/command_handlers.py::launch_delete_workflow`

#### Current

```text
[ 🎬 Movie ] [ 📺 TV Show ]
[ ❌ Cancel ]
```

### 4. Link Intake Prompt
Used in:
- `telegram_bot/handlers/command_handlers.py::launch_link_workflow`

#### Current

```text
[ ❌ Cancel ]
```

## Download and Link Confirmation

### 5. Torrent Download Confirmation
Used in:
- `telegram_bot/ui/views.py::send_confirmation_prompt`

#### Current

```text
[ ✅ Confirm Download ] [ ❌ Cancel ]
```

### 6. Magnet Choice List
Used in:
- `telegram_bot/services/torrent_service/input_handlers.py::_handle_webpage_url`

#### Current

```text
[ <resolution | file type | size> ]
[ <resolution | file type | size> ]
[ <resolution | file type | size> ]
[ ... ]
[ ❌ Cancel ]
```

## Search Workflow

### 7. Search Movie Scope
Used in:
- `telegram_bot/workflows/search_workflow/handlers.py::_handle_start_button`

#### Current

```text
[ Single Movie ]
[ Collection   ]
[ ❌ Cancel    ]
```

### 8. Search Cancel-Only Prompt
Used in:
- `telegram_bot/workflows/search_workflow/handlers.py::_handle_start_button`
- `telegram_bot/workflows/search_workflow/handlers.py::_handle_movie_scope_button`
- `telegram_bot/workflows/search_workflow/state.py::_send_prompt`
- `telegram_bot/workflows/search_workflow/movie_collection_flow.py::_handle_collection_accept` failure path
- other search prompts that only need a cancel action

#### Current

```text
[ ❌ Cancel ]
```

### 9. Movie Year Picker
Used in:
- `telegram_bot/workflows/search_workflow/movie_flow.py::_prompt_for_year_selection`

#### Current

```text
[ <year> ]
[ <year> ]
[ <year> ]
[ ...    ]
[ ❌ Cancel ]
```

### 10. Movie Resolution Picker
Used in:
- `telegram_bot/workflows/search_workflow/movie_flow.py::_prompt_for_resolution`

#### Current

```text
[ 🪙 1080p ] [ 💎 4K (2160p) ]
[ ❌ Cancel ]
```

### 11. TV Single-Episode Resolution Picker
Used in:
- `telegram_bot/workflows/search_workflow/movie_flow.py::_prompt_for_resolution`

#### Current

```text
[ 💎 1080p ] [ 💩 720p ]
[ 🔄️ Change ]   optional
[ ❌ Cancel ]
```

### 12. TV Scope Picker
Used in:
- `telegram_bot/workflows/search_workflow/tv_flow.py::_build_tv_scope_keyboard`
- invoked from `tv_flow.py` and `handlers.py`

#### Current

```text
[ Single Episode ] [ Entire Season ]
[ Change ]   optional
[ ❌ Cancel ]
```

### 13. TV Season Grid
Used in:
- `telegram_bot/workflows/search_workflow/tv_flow.py::_prompt_for_tv_season_selection`

#### Current

Grid rules:
- 4 columns
- up to 40 season buttons before falling back to text entry

```text
[ 1 ] [ 2 ] [ 3 ] [ 4 ]
[ 5 ] [ 6 ] [ 7 ] [ 8 ]
[ ...                 ]
[ ❌ Cancel ]
```

### 14. TV Episode Grid
Used in:
- `telegram_bot/workflows/search_workflow/tv_flow.py::_handle_tv_scope_selection`

#### Current

Grid rules:
- 4 columns
- up to 40 episode buttons before falling back to text entry

```text
[ 1 ] [ 2 ] [ 3 ] [ 4 ]
[ 5 ] [ 6 ] [ 7 ] [ 8 ]
[ ...                 ]
[ ❌ Cancel ]
```

### 15. TV Season Preferences
Used in:
- `telegram_bot/workflows/search_workflow/tv_flow.py::_prompt_tv_season_preferences`
- rendered by `telegram_bot/workflows/search_workflow/preferences.py::_render_search_preferences_prompt`

#### Current

Current option rows:

```text
[ 720p ] [ 1080p ]
[ x264 / AVC ] [ x265 / HEVC ]
[ ➡️ Search ]
[ ❌ Cancel ]
```

Notes:
- The selected option gets a `🟢` prefix.
- This is the main reusable preference keyboard builder in the search workflow.

### 16. Search Results Browser
Used in:
- `telegram_bot/workflows/search_workflow/results.py::_build_results_keyboard`

#### Current

```text
[ <result 1> ]
[ <result 2> ]
[ <result 3> ]
[ ...        ]
[ < Prev ] [ Next > ]   optional
[ All ] [ 720p ] [ 1080p ] [ 2160p ]
[ All ] [ x264 ] [ x265 ]
[ 🔄️ Change ]   optional for TV single-episode results
[ ❌ Cancel ]
```

Notes:
- Active resolution and codec filters get a `🟢` prefix.
- Resolution row contents depend on what filters are allowed for the current session.

### 17. Collection Use Prompt
Used in:
- `telegram_bot/workflows/search_workflow/movie_collection_flow.py::_prompt_collection_confirmation`

#### Current

```text
[ Use Collection ]
[ ❌ Cancel      ]
```

### 18. Collection Movie Picker
Used in:
- `telegram_bot/workflows/search_workflow/movie_collection_flow.py::_render_collection_movie_picker`

#### Current

```text
[ <movie label with status prefix> ]
[ <movie label with status prefix> ]
[ <movie label with status prefix> ]
[ ...                           ]
[ ✅ Confirm Selection ] or [ ✅ Continue ]   optional
[ ❌ Cancel ]
```

Status prefixes currently used:
- `🟢` ready to download
- `🔴` excluded from this run
- `📁` already in collection folder
- `📦` found elsewhere in library
- `⏳` already queued
- `⚠️` ambiguous / needs manual review

### 19. Collection Final Confirmation
Used in:
- `telegram_bot/workflows/search_workflow/movie_collection_flow.py::_present_collection_download_confirmation`

#### Current

```text
[ ✅ Confirm Collection ]
[ ❌ Cancel            ]
```

### 20. Season Download Confirmation
Used in:
- `telegram_bot/workflows/search_workflow/tv_flow.py::_present_season_download_confirmation`

#### Current

Pack variant:

```text
[ ✅ Confirm ] [ ⛔ Reject ] [ ❌ Cancel ]
```

Single-confirm variant:

```text
[ ✅ Confirm ] [ ❌ Cancel ]
```

## Delete Workflow

### 21. Delete Movie Scope
Used in:
- `telegram_bot/workflows/delete_workflow/handlers.py::_handle_start_buttons`

#### Current

```text
[ 🗂️ Collection ] [ 📄 Single File ] [ ❌ Cancel ]
```

### 22. Delete Cancel-Only Prompt
Used in:
- `telegram_bot/workflows/delete_workflow/handlers.py::_handle_movie_type_buttons`
- `telegram_bot/workflows/delete_workflow/handlers.py::_handle_tv_scope_buttons`
- episode/season text prompts in delete flow

#### Current

```text
[ ❌ Cancel ]
```

### 23. Delete TV Scope
Used in:
- `telegram_bot/workflows/delete_workflow/handlers.py` after a TV show is found

#### Current

```text
[ 🗑️ All    ]
[ 💿 Season ]
[ ▶️ Episode ]
[ ❌ Cancel ]
```

### 24. Delete Single-Match Confirmation
Used in:
- `telegram_bot/workflows/delete_workflow/selection.py::_present_delete_results`
- `telegram_bot/workflows/delete_workflow/handlers.py::_handle_selection_button`

#### Current

```text
[ ✅ Yes, Delete It ] [ ❌ No, Cancel ]
```

### 25. Delete Entire-TV-Show Confirmation
Used in:
- `telegram_bot/workflows/delete_workflow/handlers.py::_handle_tv_scope_buttons`

#### Current

```text
[ ✅ Yes, Delete All ] [ ❌ No, Cancel ]
```

### 26. Delete Multiple-Match Picker
Used in:
- `telegram_bot/workflows/delete_workflow/selection.py::_present_delete_results`

#### Current

```text
[ <match label> ]
[ <match label> ]
[ <match label> ]
[ ...          ]
[ ❌ Cancel ]
```

## Download Manager Controls

### 27. Initial Download Controls
Used in:
- `telegram_bot/services/download_manager/queue.py::_start_download_task`

#### Current

```text
[ ⏹️ Cancel Download ] [ 🛑 Stop ]   stop button only when a queue exists
```

### 28. Active Download Progress Controls
Used in:
- `telegram_bot/services/download_manager/progress.py::ProgressReporter.report`

#### Current

Paused variant:

```text
[ ▶️ Resume ] [ ⏹️ Cancel ] [ 🛑 Stop ]   stop button optional
```

Running variant:

```text
[ ⏸️ Pause ] [ ⏹️ Cancel ] [ 🛑 Stop ]   stop button optional
```

### 29. Cancel Current Download Confirmation
Used in:
- `telegram_bot/services/download_manager/controls.py::handle_cancel_request`

#### Current

```text
[ ✅ Yes, Cancel ] [ ❌ No, Continue ]
```

### 30. Cancel All Confirmation
Used in:
- `telegram_bot/services/download_manager/controls.py::handle_cancel_all`

#### Current

```text
[ ✅ Yes, Cancel All ] [ ❌ No, Continue ]
```

## Observations

- The most reused keyboard in the codebase is effectively the cancel-only prompt, but it is duplicated instead of centralized.
- Search has a few genuine keyboard builders.
- Delete is still mostly inline keyboard construction.
- Download controls are partially centralized by shared button label constants, but not by shared layout builders.
- Several layouts differ only slightly and could be normalized if you want a smaller, more maintainable keyboard surface area.
