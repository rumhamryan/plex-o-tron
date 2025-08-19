# Refactoring Plan: Universal Scraper Implementation

This document outlines the multi-phase plan to integrate the new generic scraper, unify the search logic, and deprecate redundant, site-specific scrapers. Each phase is designed to be implemented and tested independently.

---

### Phase 1: Integrate the Generic Scraper as a Fallback

**Goal:** Modify the search orchestrator to use the `scrape_generic_page` function for any configured site that does not have a dedicated scraper function. This will immediately enable scraping for sites like `EZTVx.to`.

**Implementation Steps:**

1.  **Modify `telegram_bot/services/search_logic.py`:**
    *   Import `urllib.parse` at the top of the file.
    *   Locate the `for` loop inside the `orchestrate_searches` function.
    *   Find the `else` block that currently logs the warning: `Configured site '{site_name}' has no corresponding scraper function`.
    *   Modify this block to do the following:
        *   Log an informational message that it's using the generic scraper as a fallback.
        *   Assign `scraping_service.scrape_generic_page` to a `scraper_func` variable.
        *   The generic scraper expects a fully-formed URL. Create the final `search_url` by replacing the `{query}` placeholder in the `site_url` template with the URL-encoded `search_query`.
        *   Create the `asyncio` task. Note that `scrape_generic_page` has a different signature than the dedicated scrapers. You will need to call it with only the arguments it requires (`query`, `media_type`, `search_url`).

**Testing & Verification:**

*   Ensure `EZTVx.to` is enabled in your `config.ini`.
*   Run a search that includes this site.
*   **Expected Outcome:** The log should no longer show the `[WARNING] ... has no corresponding scraper function` message for `EZTVx.to`. Instead, it should indicate that the generic scraper is being used. The search results may be minimal or empty, as the generic scraper is not yet fully featured. The key is that it runs without error.

---

### Phase 2: Enhance the Generic Scraper and Unify Result Formatting

**Goal:** Improve the `scrape_generic_page` function so that it returns a rich list of torrent data (including title, seeders, size, uploader, score, etc.), matching the format produced by the dedicated scrapers.

**Implementation Steps:**

1.  **Modify `telegram_bot/services/scraping_service.py`:**
    *   Update the generic scraping strategies (`_strategy_find_in_tables`, `_strategy_contextual_search`) to extract more than just a link. They should attempt to find the torrent title, seeders, leechers, and size from the same HTML element (e.g., a `<tr>` table row).
    *   The strategies should return a list of dictionaries, with each dictionary containing the raw scraped data for a potential torrent.
    *   In `scrape_generic_page`, iterate through the raw data returned by the strategies.
    *   For each item, use the existing helper functions (`_parse_size_to_gb`, `_parse_codec`, `parse_torrent_name`) to clean the data.
    *   Use the `score_torrent_result` function to calculate a score for each torrent based on user preferences.
    *   The function should now return a list of fully-formed result dictionaries, identical in structure to those from `scrape_1337x`.

**Testing & Verification:**

*   Run a search against `EZTVx.to` again.
*   **Expected Outcome:** The search should now return a list of properly formatted and scored torrents from `EZTVx.to`. The results should appear in the final sorted list alongside results from other sites like `1337x`.

---

### Phase 3: Deprecate the Dedicated `scrape_1337x` Scraper

**Goal:** With a powerful and reliable generic scraper in place, remove the now-redundant `scrape_1337x` function and have `1337x` use the generic logic.

**Implementation Steps:**

1.  **Modify `telegram_bot/services/search_logic.py`:**
    *   In the `scraper_map` dictionary, delete the line: `"1337x": scraping_service.scrape_1337x,`.
    *   Remove any special-case logic in `orchestrate_searches` that was specific to `1337x` (e.g., the `if site_name == "1337x" and year:` block). The generic scraper should be robust enough to handle this.

2.  **Modify `telegram_bot/services/scraping_service.py`:**
    *   Delete the entire `scrape_1337x` function.

**Testing & Verification:**

*   Run a search for a movie or TV show using `1337x`.
*   **Expected Outcome:** The search should execute successfully. The logs will show that the generic scraper is being used for `1337x`. The results should be of similar quality and quantity to what the dedicated scraper previously provided.

---

### Phase 4: Final Cleanup and Strategy Review

**Goal:** Solidify the new architecture and acknowledge the role of specialized scrapers for non-standard sources (like APIs).

**Implementation Steps:**

1.  **Review `telegram_bot/services/search_logic.py`:**
    *   The `scraper_map` should now only contain scrapers for sites that cannot be handled by the generic HTML scraper. The `scrape_yts` function is a perfect example, as it relies on a JSON API, not HTML parsing.
    *   The final `scraper_map` should look like this:
      ```python
      scraper_map: dict[str, ScraperFunction] = {
          "YTS.mx": scraping_service.scrape_yts,
      }
      ```
    *   Ensure the logic correctly routes `YTS.mx` to its dedicated function and all other configured sites (like `1337x`, `EZTVx.to`, etc.) to the generic fallback.

2.  **Code Cleanup:**
    *   Review all related files for any dead code or comments that referred to the old `scrape_1337x` function and remove them.

**Testing & Verification:**

*   Run a comprehensive search that targets both a movie (which should use YTS) and a TV show (which should use 1337x/EZTVx).
*   **Expected Outcome:** The bot should correctly use the `scrape_yts` function for the movie search and the `scrape_generic_page` function for the TV show search. All results should be aggregated and sorted correctly. The system is now more maintainable and easier to extend with new HTML-based torrent sites.