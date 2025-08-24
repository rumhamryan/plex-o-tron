

### 1. Fix Improperly Formatted Movie Titles

Some movie titles are displayed with extra parentheses, e.g., `Superman ( (2025)`. This needs to be corrected.

-   **File to modify**: `telegram_bot/workflows/delete_workflow.py`
-   **Change**: In the `format_media_html` function, which prepares media titles for display, add a line to clean up the title string. A simple string replacement to convert ` ( (` to ` (` should resolve the issue.

### 2. Add Source Site to Download Options

When presenting download options from torrent sites, the name of the source site should be included.

-   **Step 1: Create a helper function.**
    -   **File to modify**: `telegram_bot/utils.py`
    -   **Change**: Add a new function `get_site_name_from_url(url: str) -> str`. This function will take a URL and extract a short, readable site name from it (e.g., `https://yts.mx/...` becomes `YTS`).

-   **Step 2: Use the helper function.**
    -   **File to modify**: `telegram_bot/services/search_logic.py`
    -   **Change**: In the function that formats torrent search results for display, call the new `get_site_name_from_url` helper for each result. Prepend the returned site name to the result string, for example: `[YTS] Movie Title...`. You will need to import the new function from `telegram_bot.utils`.
