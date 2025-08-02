ToDo:

# **Project: `search` Command Integration**

**Objective:** To implement a new `/search` command that allows users to find and download movies and TV shows by searching configured torrent websites, filtering the results based on user-defined preferences, and presenting the best options for download. Python 3.11.8 environment.

---

### **Phase 1: The Skeleton - Command & User Workflow**

**Goal:** Establish the basic user-facing command and conversation flow. This phase involves no actual searching; it's about building the interface and state management.

**Actions:**

1.  **Modify `telegram_bot.py`:**
    *   Create a new command handler function `search_command(update, context)` that responds to `/search`.
        *   This function will not accept arguments. It will delete the user's command and send the initial prompt.
        *   It will display two inline buttons: `[🎬 Movie]` and `[📺 TV Show]`, along with a `[❌ Cancel]` button.
    *   Create a new callback query handler function, or extend the main `button_handler`, to process the "Movie" or "TV Show" selection.
        *   When a selection is made, it will edit the message to ask the user for the search query (e.g., "🎬 Please send me the title of the movie to search for.").
        *   It will set a flag in `context.user_data`, such as `{'next_action': 'search_movie_title'}`.
    *   Create a new message handler function `handle_search_workflow(update, context)` that triggers when `next_action` is set.
        *   This function will receive the user's text (the movie/TV show title).
        *   For this phase, it will simply respond with a placeholder message like, "Searching for `{user_text}`..." and then the process for this phase will end.
    *   Register the new handlers in the `main` execution block.

**Files to Modify:**
*   `telegram_bot.py`

**Validation at End of Phase 1:**
*   The bot administrator can type `/search`.
*   The bot replies with "Movie" and "TV Show" buttons.
*   Clicking "Movie" prompts for a movie title.
*   Replying with "The Matrix" results in the bot sending a "Searching for The Matrix..." message.
*   The "Cancel" button works at every stage to exit the workflow.

---

### **Phase 2: Configuration - Loading Search Parameters**

**Goal:** Integrate the new search configurations into the bot's startup process, making them available for later phases.

**Actions:**

1.  **Modify `config.ini`:**
    *   Add a new section `[search]`.
    *   Inside this section, add two keys: `websites` and `preferences`.
    *   The values for these keys will be JSON-formatted strings, which allows for complex nested data structures within the INI file format.

    ```ini
    [search]
    websites = {
        "movies": [
            {"name": "YTS.mx", "search_url": "https://yts.mx/browse-movies/{query}/all/all/0/latest/0/all"},
            {"name": "1337x", "search_url": "https://1337x.to/search/{query}/1/"}
        ],
        "tv": [
            {"name": "EZTVx.to", "search_url": "https://eztvx.to/search/{query}"},
            {"name": "1337x", "search_url": "https://1337x.to/category-search/{query}/TV/1/"}
        ]
    }

    preferences = {
        "resolutions": {
            "4k": 5,
            "2160p": 5,
            "1080p": 3,
            "720p": 1
        },
        "codecs": {
            "x265": 2,
            "hevc": 2,
            "x264": 1
        },
        "uploaders": {
            "EZTV": 5,
            "MeGusta": 5
        }
    }
    ```

2.  **Modify `telegram_bot.py`:**
    *   Update the `get_configuration()` function to parse the new `[search]` section.
    *   Use `config.get('search', 'websites', fallback='{}')` to read the values.
    *   Use the `json.loads()` function to parse the JSON strings into Python dictionaries.
    *   Store the parsed dictionaries in `application.bot_data` (e.g., `application.bot_data['SEARCH_CONFIG']`).
    *   Add logging to confirm that the search configuration was loaded successfully or if there was a parsing error.

**Files to Modify:**
*   `config.ini`
*   `telegram_bot.py`

**Validation at End of Phase 2:**
*   When the bot starts, the console logs show a message confirming "Search configuration loaded successfully."
*   If the JSON in the config is malformed, the bot logs an error and exits gracefully.

---

### **Phase 3: The First Scraper - A Proof of Concept**

**Goal:** Implement the logic to scrape a single, relatively simple website (YTS.mx) and return raw, unranked results. This proves the core scraping mechanism works.

**Actions:**

1.  **Modify `telegram_bot.py`:**
    *   Create a new `async` helper function: `_scrape_yts(query: str, context: ContextTypes.DEFAULT_TYPE) -> list`.
        *   This function will use `httpx` to make a web request to the YTS search URL stored in the config.
        *   It will use `BeautifulSoup` to parse the returned HTML.
        *   It will loop through the movie listings on the page, extracting the **title, year, and magnet link** for each.
        *   It will return a list of dictionaries, e.g., `[{'name': 'The Matrix (1999)', 'magnet': 'magnet:...', 'source': 'YTS.mx'}]`.
    *   Create a primary orchestrator function: `_search_for_media(query: str, media_type: str, context: ContextTypes.DEFAULT_TYPE, site_index: int = 0)`.
        *   This function will retrieve the list of websites for the given `media_type` from the search config.
        *   For now, it will only call `_scrape_yts` if the site name matches.
    *   Connect this orchestrator to the `handle_search_workflow` function created in Phase 1. Instead of a placeholder, it will now call `_search_for_media` and present the raw results to the user as simple text.

**Files to Modify:**
*   `telegram_bot.py`

**Validation at End of Phase 3:**
*   Using the `/search` command for a movie like "The Matrix" now returns a list of actual (but unscored) torrent names found on YTS.
*   If no results are found, it correctly states, "No results found on YTS.mx."

---

### **Phase 4: Scoring, Ranking, and Presentation**

**Goal:** Implement the preference-based scoring system to rank the scraped results and present them to the user in the polished, interactive UI.

**Actions:**

1.  **Modify `telegram_bot.py`:**
    *   Create a new helper function `_score_and_rank_results(results: list, context: ContextTypes.DEFAULT_TYPE) -> list`.
        *   This function will iterate through the list of dictionaries from the scraper.
        *   For each result, it will parse the name and assign points based on the `preferences` dictionary loaded from the config (resolution, codec).
        *   It will sort the list of results in descending order based on their calculated score.
    *   **Crucially, implement the "don't only show 4K" rule:** After sorting, if the top result has a 4K score, check if a 1080p result exists within the top 5-7 results. If so, ensure it's included in the final list, even if it pushes out a slightly higher-scoring 4K result. This guarantees variety.
    *   Update `_search_for_media` to pass the scraped results through this new scoring function.
    *   Use the existing multi-magnet UI pattern to display the top 4 results. Each result will be a button with text like `[1080p | x265 | YTS.mx]`. The size will be added later when the magnet metadata is fetched.
    *   The callback data for each button will contain the magnet link. When a user clicks a result, the flow is handed off to the existing `fetch_metadata_from_magnet` -> `validate_and_enrich_torrent` -> `send_confirmation_prompt` workflow.

**Files to Modify:**
*   `telegram_bot.py`

**Validation at End of Phase 4:**
*   Searching for a popular movie returns up to 4 interactive buttons.
*   The results are logically ranked (e.g., a 1080p x265 version appears above a 720p x264 version).
*   Selecting an option correctly proceeds to the final confirmation and download screen.

---

### **Phase 5: The Hybrid Search Workflow**

**Goal:** Implement the "Search Next Site" button to allow the user to reject the current site's results and continue searching.

**Actions:**

1.  **Modify `telegram_bot.py`:**
    *   Update the result presentation logic. If there are more websites to search in the config for the given media type, add a `[➡️ Search Next Site]` button below the results.
    *   The callback data for this button will be `search_next_{next_site_index}`.
    *   Update `button_handler` to catch this new callback.
    *   When pressed, the handler will extract the `next_site_index` and re-invoke the `_search_for_media` orchestrator, passing in the original query (which must be stored in `context.user_data`) and the new `site_index`.

**Files to Modify:**
*   `telegram_bot.py`

**Validation at End of Phase 5:**
*   A search for a movie shows results from YTS.mx and a "Search Next Site" button.
*   Clicking the button edits the message to "Searching 1337x..." (even if the scraper for it doesn't exist yet). The workflow is the key part to validate here.

---

### **Phase 6: Expansion and Completion**

**Goal:** Prove the system is modular and complete the initial feature set by adding the scraper for 1337x and handling the TV show logic.

**Actions:**

1.  **Modify `telegram_bot.py`:**
    *   Create a new `async` scraper function: `_scrape_1337x(query: str, media_type: str, context: ContextTypes.DEFAULT_TYPE) -> list`. This will require its own unique parsing logic, as its HTML structure is different from YTS. It will need to handle both movie and TV show search pages.
    *   Update the `_search_for_media` orchestrator to be a router. It will check the `site['name']` for the current `site_index` and call the appropriate scraper function (`_scrape_yts` or `_scrape_1337x`).
    *   Implement the logic to handle year ambiguity. If multiple results with the same title but different years are found, the bot will present buttons for each year, asking the user to clarify before proceeding with the search on that specific entry.
    *   Fully test the TV show search path from start to finish.

**Files to Modify:**
*   `telegram_bot.py`

**Validation at End of Phase 6:**
*   The entire `search` feature is complete.
*   Searching for a movie shows results from YTS. Clicking "Search Next Site" shows new, ranked results from 1337x.
*   Searching for a TV show correctly searches the TV-specific sites and returns ranked results.
*   The bot handles ambiguous movie years by prompting the user for clarification.
*   The final selection of any item leads to the established and stable download workflow.