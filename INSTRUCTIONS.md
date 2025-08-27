### **Project Plan: Implement "Top N" Optimization for Generic Scraper**

**Objective:** Refactor the `GenericTorrentScraper` to significantly improve performance by avoiding the processing of low-quality torrents. The scraper will be modified to find a limited number of the best torrents ("Top N") instead of processing every result on a page.

---

### **Phase 1: Enhance Configuration for Granular Parsing**

**Goal:** Update the scraper's YAML configuration to provide the specific CSS selectors needed for the new, more targeted parsing logic.

1.  **Locate the Target Configuration File:** Open the relevant YAML configuration file for the website you are optimizing (e.g., `1337x.yaml`).

2.  **Add Detailed Selectors:** Under the `selectors` key, add new entries that allow the scraper to precisely target individual pieces of data within a result row. This is crucial for the new logic, which needs to quickly extract a preliminary score (seeders) before deciding to parse the rest of the row.

3.  **Example `1337x.yaml` Implementation:**
    ```yaml
    # In file: telegram_bot/scrapers/configs/1337x.yaml

    # ... (existing configuration) ...
    selectors:
      # This selector should identify the container holding all search results.
      results_container: 'table.table-list tbody'

      # This selector should identify a single torrent row within the container.
      result_row: 'tr'

      # --- Add the following new, granular selectors ---

      # Selector for the torrent name/title within a row.
      name: 'td.name a:nth-of-type(2)'

      # Selector for the link to the details page within a row.
      magnet: 'td.name a[href^="/torrent/"]'

      # Selector for the seeder count within a row. THIS IS THE MOST IMPORTANT ONE.
      seeders: 'td.seeds'

      # Selector for the size within a row.
      size: 'td.size'

      # Selector for the uploader within a row.
      uploader: 'td.uploader a'
    ```

#### **Verification Steps:**

1.  **Manual Selector Validation:**
    *   Open a web browser and navigate to a search results page on the target website (e.g., 1337x).
    *   Open the browser's Developer Tools (usually by pressing F12).
    *   Go to the "Console" tab.
    *   For each new selector you added to the YAML file, test it using `document.querySelectorAll()`. For example, type `document.querySelectorAll('td.seeds')` into the console and press Enter.
    *   **Confirm** that the command returns a list of elements and that the content of these elements matches the data you expect (e.g., the seeder counts on the page).
    *   **Verify** that the `results_container` selector returns exactly one element.

---

### **Phase 2: Implement the Core Optimization Logic**

**Goal:** Create the new methods within the `GenericTorrentScraper` class that perform the efficient "Top N" selection.

1.  **Locate the Scraper Class:** Open the file `generic_torrent_scraper.py` (or the file containing the `GenericTorrentScraper` class).

2.  **Create a Row-Parsing Helper Method (`_extract_data_from_row`):** This private method is responsible for the detailed parsing of a *single* HTML row.

3.  **Implement the Main "Top N" Selection Method (`_parse_and_select_top_results`):** This primary method iterates through all rows but only calls the expensive `_extract_data_from_row` for high-quality torrents.

    *(Refer to the previous response for the full code of these methods.)*

#### **Verification Steps:**

1.  **Unit Test `_extract_data_from_row`:**
    *   Create a temporary test script or a formal unit test.
    *   Save a sample HTML `<tr>...</tr>` element from the target site's search results into a string.
    *   Use BeautifulSoup to parse this string into a `Tag` object.
    *   Instantiate your `GenericTorrentScraper` (it may require a mock config).
    *   Call `scraper._extract_data_from_row()` with the sample `Tag`.
    *   **Assert** that the method returns a dictionary containing the correctly parsed data (title, seeders, etc.).
    *   **Assert** that the method returns `None` if you pass it a malformed or irrelevant row tag.

2.  **Unit Test `_parse_and_select_top_results`:**
    *   Save a larger block of HTML (the entire `<tbody>...</tbody>` content) into a file or a multi-line string.
    *   In a test script, parse this HTML into a `Tag` object representing the `search_area`.
    *   Call `scraper._parse_and_select_top_results(search_area, limit=5)`.
    *   **Assert** that the returned list has a length of exactly 5.
    *   **Assert** that the first item in the list has the highest seeder count from your sample HTML, and the last item has the fifth-highest seeder count. The list must be sorted correctly.

---

### **Phase 3: Integrate New Logic into the Main `search` Method**

**Goal:** Modify the existing `search` method to use the new, efficient logic instead of the old approach.

1.  **Locate the `search` Method:** In the `GenericTorrentScraper` class, find the `async def search(...)` method.

2.  **Refactor the Method:** Replace the existing parsing loop with a call to your new `_parse_and_select_top_results` method. Add a `limit` parameter to the method signature.

    *(Refer to the previous response for the full refactoring code.)*

#### **Verification Steps:**

1.  **Integration Test `search`:**
    *   Modify your test script to now call the public `scraper.search()` method.
    *   You may need to use a library like `pytest-asyncio` to test the `async` method.
    *   Mock the network call (`_fetch_page`) to return your saved sample HTML instead of making a real web request.
    *   Call `await scraper.search(query="test", media_type="movie", limit=10)`.
    *   **Assert** that the final returned list contains no more than 10 items.
    *   **Assert** that the results are the highest-quality ones from your sample data, demonstrating that the entire internal pipeline is working correctly.

---

### **Phase 4: Update the High-Level Scraper Invocation**

**Goal:** Ensure the top-level function that calls the generic scraper passes the new `limit` parameter.

1.  **Locate the Calling Function:** Open `telegram_bot/services/scraping_service.py` and find the `scrape_1337x` function.

2.  **Pass the `limit` Argument:** When you call `scraper.search`, add the `limit` argument.

    *(Refer to the previous response for the full code example.)*

#### **Verification Steps:**

1.  **Confirm Parameter Propagation:**
    *   Temporarily add a `print()` or `logger.info()` statement inside the `GenericTorrentScraper.search()` method to display the value of the `limit` parameter it receives.
    *   Run the application and perform a search.
    *   **Observe** the console/log output.
    *   **Confirm** that the log message shows the correct limit value (e.g., `15`) that was passed from `scrape_1337x`. This verifies the parameter is being passed through the layers correctly.
    *   Remove the temporary log statement once verified.

---

### **Phase 5: Final End-to-End Verification**

**Goal:** Confirm that the complete, integrated optimization is working as expected in the live application.

1.  **Run a Test Search:** Execute a search in your Telegram bot for a popular item that is known to have many results on the target site.
2.  **Check the Logs:** Examine the application's log output. You should see your final log message: `"[SCRAPER] 1337x: Efficiently processed top X torrents..."`, where `X` is at most the limit you set (e.g., 15).
3.  **Observe Performance:** The time taken for the search operation should be noticeably faster than before the changes.
4.  **Validate UI Results:** Confirm that the bot presents the user with the expected number of choices (e.g., 5) and that these choices correspond to the highest-quality torrents (highest seeder counts) from the website.
