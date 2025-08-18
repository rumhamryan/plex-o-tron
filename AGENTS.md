### Multi-Phase Plan: Generic Web Scraper Implementation

This document outlines a phased approach to developing a new, generic web scraping module. The goal is to create a robust and maintainable solution capable of extracting torrent and magnet links from various websites, starting with EZTVx.to, without being tightly coupled to any single site's structure.

Each phase represents an atomic, testable strategy. The strategies are designed to be layered, meaning the scraper will try them in sequence to maximize the chance of finding a valid link.

#### **FORMAT**
- Use clear, descriptive naming conventions
- Include meaningful comments explaining the "why" behind complex logic
- Follow the principle of least surprise
- Implement appropriate error handling with informative messages
- Organize code with logical separation of concerns

#### **FOUNDATIONS**
- Prioritize readability over clever optimizations
- Include comprehensive input validation
- Implement proper exception handling
- Use consistent patterns throughout
- Create modular components with clear responsibilities
- Include unit tests that document expected behavior

#### **Phase 1: Direct Link Discovery Strategy**

This initial phase is the simplest and most direct approach. It serves as the foundation for all subsequent strategies by handling the most common and easily identifiable link types.

*   **Objective:** Find all `<a>` tags on a page whose `href` attribute points directly to a magnet link or a `.torrent` file.
*   **Implementation Steps:**
    1.  Create a new function `_strategy_find_direct_links(soup: BeautifulSoup) -> set[str]`.
    2.  Inside this function, use BeautifulSoup to find all `<a>` elements.
    3.  Filter these elements to find those where the `href` attribute exists and starts with `magnet:` or ends with `.torrent`.
    4.  Return a set of unique, valid URLs found.
*   **Testing:**
    *   Unit Test 1: Provide HTML with a standard `<a href="magnet:?...">Link</a>`. Verify the magnet link is returned.
    *   Unit Test 2: Provide HTML with a direct link to `<a href="https://example.com/file.torrent">Download</a>`. Verify the torrent URL is returned.
    *   Unit Test 3: Provide HTML with no relevant links. Verify an empty set is returned.

```python
# telegram_bot/services/scraping_service.py (Conceptual)

def _strategy_find_direct_links(soup: BeautifulSoup) -> set[str]:
    """
    Strategy 1: Finds all anchor tags that directly link to a magnet
    or .torrent file. This is the most reliable but least common case.
    """
    found_links = set()
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if isinstance(href, str):
            if href.startswith("magnet:"):
                found_links.add(href)
            elif href.endswith(".torrent"):
                # Ensure it's a full URL
                # (Logic to resolve relative URLs would be added here)
                found_links.add(href)
    return found_links
```

#### **Phase 2: Keyword-Based Contextual Search Strategy**

This phase expands our search beyond direct links. It looks for textual clues within or near links that suggest a torrent download, which is common on modern sites that might obscure direct links.

*   **Objective:** Identify links that are likely download links based on their text content or the text of nearby elements.
*   **Implementation Steps:**
    1.  Create a new function `_strategy_contextual_search(soup: BeautifulSoup, query: str) -> set[str]`.
    2.  Define a list of keywords (e.g., `["magnet", "torrent", "download", "1080p", "720p", "x265"]`).
    3.  Find all `<a>` tags. For each tag, check if its text, its parent's text, or the `href` itself contains any of the keywords or a significant portion of the user's search `query`.
    4.  If a match is found, add the `href` to a set of potential links. This set will be further processed by other strategies or a final validation step.
*   **Testing:**
    *   Unit Test 1: Provide HTML where the link is `<a href="/download/123">Download Torrent</a>`. Verify `/download/123` is identified.
    *   Unit Test 2: Provide HTML with `<a href="/details.php?id=456">My Show S01E01 1080p</a>`. If the query is "My Show", verify this link is found.
    *   Unit Test 3: Provide HTML with unrelated links containing keywords (e.g., `<a href="/about">About our download policy</a>`). Ensure these are handled or have a lower priority.

```python
# telegram_bot/services/scraping_service.py (Conceptual)

def _strategy_contextual_search(soup: BeautifulSoup, query: str) -> set[str]:
    """
    Strategy 2: Searches for links that don't point directly to a torrent
    but whose text or context (e.g., parent elements) contains keywords
    like "download", "1080p", or the search query itself.
    """
    potential_links = set()
    keywords = {"magnet", "torrent", "download", "1080p", "720p", "x265"}
    # Simplified example:
    for tag in soup.find_all("a", href=True):
        tag_text = tag.get_text(strip=True).lower()
        # Check if any keyword or a good portion of the query is in the link text
        if any(kw in tag_text for kw in keywords) or \
           fuzz.partial_ratio(query.lower(), tag_text) > 80:
            potential_links.add(tag["href"])
    return potential_links
```

#### **Phase 3: HTML Table Traversal Strategy**

This strategy specifically targets the most common layout for torrent sites: an HTML table. It assumes that relevant information (title, link, seeders) is organized in rows, making parsing more structured and reliable.

*   **Objective:** Find all HTML tables, iterate through their rows, and extract the most promising link from each row that appears to contain a torrent listing.
*   **Implementation Steps:**
    1.  Create a function `_strategy_find_in_tables(soup: BeautifulSoup, query: str) -> dict[str, float]`. This will return a dictionary of {link: score}.
    2.  Find all `<table>` elements on the page.
    3.  For each table, iterate through its `<tr>` (table row) elements.
    4.  In each row, check if the row's text contains the search `query` (using fuzzy matching for robustness).
    5.  If it's a potential match, find the first `<a>` tag with an `href` within that row. This is assumed to be the link to the detail or download page.
    6.  Assign a score to this link (e.g., higher score for better query match) and add it to the results dictionary.
*   **Testing:**
    *   Unit Test 1: Provide HTML with a simple table of torrents. Verify that the correct `href` is extracted from the row matching the query.
    *   Unit Test 2: Provide a table where the query matches multiple rows. Verify all corresponding links are returned.
    *   Unit Test 3: Provide a page with multiple tables (e.g., one for torrents, one for navigation). Verify it only extracts links from the correct table.

```python
# telegram_bot/services/scraping_service.py (Conceptual)

def _strategy_find_in_tables(soup: BeautifulSoup, query: str) -> dict[str, float]:
    """
    Strategy 3: Specifically targets HTML tables, which are a common
    way to list torrents. It iterates through rows, and if a row's text
    is a good match for the query, it extracts the first available link.
    """
    scored_links = {}
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            row_text = row.get_text(strip=True, separator=" ")
            # Use fuzzy matching to see if this row is relevant to the search
            match_score = fuzz.ratio(query.lower(), row_text.lower())

            if match_score > 75:  # Confidence threshold
                first_link = row.find("a", href=True)
                if first_link:
                    # The score is based on how well the row text matched
                    scored_links[first_link["href"]] = match_score
    return scored_links
```

#### **Phase 4: Heuristic Scoring and Final Selection**

This final phase acts as the "brain" of the scraper. It takes the links gathered from all previous strategies and applies a scoring heuristic to determine the single best candidate link. This prevents the scraper from picking an incorrect link on a noisy page.

*   **Objective:** Aggregate links from all strategies, score them based on a set of heuristics, and select the one with the highest score.
*   **Implementation Steps:**
    1.  Create the main orchestrator function `scrape_generic_page(...)` that will call the three strategy functions.
    2.  Create a helper function `_score_candidate_links(links: set[str], query: str) -> str | None`.
    3.  Inside the scoring function, iterate through each unique link found by the strategies.
    4.  Apply a scoring algorithm. A link gets points for:
        *   Being a direct magnet link (high score).
        *   Having link text that is very similar to the search query.
        *   Being found via the table strategy (indicates structure).
        *   A link loses points if its parent elements have attributes like `class="ads"` or `id="comments"`.
    5.  The main function will then take the highest-scoring link and proceed with the download process.
*   **Testing:**
    *   Unit Test 1: Provide a set of links including a direct magnet, a contextual link, and a table link. Verify the magnet link is chosen.
    *   Unit Test 2: Provide two similar links, but one is inside a `<div class="ad">`. Verify the other link is chosen.
    *   Unit Test 3: Provide a link whose text is a 95% match to the query vs. another link that is a 70% match. Verify the 95% match is chosen.

```python
# telegram_bot/services/scraping_service.py (Conceptual)

def scrape_generic_page(query: str, media_type: str, search_url: str, ...) -> list[dict]:
    """
    Orchestrator for the generic scraper. It runs multiple strategies
    to find links and then uses a heuristic model to score and select
    the best candidate. This allows it to adapt to different page layouts.
    """
    html = await _get_page_html(search_url)
    soup = BeautifulSoup(html, "lxml")

    # 1. Gather candidates from all strategies
    direct_links = _strategy_find_direct_links(soup)
    context_links = _strategy_contextual_search(soup, query)
    table_links_scored = _strategy_find_in_tables(soup, query)

    # 2. Aggregate and score all candidates
    all_candidates = direct_links.union(context_links).union(table_links_scored.keys())
    best_link = _score_candidate_links(all_candidates, query, table_links_scored)

    # 3. If a winner is found, format it into the standard result format
    if best_link:
        # This link might be a magnet or another webpage that needs to be scraped.
        # The function will recursively call the scraper or process the magnet.
        # ... logic to create the final result dictionary ...
        return [final_result]
    return []
```