# Plan to Fix TV Show Episode Title Scraping

## Problem:
The `fetch_episode_title_from_wikipedia` function is returning an incorrect numerical value instead of the actual episode title, particularly when relying on the "Flexible Row Search" (Strategy 4). This indicates issues with both the title extraction in Strategy 4 and the failure of more specific strategies (1, 2, 3) to correctly parse dedicated episode pages.

---

### **Phase 1: Diagnose and Refine Title Extraction in `_parse_embedded_episode_table` (Strategy 4)**

**Objective:** Understand why Strategy 4 is extracting a number instead of the episode title and implement a robust fix.

1.  **Analyze Current Logic:**
    *   Examine the `_parse_embedded_episode_table` function in `telegram_bot/services/scraping_service.py`.
    *   Focus on the lines where `cell_text` is extracted from `cells[0]` and where `found_text` and `cleaned_title` are derived from `title_cell`.
    *   Specifically, investigate the regex `r"\"([^\"]+)\""` and the fallback `title_cell.get_text(strip=True)`. It\'s likely that when the regex fails to find quoted text, the fallback is inadvertently picking up numerical data from the `title_cell` or an adjacent cell.

2.  **HTML Inspection for Problematic Pages:**
    *   Identify a specific Wikipedia page (e.g., "List of South Park episodes") that exhibits this behavior.
    *   Manually inspect the HTML structure of the episode tables on such pages. Pay close attention to:
        *   The `<td>` or `<th>` tags containing the episode number.
        *   The `<td>` or `<th>` tags containing the episode title.
        *   Any `<span>`, `<i>`, or other nested tags within the title cell that might contain the actual title.
        *   The presence or absence of quotation marks around the episode title in the HTML.

3.  **Refine Title Extraction Logic:**
    *   Based on the HTML inspection, modify `_parse_embedded_episode_table` to ensure it reliably targets and extracts *only* the episode title. This might involve:
        *   Adjusting the index of `cells` accessed for the title.
        *   Using more specific BeautifulSoup selectors (e.g., `title_cell.find(\'i\')` if titles are consistently italicized).
        *   Improving the regex or logic to handle cases where titles are not quoted or are nested within other tags.
        *   Adding more robust checks to differentiate between episode numbers and titles within a cell.

---

### **Phase 2: Enhance and Debug Specific Table Parsing Strategies (1, 2, and 3)**

**Objective:** Improve the reliability of `_parse_table_by_season_link` (Strategy 1), `_parse_table_after_season_header` (Strategy 2), and `_parse_all_tables_flexibly` (Strategy 3) to correctly identify and extract titles from dedicated episode list pages.

1.  **Review `_parse_table_by_season_link` (Strategy 1):**
    *   **Season Link Pattern:** Re-evaluate the regex `re.compile(f"season {season}\b", re.IGNORECASE)` for finding season links. Ensure it\'s broad enough to catch variations (e.g., "Season X", "X Season") but specific enough to avoid false positives.
    *   **Table Identification:** Verify the logic for finding the `target_table` after the `season_link`. The `find_next_siblings()` approach might need adjustment if there are intervening non-table elements or if the table is nested differently. Consider using `find_next()` with a more general selector if the table isn\'t a direct sibling.

2.  **Review `_parse_table_after_season_header` (Strategy 2):**
    *   **Header Pattern:** Check the regex `re.compile(f"Season\s+{season}|Episodes", re.IGNORECASE)` for identifying season headers. Ensure it covers common header formats.
    *   **Table Proximity:** Confirm that `header_tag.find_next("table", class_="wikitable")` reliably finds the correct table. If other tables or elements can appear between the header and the target table, this might need to be more flexible (e.g., iterating through next siblings until a `wikitable` is found).

3.  **Review `_parse_all_tables_flexibly` (Strategy 3 - Fallback):**
    *   **Season Relevance Check:** The current check `if not season_pattern.search(prev_header.get_text()): continue` might be too strict. If a relevant table exists but its preceding header doesn\'t explicitly mention the season, it might be skipped. Consider if this fallback should be more lenient in its initial table selection, relying more on `_extract_title_from_table` to confirm relevance.

4.  **Refine `_extract_title_from_table`:**
    *   This function is crucial as it\'s used by Strategies 1, 2, and 3. Ensure its logic for identifying `no_in_season_col` and `title_col` is robust across different table structures.
    *   Verify that the title extraction within this function (`found_text` and `title_cell.get_text(strip=True)`) is also resilient to variations in HTML structure and consistently extracts the correct title.

---

### **Phase 3: Comprehensive Testing and Iterative Refinement**

**Objective:** Ensure all parsing strategies work correctly across a variety of Wikipedia page structures for TV shows, and that the correct episode title is always returned.

1.  **Develop Targeted Unit Tests:**
    *   Create isolated unit tests for each parsing function (`_parse_table_by_season_link`, `_parse_table_after_season_header`, `_parse_all_tables_flexibly`, `_parse_embedded_episode_table`, `_extract_title_from_table`).
    *   Use mock HTML content that simulates various Wikipedia page layouts:
        *   Dedicated episode list pages with season links.
        *   Dedicated episode list pages with season headers.
        *   Main show pages with embedded episode tables.
        *   Pages with unusual column orders or missing headers.
        *   Pages with titles containing special characters or nested tags.

2.  **Enhanced Logging:**
    *   Temporarily add more detailed `logger.debug` or `logger.info` statements within each parsing function.
    *   Log the HTML elements being processed, the text extracted at each step, and the decisions made by the parsing logic (e.g., "Skipping table because no season header found"). This will provide granular insight during debugging.

3.  **Iterative Refinement:**
    *   Run the tests and analyze the logs.
    *   Based on failures and log insights, iteratively adjust the parsing logic, regex patterns, and BeautifulSoup selectors.
    *   Repeat the testing and refinement cycle until all known problematic cases are resolved and the correct episode title is consistently extracted.
