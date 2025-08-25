### 1. Add Source Site to Download Options

When presenting download options from torrent sites, the name of the source site should be included.

-   **Step 1: Create a helper function.**
    -   **File to modify**: `telegram_bot/utils.py`
    -   **Change**: Add a new function `get_site_name_from_url(url: str) -> str`. This function will take a URL and extract a short, readable site name from it (e.g., `https://yts.mx/...` becomes `YTS`).

-   **Step 2: Use the helper function.**
    -   **File to modify**: `telegram_bot/services/search_logic.py`
    -   **Change**: In the function that formats torrent search results for display, call the new `get_site_name_from_url` helper for each result. Prepend the returned site name to the result string, for example: `[YTS] Movie Title...`. You will need to import the new function from `telegram_bot.utils`.


### 2. Do not overwrite cancel confirmation

-   **Goal**: Prevent the download status update message from overwriting the cancellation confirmation prompt.
-   **Strategy**: Introduce a state flag to temporarily pause status updates for a torrent when a cancellation is in progress.

-   **Step 1: Modify Cancellation Initiation**
    -   **File to modify**: `telegram_bot/services/download_manager.py`
    -   **Change**: In `handle_cancel_request`, when a user first clicks a "Cancel" button (for a specific torrent identified by `info_hash`), before sending the confirmation prompt, set a flag in `context.chat_data`.
    -   **Example**: `context.chat_data.setdefault('downloads', {})[info_hash]['cancellation_pending'] = True`

-   **Step 2: Modify Status Update Logic**
    -   **File to modify**: `telegram_bot/services/download_manager.py`
    -   **Change**: Inside `report`, before it edits the message with a status update, add a check for the `cancellation_pending` flag for that torrent.
    -   **Example**: `if context.chat_data.get('downloads', {}).get(info_hash, {}).get('cancellation_pending'): continue`

-   **Step 3: Clear the Flag on Resolution**
    -   **File to modify**: `telegram_bot/services/download_manager.py`
    -   **Change**: In `handle_cancel_request`, after the user makes a choice on the confirmation prompt ("Yes, cancel" or "No, keep"), ensure the `cancellation_pending` flag is removed from `context.chat_data` for that torrent, regardless of their choice. This will resume status updates.

### 3. Make Fuzzy Searching for Available Years More Precise

-   **Goal**: Improve the speed and accuracy of identifying the correct media item from search results, especially when multiple years are possible for a given title.
-   **Strategy**: Refactor the logic to first group search results by title similarity, and only then check for year ambiguity within the best-matching group. This avoids mixing years from unrelated search results.

-   **Step 1: Centralize and Refine Search Logic**
    -   **File to modify**: `telegram_bot/services/search_logic.py`
    -   **Change**: Create a new function, `group_and_filter_results(results: list[dict], query: str) -> dict`. This function will be responsible for the heavy lifting.
    -   **Inside `group_and_filter_results`**:
        1.  **Group by Title**: Use a fuzzy matching library (like `thefuzz`) to group torrents that refer to the same media item. The output should be a dictionary where keys are a canonical title (e.g., the most common variant) and values are a list of associated torrent results.
        2.  **Find Best Match**: Compare the user's original `query` against the canonical titles (the keys of your new dictionary) to find the best matching group.
        3.  **Return Structured Data**: The function should return a dictionary containing the best-matched group of results, and a list of unique years found *only within that group*.

-   **Step 2: Update the Search Workflow**
    -   **File to modify**: `telegram_bot/workflows/search_workflow.py`
    -   **Change**: In `_process_preliminary_results`, replace the existing logic for finding unique years.
    -   Instead, call the new `group_and_filter_results` function from `search_logic.py`.
    -   Based on the return from the new function:
        -   If only one year is returned, proceed to show the results.
        -   If multiple years are returned, use them to call `_prompt_for_year_selection`. This prompt is now much more accurate as it's based on a pre-filtered, relevant set of results.
