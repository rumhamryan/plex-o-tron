### Revised Plan: Creating a Media-Type-Aware Generic Scraper

Here is the revised, multi-phase plan to fix the `generic_scraper` branch. This plan ensures that the scraper can handle different media types (like movies and TV shows) by using a more powerful YAML configuration, and it implements the necessary multi-step logic required for complex sites like 1337x.to.

---

#### Phase 1: Implement Dynamic, Category-Aware URL Construction

**Goal:** Fix the immediate `403 Forbidden` error by correctly building the category-specific search URL for any given media type.

1.  **Upgrade `1337x.yaml` to be Media-Type Aware:**
    Modify your YAML configuration to map your internal media types to the specific URL paths the website requires. The `search_path` will be updated to use a new `{category}` placeholder.

    **File: `scrapers/configs/1337x.yaml`**
    ```yaml
    name: 1337x
    base_url: https://1337x.to

    # NEW: A mapping from your internal media types to the site's URL paths
    category_mapping:
      movie: Movies
      tv: TV-shows # Note: Verify the exact path name on the site, e.g., "TV" or "TV-shows"

    # UPDATED: The search path now includes a {category} placeholder
    search_path: /category-search/{query}/{category}/{page}/

    # ... other selectors will be added in Phase 2 ...
    ```

2.  **Update the `scrape_1337x` Wrapper Function:**
    Modify the wrapper in `scraping_service.py` to pass the `media_type` variable into the generic scraper's `search` method.

    **File: `scraping_service.py`**
    ```python
    async def scrape_1337x(
        query: str,
        media_type: str, # This variable is the key
        # ... other args
    ) -> list[dict[str, Any]]:
        # ... (load config and preferences)
        scraper = GenericTorrentScraper(site_config)

        # MODIFIED: Pass the media_type to the search method
        raw_results = await scraper.search(query, media_type)

        # ... (the rest of the function remains the same)
    ```

3.  **Enhance the `GenericTorrentScraper.search()` Method:**
    Update the core search logic in `generic_torrent_scraper.py` to use the `media_type` to look up the correct category from the YAML and build the final URL.

    **File: `generic_torrent_scraper.py` (Conceptual Logic)**
    ```python
    # The search method now accepts media_type
    async def search(self, query: str, media_type: str):
        # 1. Look up the category from the config mapping
        category_path = self.config.category_mapping.get(media_type)
        if not category_path:
            logger.error(f"Media type '{media_type}' not found in category_mapping for {self.config.name}")
            return []

        # 2. Build the final, correct search URL
        formatted_query = urllib.parse.quote_plus(query)
        search_path = self.config.search_path.format(
            query=formatted_query,
            category=category_path,
            page=1 # Or handle pagination later
        )
        search_url = self.base_url + search_path

        logger.info(f"Constructed search URL: {search_url}")

        # ... continue with fetching the page and scraping results ...
    ```
**Result of Phase 1:** The `403 Forbidden` error will be resolved for all media types. The scraper will now successfully request the correct search results page but will not yet find torrents.

---

#### Phase 2: Evolve the Generic Scraper to Support Detail Pages

**Goal:** Teach the scraper to handle sites where the magnet link is on a secondary "detail page," not on the search results list.

1.  **Expand the YAML Specification:**
    Structure the YAML to differentiate between selectors for the search results page and the detail page. Add a selector to find the link *to* the detail page.

    **File: `scrapers/configs/1337x.yaml` (Continued)**
    ```yaml
    # ... (name, base_url, category_mapping, search_path from Phase 1) ...

    # Selectors for the INITIAL search results page
    results_page_selectors:
      rows: "tbody > tr"
      name: "td.name a:nth-of-type(2)"
      seeds: "td.seeds"
      size: "td.size"
      uploader: "td.coll-uploader a"
      # NEW: Selector for the link to the detail page
      details_page_link: "td.name a:nth-of-type(2)"

    # Selectors for the SECONDARY torrent detail page
    details_page_selectors:
      magnet_url: "a[href^='magnet:']"
    ```

2.  **Upgrade `GenericTorrentScraper.search()` Logic:**
    Implement the full multi-step scraping process inside the `search` method.

    **File: `generic_torrent_scraper.py` (Conceptual Logic)**
    ```python
    async def search(self, query: str, media_type: str):
        # ... (URL construction logic from Phase 1)
        search_page_html = await self._fetch_page(search_url)
        if not search_page_html:
            return []

        soup = BeautifulSoup(search_page_html, "lxml")
        results = []
        result_rows = soup.select(self.config.results_page_selectors.rows)

        for row in result_rows:
            # Step 1: Extract data from the search results row
            name = self._extract_text(row, self.config.results_page_selectors.name)
            # ... extract seeds, size, uploader etc. from the row

            # Step 2: Get the link to the detail page
            details_link_href = self._extract_href(row, self.config.results_page_selectors.details_page_link)
            if not details_link_href:
                continue

            # Step 3: Visit the detail page
            detail_page_url = self.base_url + details_link_href
            detail_page_html = await self._fetch_page(detail_page_url)
            if not detail_page_html:
                continue

            detail_soup = BeautifulSoup(detail_page_html, "lxml")

            # Step 4: Extract the magnet link from the detail page
            magnet_link = self._extract_href(detail_soup, self.config.details_page_selectors.magnet_url)
            if not magnet_link:
                continue

            # Step 5: Assemble and append the final result object
            results.append(TorrentResult(name=name, magnet_url=magnet_link, ...))

        return results
    ```
**Result of Phase 2:** The scraper will now successfully find and extract magnet links from 1337x.to for both movies and TV shows.

---

#### Phase 3: Refine and Re-integrate Advanced Features

**Goal:** Improve the quality of results by adding back the intelligent filtering from the `master` branch and making the scraper more resilient.

1.  **Re-implement Fuzzy Filtering as a Generic Feature:**
    Move the logic that identifies the "most common base name" into the `GenericTorrentScraper`. Make it an optional feature that can be enabled and configured in the YAML.

    **File: `scrapers/configs/1337x.yaml` (Additions)**
    ```yaml
    # ...
    # NEW: Optional advanced features
    advanced_features:
      enable_fuzzy_filter: true
      fuzzy_filter_ratio: 85 # Configurable threshold
    ```

2.  **Add Robustness and Error Handling:**
    Wrap network requests and parsing steps in the `GenericTorrentScraper`'s loop with `try...except` blocks. This ensures that a single malformed row or a failed detail page request doesn't terminate the entire scraping process.

**Final Result:** You will have a single, robust `GenericTorrentScraper` class that can be configured via YAML files to handle both simple sites and complex, multi-step sites like 1337x.to, while correctly processing different media categories.
