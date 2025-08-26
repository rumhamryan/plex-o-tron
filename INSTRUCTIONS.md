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

1.  **Precision Degradation:** The precision was lost because the powerful two-stage filtering logic from the old scraper was not carried over.
    *   **Old Logic:** It would first find all potential matches, then identify the *single most common media title* among them (e.g., "Dune Part Two"), and finally, it would discard any results that didn't match that exact title. This was brilliant for eliminating noise from similarly named but incorrect media.
    *   **New Logic:** It uses a lower fuzzy match threshold (`75-80`) with `fuzz.partial_ratio`, which is less strict. It evaluates each torrent individually against the query rather than against a consensus of what the "correct" media title is on the page. This is precisely why a search for "Dune" might pull in results for "Dune (1984)" and "Jodorowsky's Dune", leading to a confusing list of years.

---

#### **1. Improve Precision with Two-Stage Filtering**

This is the most critical change to fix the accuracy issue. We will replicate the intelligent filtering from your old scraper within the generic one.

**Step 1.1: Re-implement the Two-Stage Candidate Analysis**
The main `search` method of your `GenericTorrentScraper` should be updated to perform this two-stage logic.

*   **Action:** Refactor the `search` method.

```python
# In GenericTorrentScraper class

async def search(self, query: str, media_type: str, base_query_for_filter: str | None = None) -> list[TorrentResult]:
    # ... (previous code to get html and the 'search_area' soup object) ...

    # --- STAGE 1: Gather all potential candidates from the page ---
    # Run your strategies (table parsing, etc.) on the 'search_area' to get a list of initial results.
    # This should return a list of Pydantic models or dicts, each with a 'name' field.
    all_candidates = self._parse_results_from_page(search_area)

    if not all_candidates:
        return []

    # --- STAGE 2: Identify the correct media title by consensus ---
    filter_query = base_query_for_filter or query

    # Add parsed base name to each candidate
    for cand in all_candidates:
        cand.parsed_info = parse_torrent_name(cand.name)
        cand.base_name = cand.parsed_info.get("title", "")

    # First-pass filter to remove obvious mismatches
    strong_candidates = [
        c for c in all_candidates
        if fuzz.ratio(filter_query.lower(), c.base_name.lower()) > 75 # Lenient first pass
    ]

    if not strong_candidates:
        return []

    # Find the most common (and therefore likely correct) base name
    from collections import Counter
    base_name_counts = Counter(c.base_name for c in strong_candidates)
    if not base_name_counts:
        return []

    best_match_base_name, _ = base_name_counts.most_common(1)[0]
    logger.info(f"Identified consensus title: '{best_match_base_name}'")

    # --- STAGE 3: Final filtering ---
    # Only keep results that match the consensus title. This is the crucial step.
    final_results = [
        c for c in strong_candidates if c.base_name == best_match_base_name
    ]

    # Now you can proceed to fetch detail pages or magnet links for the 'final_results'
    # ...

    return final_results
```

**Step 1.2: Make Fuzzy Scorer and Threshold Configurable**
To fine-tune precision for different sites, you can make the fuzzy matching parameters part of the YAML config.

*   **Action:** Add options to your YAML and use them in the code.

**`1337x.yaml` (Example Addition):**
```yaml
# ...
matching:
  fuzz_scorer: 'token_set_ratio' # Can be 'ratio', 'partial_ratio', etc.
  fuzz_threshold: 88
# ...
```
This allows you to use a stricter scorer like `fuzz.token_set_ratio` and a higher threshold for reliable sites like 1337x, while perhaps using more lenient settings for less structured sites, all without changing the Python code.

By implementing these optimizations, your generic scraper will become a robust and efficient tool, combining the speed and precision of your original code with the flexibility and scalability of your new architecture.
