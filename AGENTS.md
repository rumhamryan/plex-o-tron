Here is a plan to implement the feature for downloading a whole season of a TV show. Follow these instructions to modify the existing codebase.

### Phase 1: Modify the User-Facing Search Workflow

**Objective:** Change the conversation flow to give the user the option to download a whole season after selecting a season number.

**File to Modify:** `telegram_bot/workflows/search_workflow.py`

1.  **Update `_handle_tv_season_reply` function:**
    *   Locate the `_handle_tv_season_reply` function.
    *   Instead of immediately asking for an episode number, modify it to present the user with two new inline buttons: "Single Episode" and "Entire Season".
    *   The message text should be updated to something like: "Season X selected. Do you want a single episode or the entire season?"
    *   The callback data for the buttons should be `search_tv_scope_single` and `search_tv_scope_season`.

2.  **Create a New Button Handler Function: `_handle_tv_scope_selection`:**
    *   Create a new asynchronous function `_handle_tv_scope_selection(query, context)`.
    *   This function will handle the callbacks from the new buttons.
    *   **If `query.data` is `search_tv_scope_single`:** Implement the original logic. Call `_send_prompt` to ask for the episode number and set `context.user_data["next_action"] = "search_tv_get_episode"`.
    *   **If `query.data` is `search_tv_scope_season`:** This will trigger the new season download logic. It should send a status message to the user, such as "Verifying season details on Wikipedia...", and then proceed to call the logic outlined in Phase 2.

**File to Modify:** `telegram_bot/handlers/callback_handlers.py`

1.  **Update `button_handler`:**
    *   Add a new `elif` condition to the router.
    *   The condition should check if `action.startswith("search_tv_scope_")`.
    *   If true, it should await the new `handle_search_buttons` function which will now contain the `_handle_tv_scope_selection` logic. *Correction*: Since `handle_search_buttons` already exists as a router, add the new `elif` block inside it to call `_handle_tv_scope_selection`.

---

### Phase 2: Implement Wikipedia Episode Count Verification

**Objective:** Scrape Wikipedia to determine the number of episodes in the selected season.

**File to Modify:** `telegram_bot/services/scraping_service.py`

1.  **Create `fetch_season_episode_count_from_wikipedia` function:**
    *   Define a new asynchronous function: `async def fetch_season_episode_count_from_wikipedia(show_title: str, season: int) -> int | None:`.
    *   Reuse the existing page-finding logic from `fetch_episode_title_from_wikipedia` to get the correct Wikipedia page (`"List of {show_title} episodes"` first, then fallback to the main show page).
    *   Parse the page's HTML with BeautifulSoup.
    *   Locate the "Series overview" table. Iterate through its rows to find the one corresponding to the `season` argument.
    *   In that row, find the cell corresponding to the "Episodes" column. This usually requires finding the header row first to get the correct column index.
    *   Extract the number from that cell, convert it to an integer, and return it.
    *   Return `None` if the page, table, or episode count cannot be found.

---

### Phase 3: Adapt Search and Selection Logic

**Objective:** Implement the core logic for searching for season packs or individual episodes and presenting the results.

**File to Modify:** `telegram_bot/workflows/search_workflow.py`

1.  **Implement the "Entire Season" Logic in `_handle_tv_scope_selection`:**
    *   When the user has chosen the "Entire Season" option:
        a.  Call the new `fetch_season_episode_count_from_wikipedia` function from `scraping_service`. If it returns `None`, inform the user that the episode count could not be verified and cancel the operation.
        b.  **Primary Strategy (Season Packs):** Perform a search by calling `search_logic.orchestrate_searches`. Use a query formatted to find season packs (e.g., f'{show_title} S{season:02d}', f'{show_title} Season {season}').
        c.  **Fallback Strategy (Individual Episodes):** If the primary strategy yields no high-quality season pack (define a threshold, e.g., fewer than 3 good results), initiate a fallback. Loop from `episode = 1` to the count retrieved from Wikipedia. In each iteration, call `search_logic.orchestrate_searches` with a query for the specific episode (e.g., f'{show_title} S{season:02d}E{episode:02d}'). From the results for each episode, programmatically select the single best torrent based on the existing scoring logic.
        d.  Store all the selected torrents (whether it's a single season pack or multiple individual episodes) in a list.

2.  **Create `_present_season_download_confirmation` function:**
    *   Create a new asynchronous function `_present_season_download_confirmation(message, context, found_torrents)`.
    *   This function will format a message to the user summarizing what was found. For example: "Found a season pack for Season 1." or "Found torrents for 10 of the 12 episodes in Season 1."
    *   The message should include "Confirm" and "Cancel" buttons.
    *   Store the list of magnet links/URLs for the `found_torrents` in `context.user_data['pending_season_download']`.
    *   The 'Confirm' button should have the callback data `confirm_season_download`.

---

### Phase 4: Implement Queuing for Multiple Downloads

**Objective:** Add all selected torrents for the season to the existing download queue.

**File to Modify:** `telegram_bot/services/download_manager.py`

1.  **Create `add_season_to_queue` function:**
    *   Define a new asynchronous function `async def add_season_to_queue(update, context):`.
    *   This function is triggered by the `confirm_season_download` callback.
    *   Retrieve the list of torrent links from `context.user_data.pop('pending_season_download', [])`.
    *   Loop through this list of links. In each iteration:
        i.  Construct the `download_data` dictionary exactly as it's done in the existing `add_download_to_queue` function.
        ii. Append this dictionary to the user's queue located at `context.bot_data["download_queues"][str(chat_id)]`.
    *   After the loop finishes, call `save_state(...)` to persist the updated queue.
    *   Call `await process_queue_for_user(...)` to kick off the download process if the queue is not already running.
    *   Edit the original message to give the user a final confirmation, e.g., "✅ Success! Added 10 episodes to your download queue."

**File to Modify:** `telegram_bot/handlers/callback_handlers.py`

1.  **Update `button_handler`:**
    *   Add an `elif` condition to route the `confirm_season_download` callback to the new `add_season_to_queue` function in `download_manager.py`.