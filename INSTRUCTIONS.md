You are an expert Python developer tasked with optimizing a generic web scraper. I will provide you with an analysis and a detailed refactoring plan. Your goal is to apply the logic from this plan to the project's source code.

Please follow these instructions carefully:

1. Understand the Goal: The primary objective is to improve the performance and precision of the existing generic scraper by implementing caching and a more robust, two-stage filtering mechanism. Do not change the overall generic architecture.

2. Code is Illustrative: The code examples provided within the '''python and '''yaml blocks are templates and logical guides. They are not complete, production-ready files.

3. Do Not Copy-Paste Blindly: You must intelligently integrate the logic from the examples into the actual project files. This will require you to adapt variable names, ensure correct class structure, and handle all necessary imports.

4. Follow the Plan's Logic: The core of the task is to implement the concepts described:
    - Caching for YAML configuration files.
    - Using a "results container" selector to narrow the HTML parsing scope.
    - Implementing a two-stage filtering process: gather all candidates, find the most likely correct media title by consensus, and then filter out non-matching results.

Here is the analysis and plan:

### Analysis: Pinpointing the Inefficiencies

The core issues lie in how the generic scraper discovers and filters content, which is fundamentally different from the old, highly specialized function.

1.  **Performance Bottleneck:** The primary slowdown comes from performing multiple, expensive full-document scans for every search. The old code was fast because it knew exactly where to look (e.g., the `<tbody>` of a specific table). The new code runs several strategies (`_strategy_find_direct_links`, `_strategy_contextual_search`, `_strategy_find_in_tables`), each of which may iterate over every single `<a>` tag or `<table>` tag in the entire HTML document. When searching for a full season, this process repeats for every single episode, compounding the delay.


### Plan to Optimize the Generic Scraper

Here is a step-by-step plan to refactor your generic scraper to incorporate the best elements of the old version, thereby increasing its speed and accuracy.

#### **1. Optimize Performance by Reducing Redundant Work**

We can make the scraper significantly faster by loading configurations only once and narrowing the scope of the HTML parsing.

**Step 1.1: Cache Scraper Configurations**
The scraper loads and parses the `1337x.yaml` file every time `scrape_1337x` is called. This is unnecessary I/O. We should cache this configuration in memory.

*   **Action:** Implement a simple module-level cache.

```python
# telegram_bot/services/generic_torrent_scraper.py

_config_cache = {}

def load_site_config(config_path: Path) -> dict[str, Any]:
    """Loads a site's YAML configuration, using a cache to avoid repeated file reads."""
    if config_path in _config_cache:
        return _config_cache[config_path]

    # ... (your existing YAML loading logic)

    config = ... # result of yaml.safe_load()
    _config_cache[config_path] = config
    return config
```

**Step 1.2: Pre-filter the HTML with a "Results Container" Selector**
Instead of having each strategy scan the entire `BeautifulSoup` object, we can add a selector to the YAML config that identifies the main container for search results. This reduces the search space for your strategies from the whole document to just the relevant section.

*   **Action:** Update your YAML config and the main search method.

**`1337x.yaml` (Example Addition):**
```yaml
# ... other configs ...
selectors:
  results_container: 'table.table-list tbody' # This selector isolates the table body with the results
# ... other selectors ...
```

**`generic_torrent_scraper.py` (Updated `search` method):**
```python
# In GenericTorrentScraper class
async def search(self, query: str, ...):
    html = await self._fetch_page(...)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")

    # Isolate the search to only the relevant part of the page
    results_container_selector = self.config.get("selectors", {}).get("results_container")
    if results_container_selector:
        search_area = soup.select_one(results_container_selector)
        if not search_area:
            # If the container isn't found, maybe the page structure changed.
            # Fall back to searching the whole soup object as before.
            search_area = soup
    else:
        search_area = soup

    # Now, pass the much smaller 'search_area' to your strategies instead of 'soup'
    # e.g., raw_results = self._strategy_find_in_tables(search_area, query)
```
