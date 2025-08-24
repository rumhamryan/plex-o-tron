### Action Plan: Fixing the Generic Scraper Magnet Link Extraction

This plan will guide you through debugging and fixing the issue where the generic scraper fails to find magnet links on 1337x.to detail pages. The core strategy is to use the proven selector from your `master` branch and add precise logging to pinpoint the exact point of failure in your new code.

---

#### Step 1: Solidify the YAML Configuration

First, let's eliminate any possibility of a typo or structural error in the configuration file. Replace the entire content of your `scrapers/configs/1337x.yaml` with the following known-good structure.

**File: `scrapers/configs/1337x.yaml`**
```yaml
name: 1337x
base_url: https://1337x.to

# Maps your bot's internal media types to the site's URL paths
category_mapping:
  movie: Movies
  tv: TV-shows # Note: Double-check if the site uses "TV-shows", "TV", or something else for TV content

# The URL structure for searches
search_path: /category-search/{query}/{category}/{page}/

# --- Selectors ---

# Selectors for the INITIAL search results page
results_page_selectors:
  rows: "tbody > tr"
  name: "td.name a:nth-of-type(2)"
  seeds: "td.seeds"
  size: "td.size"
  uploader: "td.coll-uploader a"
  # This selector finds the link that leads to the detail page
  details_page_link: "td.name a:nth-of-type(2)"

# Selectors for the SECONDARY torrent detail page
details_page_selectors:
  # This is the simple, proven selector from your master branch
  magnet_url: "a[href^='magnet:']"
```

---

#### Step 2: Add Precise Debug Logging

Now, we need to see what the scraper is "thinking" when it parses the detail page. Edit your `generic_torrent_scraper.py` file to add logging at the critical step.

Locate the `search` method within your `GenericTorrentScraper` class. Find the part inside the `for` loop where you process the detail page HTML. Add the following three `logger.debug` lines as shown below.

**File: `generic_torrent_scraper.py` (Inside the `search` method)**
```python
# ... inside the 'for row in result_rows:' loop ...

detail_page_html = await self._fetch_page(detail_page_url)
if not detail_page_html:
    continue # Skip this row if fetching the detail page failed

detail_soup = BeautifulSoup(detail_page_html, "lxml")

# --- ADD THE FOLLOWING DEBUG CODE ---

# 1. Get the selector from the config
magnet_selector = self.config.details_page_selectors.get("magnet_url")
logger.debug(f"[DEBUG] Using magnet selector: '{magnet_selector}' on {detail_page_url}")

# 2. Try to find the element
magnet_element = detail_soup.select_one(magnet_selector)
logger.debug(f"[DEBUG] Found element with selector: {magnet_element is not None}")

# 3. Extract the href if the element was found
if magnet_element:
    magnet_link = magnet_element.get("href")
    logger.debug(f"[DEBUG] Extracted href: {magnet_link[:70] if magnet_link else 'None'}")
else:
    magnet_link = None

# --- END OF DEBUG CODE ---

# Your original logic to check if magnet_link exists would follow
if not magnet_link:
    logger.warning(f"Failed to extract magnet link from {detail_page_url}")
    continue

# ... continue to assemble and append the final result ...
```

---

#### Step 3: Run and Analyze the New Logs

Run your bot and perform a search for "Happy Gilmore 2" again. This time, examine the new `[DEBUG]` messages in your `plex-o-tron.txt` log file for each detail page that is scraped.

You are looking for one of three outcomes:

1.  **SUCCESS:**
    ```
    [DEBUG] Using magnet selector: 'a[href^='magnet:']' on https://1337x.to/torrent/...
    [DEBUG] Found element with selector: True
    [DEBUG] Extracted href: magnet:?xt=urn:btih:BB4900E286223E54D716F662010AA1C29E4F252E&dn=Hap...
    ```
    *   **Meaning:** The code is working perfectly. If you still get 0 results, the issue might be in the code that *appends* the result to your list.

2.  **FAILURE CASE A: Element Not Found**
    ```
    [DEBUG] Using magnet selector: 'a[href^='magnet:']' on https://1337x.to/torrent/...
    [DEBUG] Found element with selector: False
    ```
    *   **Meaning:** The selector, while correct in the `master` branch's context, is failing here. This is unlikely but possible.
    *   **Fix:** The most common reason for this is that 1337x has served you a Cloudflare/anti-bot page instead of the real content. Add `logger.debug(f"[DEBUG] Detail page HTML snippet: {detail_page_html[:500]}")` to see what HTML you're actually getting. If it's a Cloudflare page, you may need to improve your request headers (e.g., User-Agent) to look more like a real browser.

3.  **FAILURE CASE B: Href Not Extracted**
    ```
    [DEBUG] Using magnet selector: 'a[href^='magnet:']' on https://1337x.to/torrent/...
    [DEBUG] Found element with selector: True
    [DEBUG] Extracted href: None
    ```
    *   **Meaning:** This would indicate a very strange HTML structure where the `<a>` tag exists but has no `href` attribute, which is highly improbable. This result would also point toward an anti-bot page being served.

Based on your logs, **Failure Case A** is the most likely culprit. The debugging will confirm it, and from there the fix will be clear.

---

#### Step 4: Clean Up

Once the scraper is successfully returning results, remember to remove or comment out the extra `logger.debug` lines from `generic_torrent_scraper.py` to keep your production logs clean.

#### Step 5: Conclusion

Remove the file `INSTRUCTIONS.md` from the project.
