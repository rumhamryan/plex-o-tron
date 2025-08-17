# telegram_bot/services/scraping_service.py

import asyncio
from collections import Counter
import re
import urllib.parse
from typing import Any

import httpx
import wikipedia
from bs4 import BeautifulSoup, Tag
from telegram.ext import ContextTypes
from thefuzz import fuzz, process

from ..config import logger
from .search_logic import _parse_codec, _parse_size_to_gb, score_torrent_result
from ..utils import extract_first_int, parse_torrent_name


# --- Helper Functions ---


async def _get_page_html(url: str) -> str | None:
    """Fetches the HTML content of a URL."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text
    except httpx.RequestError as e:
        logger.error(f"Error fetching URL {url}: {e}")
        return None


# --- Wikipedia Scraping ---


async def fetch_episode_title_from_wikipedia(
    show_title: str, season: int, episode: int
) -> tuple[str | None, str | None]:
    """
    Fetches an episode title from Wikipedia, trying a dedicated episode list page first,
    then falling back to the main show page.

    Returns:
        A tuple of (episode_title, corrected_show_title). The corrected title is
        returned if Wikipedia redirects the initial search.
    """
    corrected_show_title: str | None = None
    canonical_title = show_title
    main_page_url: str | None = None

    # --- Step 1: Find the main show page to get the canonical, corrected title ---
    try:
        logger.info(
            f"[WIKI] Step 1: Finding main page to correct title for '{show_title}'"
        )
        search_results = await asyncio.to_thread(wikipedia.search, show_title)
        if not search_results:
            logger.error(f"[WIKI] No Wikipedia page found for '{show_title}'. Aborting.")
            return None, None

        main_page_title = search_results[0]
        main_page = await asyncio.to_thread(
            wikipedia.page, main_page_title, auto_suggest=False, redirect=True
        )
        main_page_url = main_page.url

        if main_page.title != show_title:
            corrected_show_title = main_page.title
            canonical_title = corrected_show_title
            logger.info(
                f"[WIKI] Title was corrected: '{show_title}' -> '{canonical_title}'"
            )
        else:
            logger.info("[WIKI] Successfully found main show page with original title.")

    except wikipedia.exceptions.PageError:
        logger.error(
            f"[WIKI] Could not find any Wikipedia page for '{show_title}'. Aborting."
        )
        return None, None
    except Exception as e:
        logger.error(
            f"[WIKI] An unexpected error occurred during main page search: {e}"
        )
        return None, None

    # --- Step 2: Use the canonical title to find the dedicated episode page ---
    html_to_scrape: str | None = None
    try:
        direct_query = f"List of {canonical_title} episodes"
        logger.info(
            f"[WIKI] Step 2: Attempting to find dedicated episode page: '{direct_query}'"
        )
        list_page = await asyncio.to_thread(
            wikipedia.page, direct_query, auto_suggest=False, redirect=True
        )
        html_to_scrape = await _get_page_html(list_page.url)
        logger.info("[WIKI] Found and will use dedicated episode page.")

    except wikipedia.exceptions.PageError:
        logger.warning(
            "[WIKI] No dedicated episode page found. Falling back to main show page HTML."
        )
        if main_page_url:
            html_to_scrape = await _get_page_html(main_page_url)
    except Exception as e:
        logger.error(
            f"[WIKI] Unexpected error fetching list page, falling back to main page HTML: {e}"
        )
        if main_page_url:
            html_to_scrape = await _get_page_html(main_page_url)

    if not html_to_scrape:
        logger.error("[WIKI] All page search attempts failed.")
        return None, None

    # --- Step 3: Parse the HTML to find the episode title ---
    soup = BeautifulSoup(html_to_scrape, "lxml")
    episode_title = await _parse_episode_tables(soup, season, episode)

    if not episode_title:
        logger.warning(
            f"[WIKI] All parsing strategies failed to find S{season:02d}E{episode:02d}."
        )

    return episode_title, corrected_show_title


async def _parse_episode_tables(
    soup: BeautifulSoup, season: int, episode: int
) -> str | None:
    """
    Orchestrates different strategies to parse episode titles from tables.

    This function prioritizes readability by breaking down the parsing logic into
    clear, sequential steps. It tries strategies in order of specificity.
    """
    logger.info("[WIKI] Parsing HTML for episode tables.")

    # Strategy 1: Find a link to a dedicated season page (e.g., "South Park season 27")
    # This is common for shows with many seasons.
    episode_title = await _parse_table_by_season_link(soup, season, episode)
    if episode_title:
        return episode_title

    # Strategy 2: Find a season header (e.g., "<h3>Season 1</h3>") and parse the next table.
    # This is a common pattern for dedicated episode list pages.
    episode_title = await _parse_table_after_season_header(soup, season, episode)
    if episode_title:
        return episode_title

    # Strategy 3: Flexibly search all tables if the other approaches fail.
    # This is a robust fallback for pages with unpredictable structures.
    logger.info("[WIKI] Specific strategies failed. Trying flexible table search.")
    episode_title = await _parse_all_tables_flexibly(soup, season, episode)
    if episode_title:
        return episode_title

    return None


async def _parse_table_by_season_link(
    soup: BeautifulSoup, season: int, episode: int
) -> str | None:
    """
    (Strategy 1) Parses a 'wikitable' by finding a link to the season's main article.
    This is highly effective for series like South Park with dedicated season pages.
    """
    logger.info(f"[WIKI] Trying Strategy 1: Find link for Season {season}")
    # Pattern to find "South Park season 27" or similar, case-insensitively.
    season_pattern = re.compile(f"season {season}\b", re.IGNORECASE)
    
    # Find an 'a' tag whose 'title' attribute matches the season pattern.
    season_link = soup.find("a", title=season_pattern)

    if not isinstance(season_link, Tag):
        logger.info(f"[WIKI] Strategy 1: Could not find a link for Season {season}.")
        return None

    logger.info(
        f"[WIKI] Strategy 1: Found link for Season {season}: '{season_link.get_text(strip=True)}'"
    )

    # The episode table is often the next 'wikitable' after the link's container.
    # We search from the link's parent to be robust against minor structural changes.
    search_node = season_link.parent
    if not isinstance(search_node, Tag):
        logger.warning(f"[WIKI] Strategy 1: Could not find parent of the season link.")
        return None

    target_table = None
    # Iterate through next siblings to find the first wikitable
    for sibling in search_node.find_next_siblings():
        if isinstance(sibling, Tag):
            classes = sibling.get("class")
            if classes and "wikitable" in classes:
                target_table = sibling
                break
            # Also check within the sibling if it's a container
            found = sibling.find("table", class_="wikitable")
            if found:
                target_table = found
                break

    if not isinstance(target_table, Tag):
        logger.warning(
            f"[WIKI] Strategy 1: Found link for Season {season}, but no subsequent wikitable."
        )
        return None

    logger.info("[WIKI] Strategy 1: Found table, extracting title.")
    return await _extract_title_from_table(target_table, season, episode)


async def _parse_table_after_season_header(
    soup: BeautifulSoup, season: int, episode: int
) -> str | None:
    """
    (Strategy 2) Parses a 'wikitable' immediately following a season header (h2, h3).
    """
    logger.info(f"[WIKI] Trying Strategy 2: Find header for Season {season}")
    # This regex is designed to be specific to avoid false positives.
    season_pattern = re.compile(f"Season\\s+{season}", re.IGNORECASE)
    header_tag = soup.find(lambda tag: tag.name in ['h2', 'h3'] and bool(season_pattern.search(tag.get_text())))

    if not isinstance(header_tag, Tag):
        logger.info(f"[WIKI] Strategy 2: Could not find a dedicated header for Season {season}.")
        return None

    logger.info(f"[WIKI] Strategy 2: Found header for Season {season}: '{header_tag.get_text(strip=True)}'")
    target_table = header_tag.find_next("table", class_="wikitable")

    if not isinstance(target_table, Tag):
        logger.warning(
            f"[WIKI] Strategy 2: Found header for Season {season}, but no subsequent wikitable."
        )
        return None
    
    logger.info("[WIKI] Strategy 2: Found table, extracting title.")
    return await _extract_title_from_table(target_table, season, episode)


async def _parse_all_tables_flexibly(
    soup: BeautifulSoup, season: int, episode: int
) -> str | None:
    """
    (Fallback Strategy) Iterates through all wikitables to find the episode.
    """
    logger.info("[WIKI] Trying Fallback Strategy: Flexible search of all tables.")
    for table in soup.find_all("table", class_="wikitable"):
        if isinstance(table, Tag):
            # Check if the table is for the correct season by checking the previous header
            prev_header = table.find_previous(["h2", "h3"])
            if prev_header and isinstance(prev_header, Tag):
                season_pattern = re.compile(f"Season\\s+{season}", re.IGNORECASE)
                if not season_pattern.search(prev_header.get_text()):
                    continue # Not the right season, skip this table

            title = await _extract_title_from_table(table, season, episode)
            if title:
                logger.info("[WIKI] Fallback Strategy: Found title.")
                return title
    
    logger.info("[WIKI] Fallback Strategy: Failed to find title in any table.")
    return None


async def _extract_title_from_table(
    table: Tag, season: int, episode: int
) -> str | None:
    """
    Extracts an episode title from a given table based on season and episode number.
    """
    for row in table.find_all("tr")[1:]:
        if not isinstance(row, Tag):
            continue

        cells = row.find_all(["td", "th"])
        if len(cells) < 3:  # Basic validation for required columns
            continue

        try:
            # Column 1: Episode number in season. This is a common convention.
            episode_num_cell = cells[1].get_text(strip=True)
            if extract_first_int(episode_num_cell) == episode:
                title_cell = cells[2]
                if isinstance(title_cell, Tag):
                    # The title is often in quotes. Prioritize finding that.
                    found_text = title_cell.find(string=re.compile(r'"([^"]+)"'))
                    if found_text:
                        # Cleanly extract the text within the quotes.
                        return str(found_text).strip().strip('"')
                    # Fallback to the full, stripped text of the cell.
                    return title_cell.get_text(strip=True)
        except (ValueError, IndexError):
            # This handles cases where a row doesn't match the expected format.
            continue

    return None


async def fetch_season_episode_count_from_wikipedia(
    show_title: str, season: int
) -> int | None:
    """Fetches the number of episodes for a given season from Wikipedia."""
    html_to_scrape = None
    try:
        # Attempt to get the dedicated "List of..." page first
        list_page = await asyncio.to_thread(
            wikipedia.page,
            f"List of {show_title} episodes",
            auto_suggest=False,
            redirect=True,
        )
        html_to_scrape = await _get_page_html(list_page.url)
    except wikipedia.exceptions.PageError:
        # Fallback to the main show page if the list page doesn't exist
        try:
            main_page = await asyncio.to_thread(
                wikipedia.page, show_title, auto_suggest=True, redirect=True
            )
            html_to_scrape = await _get_page_html(main_page.url)
        except Exception as e:
            logger.error(f"[WIKI] Failed to fetch page for '{show_title}': {e}")
            return None
    except Exception as e:
        logger.error(f"[WIKI] Unexpected error when fetching page: {e}")
        return None

    if not html_to_scrape:
        return None

    soup = BeautifulSoup(html_to_scrape, "lxml")
    overview_table = None

    # Find the "Series overview" table
    for table in soup.find_all("table", class_="wikitable"):
        if not isinstance(table, Tag):
            continue

        header_row = table.find("tr")
        if not isinstance(header_row, Tag):
            continue

        headers = [th.get_text(strip=True).lower() for th in header_row.find_all("th")]
        # Check if this looks like the right table
        if headers and "season" in headers[0] and any("episode" in h for h in headers):
            overview_table = table
            break

    if not isinstance(overview_table, Tag):
        return None

    # Find the column index for "Episodes"
    header_row = overview_table.find("tr")
    if not isinstance(header_row, Tag):
        return None

    header_cells = [th.get_text(strip=True).lower() for th in header_row.find_all("th")]
    episodes_col_index = -1
    for idx, text in enumerate(header_cells):
        if "episode" in text:
            episodes_col_index = idx
            break

    if episodes_col_index == -1:
        return None

    # Find the specific season in the table
    for row in overview_table.find_all("tr")[1:]:
        if not isinstance(row, Tag):
            continue

        cells = row.find_all(["td", "th"])
        if (
            len(cells) <= episodes_col_index
        ):  # Ensure the episode column exists in this row
            continue

        season_num = extract_first_int(cells[0].get_text(strip=True))
        if season_num == season:
            ep_text = cells[episodes_col_index].get_text(strip=True)
            count = extract_first_int(ep_text)
            return count  # Return the extracted episode count

    return None


# --- Torrent Site Scraping ---


async def scrape_1337x(
    query: str,
    media_type: str,
    search_url_template: str,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    base_query_for_filter: str | None = None,
    **kwargs,
) -> list[dict[str, Any]]:
    """
    Scrapes 1337x.to for torrents. It now correctly performs all network
    requests within a single client session to prevent closure errors.
    """
    search_config = context.bot_data.get("SEARCH_CONFIG", {})
    prefs_key = "movies" if "movie" in media_type else "tv"
    preferences = search_config.get("preferences", {}).get(prefs_key, {})

    if not preferences:
        logger.warning(
            f"[SCRAPER] No preferences found for '{prefs_key}'. Cannot score 1337x results."
        )
        return []

    formatted_query = urllib.parse.quote_plus(query)
    search_url = search_url_template.replace("{query}", formatted_query)
    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    }

    results = []
    best_match_base_name = "N/A"

    try:
        # CORRECTED: The 'async with' block now wraps ALL network activity.
        async with httpx.AsyncClient(
            headers=headers, timeout=30, follow_redirects=True
        ) as client:
            # --- Initial Search Request ---
            logger.info(
                f"[SCRAPER] 1337x Stage 1: Scraping candidates from {search_url}"
            )
            response = await client.get(search_url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")

            # --- Stage 1: Scrape candidates ---
            candidates = []
            table_body = soup.find("tbody")
            if not isinstance(table_body, Tag):
                return []

            for row in table_body.find_all("tr"):
                if not isinstance(row, Tag) or len(row.find_all("td")) < 2:
                    continue
                name_cell = row.find_all("td")[0]
                if (
                    not isinstance(name_cell, Tag)
                    or len(links := name_cell.find_all("a")) < 2
                ):
                    continue
                title = links[1].get_text(strip=True)
                parsed_info = parse_torrent_name(title)
                base_name = parsed_info.get("title")
                if title and base_name:
                    candidates.append(
                        {
                            "title": title,
                            "base_name": base_name,
                            "row_element": row,
                            "parsed_info": parsed_info,
                        }
                    )

            if not candidates:
                logger.warning("[SCRAPER] 1337x: Found no candidates on page.")
                return []

            # --- Stage 2: Identify best match ---
            filter_query = base_query_for_filter or query
            candidates = [
                c
                for c in candidates
                if fuzz.ratio(filter_query.lower(), c["base_name"].lower()) > 85
            ]

            if not candidates:
                logger.warning(
                    f"[SCRAPER] 1337x: No candidates survived fuzzy filter for query '{query}'."
                )
                return []

            base_name_counts = Counter(c["base_name"] for c in candidates)
            if not base_name_counts:
                return []

            best_match_base_name, _ = base_name_counts.most_common(1)[0]
            logger.info(
                f"[SCRAPER] 1337x Stage 2: Identified most common media name: '{best_match_base_name}'"
            )

            # --- Stage 3: Fetch detail pages and process torrents ---
            base_url = "https://1337x.to"
            for candidate in candidates:
                if candidate["base_name"] == best_match_base_name:
                    row = candidate["row_element"]
                    cells = row.find_all("td")
                    if len(cells) < 6:
                        continue

                    name_cell, seeds_cell, size_cell, uploader_cell = (
                        cells[0],
                        cells[1],
                        cells[4],
                        cells[5],
                    )
                    page_url_relative = name_cell.find_all("a")[1].get("href")
                    if not isinstance(page_url_relative, str):
                        continue

                    detail_page_url = f"{base_url}{page_url_relative}"

                    # This request now happens inside the active client session.
                    detail_response = await client.get(detail_page_url)
                    if detail_response.status_code != 200:
                        logger.warning(
                            f"Failed to fetch 1337x detail page {detail_page_url}, status: {detail_response.status_code}"
                        )
                        continue

                    detail_soup = BeautifulSoup(detail_response.text, "lxml")
                    magnet_tag = detail_soup.find("a", href=re.compile(r"^magnet:"))
                    if (
                        not magnet_tag
                        or not isinstance(magnet_tag, Tag)
                        or not (magnet_link := magnet_tag.get("href"))
                    ):
                        logger.warning(
                            f"Could not find magnet link on page: {detail_page_url}"
                        )
                        continue

                    # Process the rest of the data
                    size_str = size_cell.get_text(strip=True)
                    seeds_str = seeds_cell.get_text(strip=True)
                    parsed_size_gb = _parse_size_to_gb(size_str)
                    uploader = (
                        uploader_cell.find("a").get_text(strip=True)
                        if uploader_cell.find("a")
                        else "Anonymous"
                    )
                    seeders_int = int(seeds_str) if seeds_str.isdigit() else 0
                    score = score_torrent_result(
                        candidate["title"], uploader, preferences, seeders=seeders_int
                    )

                    if score > 0 and isinstance(magnet_link, str):
                        results.append(
                            {
                                "title": candidate["title"],
                                "page_url": magnet_link,
                                "score": score,
                                "source": "1337x",
                                "uploader": uploader,
                                "size_gb": parsed_size_gb,
                                "codec": _parse_codec(candidate["title"]),
                                "seeders": seeders_int,
                                "year": candidate["parsed_info"].get("year"),
                            }
                        )

    except Exception as e:
        logger.error(f"[SCRAPER ERROR] 1337x scrape failed: {e}", exc_info=True)
        return []

    logger.info(
        f"[SCRAPER] 1337x Stage 3: Found {len(results)} relevant torrents for '{best_match_base_name}'."
    )
    return results


async def scrape_yts(
    query: str,
    media_type: str,
    search_url_template: str,
    context: ContextTypes.DEFAULT_TYPE,
    **kwargs,
) -> list[dict[str, Any]]:
    """Uses the YTS.mx API and website to find movie torrents."""
    year = kwargs.get("year")
    resolution = kwargs.get("resolution")
    logger.info(
        f"[SCRAPER] YTS: Initiating API-based scrape for '{query}' (Year: {year}, Res: {resolution})."
    )

    preferences = (
        context.bot_data.get("SEARCH_CONFIG", {})
        .get("preferences", {})
        .get("movies", {})
    )
    if not preferences:
        return []

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }

            # Stage 1: Scrape search results to find the movie's page URL
            formatted_query = urllib.parse.quote_plus(query)
            search_url = search_url_template.replace("{query}", formatted_query)
            response = await client.get(search_url, headers=headers)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")

            choices = {}
            for movie_wrapper in soup.find_all("div", class_="browse-movie-wrap"):
                # --- Refactored Type Check: Ensure movie_wrapper is a Tag to resolve IDE errors ---
                if not isinstance(movie_wrapper, Tag):
                    continue

                year_tag = movie_wrapper.find("div", class_="browse-movie-year")
                scraped_year = (
                    year_tag.get_text(strip=True) if isinstance(year_tag, Tag) else None
                )
                if year and scraped_year and year != scraped_year:
                    continue

                title_tag = movie_wrapper.find("a", class_="browse-movie-title")
                if isinstance(title_tag, Tag):
                    if (href := title_tag.get("href")) and (
                        title_text := title_tag.get_text(strip=True)
                    ):
                        choices[href] = title_text

            if not choices:
                logger.warning(
                    f"[SCRAPER] YTS Stage 1: No movies found matching year '{year}'."
                )
                return []

            best_match = process.extractOne(query, choices, scorer=fuzz.ratio)

            # --- Refactored Match Validation: Safely handle potentially incorrect type stubs from `thefuzz` ---
            # `thefuzz` can return a 3-element tuple (choice, score, key) when choices is a dict.
            # This check is safer than the original complex single-line check.
            if not (best_match and len(best_match) == 3 and best_match[1] > 85):
                logger.warning(
                    f"[SCRAPER] YTS Stage 1: No confident match found for '{query}'. Best was: {best_match}"
                )
                return []

            # The URL is the third element (the key from the choices dict).
            best_page_url = best_match[2]
            if not isinstance(best_page_url, str):
                logger.error(
                    f"[SCRAPER ERROR] YTS Stage 1: Matched item key was not a string URL. Got: {best_page_url}"
                )
                return []

            # Stage 2: Scrape the movie's page to get its API ID
            response = await client.get(best_page_url, headers=headers)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")

            movie_info_div = soup.select_one("#movie-info")
            if not (
                isinstance(movie_info_div, Tag)
                and (movie_id := movie_info_div.get("data-movie-id"))
            ):
                logger.error(
                    f"[SCRAPER ERROR] YTS Stage 2: Could not find data-movie-id on page {best_page_url}"
                )
                return []

            # Stage 3: Call the YTS API with the movie ID
            api_url = f"https://yts.mx/api/v2/movie_details.json?movie_id={movie_id}"
            response = await client.get(api_url)
            response.raise_for_status()
            api_data = response.json()

            if api_data.get("status") != "ok" or "movie" not in api_data.get(
                "data", {}
            ):
                logger.error(
                    f"[SCRAPER ERROR] YTS API returned an error: {api_data.get('status_message')}"
                )
                return []

            # Stage 4: Parse the API response
            results = []
            movie_data = api_data["data"]["movie"]
            movie_title = movie_data.get("title_long", query)

            for torrent in movie_data.get("torrents", []):
                quality = torrent.get("quality", "").lower()
                if not resolution or (resolution and resolution in quality):
                    size_gb = torrent.get("size_bytes", 0) / (1024**3)
                    if size_gb > 7.0:
                        continue

                    full_title = f"{movie_title} [{torrent.get('quality')}.{torrent.get('type')}] [YTS.MX]"
                    if info_hash := torrent.get("hash"):
                        trackers = "&tr=" + "&tr=".join(
                            [
                                "udp://open.demonii.com:1337/announce",
                                "udp://tracker.openbittorrent.com:80",
                            ]
                        )
                        magnet_link = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote_plus(movie_title)}{trackers}"

                        seeders_count = torrent.get("seeds", 0)  # <--- ADD THIS
                        parsed_codec = (
                            _parse_codec(full_title) or "x264"  # Default YTS to x264
                        )
                        score = score_torrent_result(
                            full_title, "YTS", preferences, seeders=seeders_count
                        )  # <--- CHANGE THIS

                        results.append(
                            {
                                "title": full_title,
                                "page_url": magnet_link,
                                "score": score,
                                "source": "YTS.mx",
                                "uploader": "YTS",
                                "size_gb": size_gb,
                                "codec": parsed_codec,
                                "seeders": seeders_count,
                                "year": movie_data.get("year"),  # <-- NEWLY ADDED
                            }
                        )

            logger.info(
                f"[SCRAPER] YTS API scrape finished. Found {len(results)} matching torrents."
            )
            return results

    except Exception as e:
        logger.error(f"[SCRAPER ERROR] YTS scrape failed: {e}", exc_info=True)
        return []


# --- Generic Web Page Scraping ---


async def find_magnet_link_on_page(url: str) -> list[str]:
    """
    Fetches a web page and finds all unique magnet links.
    """
    unique_magnet_links: set[str] = set()
    logger.info(f"[WEBSCRAPE] Fetching URL: {url}")

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()

        soup = BeautifulSoup(response.text, "lxml")
        magnet_link_tags = soup.find_all("a", href=re.compile(r"^magnet:"))

        for tag in magnet_link_tags:
            if isinstance(tag, Tag):
                if href := tag.get("href"):
                    if isinstance(href, str):
                        unique_magnet_links.add(href)

        if unique_magnet_links:
            logger.info(
                f"Found {len(unique_magnet_links)} unique magnet link(s) on page."
            )
        else:
            logger.warning(f"No magnet links found on page: {url}")

    except httpx.RequestError as e:
        logger.error(f"[WEBSCRAPE ERROR] HTTP Request failed for {url}: {e}")
    except Exception as e:
        logger.error(
            f"[WEBSCRAPE ERROR] Unexpected error scraping {url}: {e}", exc_info=True
        )

    return list(unique_magnet_links)