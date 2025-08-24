# Plan to Generalize the Torrent Scraper

## Objective

The goal is to refactor the existing 1337x-specific scraper into a generic, configuration-driven framework. This will allow the bot to easily support scraping various torrent index websites with minimal code changes, primarily by adding a new configuration file for each new site.

## 1. Configuration Strategy: `config.ini` and Site-Specific YAMLs

While the project already uses a `config.ini` file, creating separate configuration files for each scraper is the recommended approach.

*   **Why not just use `config.ini`?**
    *   **Structure:** Scraper configurations involve nested data (e.g., selectors for a results page vs. a details page). YAML handles this naturally, whereas `ini` files are flat and would require clunky key names (e.g., `details_page_selector_magnet_link`).
    *   **Modularity:** Adding a new site becomes as simple as adding a new file. This is much cleaner than editing one large, central `config.ini` file, which can become unwieldy and prone to errors.

*   **How they work together:**
    The central `config.ini` will control the scrapers at a high level, while the individual YAML files will contain the site-specific details.

    **Example `config.ini`:**
    ```ini
    [Scrapers]
    # A comma-separated list of scrapers to enable
    enabled_scrapers = 1337x, thepiratebay

    # The directory where scraper YAML configs are stored
    scraper_config_path = telegram_bot/scrapers/configs/
    ```

    The application will read `config.ini` to determine which scrapers to load and where to find their respective `.yaml` configuration files. This gives us the best of both worlds: centralized control and modular, readable scraper definitions.

## 2. Analyze the Current 1337x Scraper

Before refactoring, we need a clear understanding of the current implementation.

-   **Identify Hardcoded Values**: Locate all values specific to 1337x. This includes:
    -   Base URL (e.g., `https://1337x.to`).
    -   Search URL patterns and query parameters.
    -   CSS selectors used to find search results, torrent titles, magnet links, seeder/leecher counts, and file sizes.
-   **Map Data Extraction Logic**: Document the exact data points being extracted and any transformations applied to them (e.g., converting "1.4 GB" into bytes).
-   **Trace the Control Flow**: Understand the sequence of operations:
    1. Building the search URL.
    2. Making the HTTP request.
    3. Parsing the HTML response.
    4. Iterating over search result elements.
    5. Extracting data from each element.
    6. Potentially visiting a details page to get the magnet link.

## 3. Design a Generic, Config-Driven Architecture

We will create a system where each supported website is defined by a configuration file.

### A. Site Configuration File

For each site, we'll have a configuration file (e.g., in YAML format for readability) that defines its unique properties.

**Example `1337x.yaml`:**

```yaml
site_name: "1337x"
base_url: "https://1337x.to"
search_path: "/search/{query}/1/" # {query} is a placeholder for the search term

# CSS Selectors for the search results page
selectors:
  results_container: "table.table-list tbody tr"
  title: "td.name a:nth-of-type(2)"
  # If the link is relative, it will be joined with base_url
  details_page_link: "td.name a:nth-of-type(2)"
  seeders: "td.seeds"
  leechers: "td.leeches"
  size: "td.size"
  # Magnet link is on the details page for 1337x
  magnet_link: null

# CSS Selectors for the torrent details page (if needed)
details_page_selectors:
  magnet_link: "a.btn-magnet"
```

### B. Core Scraper Class

Create a `GenericTorrentScraper` class that is initialized with a site configuration.

-   **`__init__(self, site_config)`**: Loads and validates the configuration for a specific site.
-   **`async search(self, query: str) -> list[TorrentData]`**:
    -   Constructs the search URL from `base_url` and `search_path`, replacing `{query}`.
    -   Fetches the page content using `httpx`.
    -   Parses the HTML with `BeautifulSoup`.
    -   Finds all result elements using `selectors['results_container']`.
    -   For each result, it calls a helper method to parse the element.
-   **`async _parse_result(self, result_element) -> TorrentData`**:
    -   Extracts basic data (title, seeders, etc.) from the search result row using the provided selectors.
    -   If `selectors['magnet_link']` is `null` but `selectors['details_page_link']` is present, it will:
        1. Construct the full details page URL.
        2. Fetch and parse the details page.
        3. Extract the magnet link using `details_page_selectors['magnet_link']`.
    -   Cleans and standardizes the extracted data (e.g., parse size string into bytes, ensure seeders are integers).
    -   Returns a structured `TorrentData` object (a `dataclass` or `TypedDict` would be ideal).

## 4. Step-by-Step Refactoring Plan

1.  **Create the `TorrentData` Structure**: Define a `dataclass` or `TypedDict` to hold the scraped torrent information consistently (e.g., `name`, `magnet_url`, `seeders`, `leechers`, `size_bytes`, `source_site`).

2.  **Implement the Site Configuration Loader**: Create a function that can load and validate a YAML file for a given site.

3.  **Build `GenericTorrentScraper`**: Implement the class as designed above. Focus on making the parsing logic robust and resilient to missing elements on a page. Use helper methods to keep the `search` method clean.

4.  **Create `1337x.yaml`**: Populate the first configuration file using the information gathered in the analysis phase.

5.  **Replace Old Scraper**: Modify the part of the application that calls the 1337x scraper. Instead of instantiating the old scraper, it should now:
    1.  Load the `1337x.yaml` config.
    2.  Instantiate `GenericTorrentScraper` with that config.
    3.  Call the `search` method.
    4.  Verify that the output is identical to the old scraper's output to ensure no functionality is lost.

## 5. Adding a New Site (Example Workflow)

Once the framework is in place, adding a new site like "The Pirate Bay" becomes a data-entry task, not a coding one.

1.  **Investigate the Target Site**:
    -   Perform a search on the site (e.g., The Pirate Bay).
    -   Use browser developer tools to inspect the search results page.
    -   Identify the CSS selectors for the results container and each piece of data (title, magnet link, size, seeders, leechers). Note that TPB has the magnet link directly on the search results page.
2.  **Create `thepiratebay.yaml`**:
    ```yaml
    site_name: "The Pirate Bay"
    base_url: "https://thepiratebay.org" # Or a proxy
    search_path: "/search.php?q={query}"

    selectors:
      results_container: "table#searchResult tr:not(.header)"
      title: "a.detLink"
      # Magnet link is directly on the search page
      magnet_link: 'a[title="Download this torrent using magnet"]'
      details_page_link: null # Not needed
      # Size, seeders, leechers are in a single string, requires more advanced parsing
      # This is a good place for a custom parsing function or regex
      size: "font.detDesc"
      seeders: "td[align='right']"
      leechers: "td[align='right']:nth-of-type(2)"

    details_page_selectors: null
    ```
3.  **Test**: The application logic can now be pointed to use the `thepiratebay.yaml` config to test scraping from the new source.

## 6. Handling Advanced Scenarios

The design should be extensible to handle common complexities.

-   **Complex Data Parsing**: For sites where data is not cleanly separated (like TPB's size/uploader string), the scraper could support optional, site-specific "parser functions" or regex patterns defined in the config to extract and clean the data.
-   **Pagination**: The config could include a selector for the "next page" link. The `search` method could have an optional `limit` parameter and follow "next page" links until the limit is reached.
-   **JavaScript-Rendered Content**: If a site relies heavily on JavaScript, `httpx` will only get the initial HTML. The framework could be designed to optionally use a browser automation tool like `Playwright` or `Selenium` instead of `httpx` for fetching page content, perhaps controlled by a `fetch_method: "javascript"` flag in the config.
