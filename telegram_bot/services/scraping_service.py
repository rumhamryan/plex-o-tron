# telegram_bot/services/scraping_service.py

import asyncio
from pathlib import Path
import re
import urllib.parse
import time
from typing import Any

import httpx
import wikipedia
from bs4 import BeautifulSoup, Tag
import yaml
from telegram.ext import ContextTypes
from thefuzz import fuzz, process

from ..config import logger
from .search_logic import _parse_codec, score_torrent_result
from ..utils import extract_first_int, parse_torrent_name
from .generic_torrent_scraper import GenericTorrentScraper, load_site_config

# --- Wikipedia caching (per-process) ---
# Caches per-season episode titles and corrected show title to avoid repeated
# network requests and HTML parsing during season workflows.
_WIKI_TITLES_CACHE: dict[tuple[str, int], tuple[dict[int, str], str | None]] = {}
_WIKI_SOUP_CACHE: dict[str, BeautifulSoup] = {}


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
    cache_key = (show_title.strip().lower(), season)

    # Fast path: return from cache if available
    cached = _WIKI_TITLES_CACHE.get(cache_key)
    if cached:
        titles_map, corrected = cached
        return titles_map.get(episode), corrected
    canonical_title = show_title
    main_page_url: str | None = None

    # --- Step 1: Find the main show page to get the canonical, corrected title ---
    try:
        logger.info(
            f"[WIKI] Step 1: Finding main page to correct title for '{show_title}'"
        )
        search_results = await asyncio.to_thread(wikipedia.search, show_title)
        if not search_results:
            logger.error(
                f"[WIKI] No Wikipedia page found for '{show_title}'. Aborting."
            )
            return None, None

        main_page_title = search_results[0]
        main_page = await asyncio.to_thread(
            wikipedia.page, main_page_title, auto_suggest=False, redirect=True
        )
        main_page_url = main_page.url

        if main_page.title != show_title:
            corrected_show_title = main_page.title
            canonical_title = main_page.title
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
    # Populate cache for the whole season while we’re here
    titles_map = await _extract_titles_for_season(soup, season)
    if titles_map:
        _WIKI_TITLES_CACHE[cache_key] = (titles_map, corrected_show_title)
    episode_title = titles_map.get(episode) if titles_map else None

    if not episode_title:
        logger.warning(
            f"[WIKI] All parsing strategies failed to find S{season:02d}E{episode:02d}."
        )

    return episode_title, corrected_show_title


async def fetch_episode_titles_for_season(
    show_title: str, season: int
) -> tuple[dict[int, str], str | None]:
    """Fetch all episode titles for a given season in one pass.

    Returns a mapping of episode number to title and an optional corrected
    show title if Wikipedia redirects.
    Results are cached per (show_title, season).
    """
    cache_key = (show_title.strip().lower(), season)
    cached = _WIKI_TITLES_CACHE.get(cache_key)
    if cached:
        logger.info(
            f"[WIKI] Cache hit for episode titles: '{show_title}' S{season:02d}."
        )
        return cached[0], cached[1]

    corrected_show_title: str | None = None
    canonical_title = show_title
    main_page_url: str | None = None

    # Step 1: Resolve main show page to get canonical title
    try:
        logger.info(
            f"[WIKI] Resolving main show page for '{show_title}' to determine canonical title."
        )
        search_results = await asyncio.to_thread(wikipedia.search, show_title)
        if not search_results:
            logger.warning(f"[WIKI] No Wikipedia search results for '{show_title}'.")
            return {}, None
        main_page_title = search_results[0]
        main_page = await asyncio.to_thread(
            wikipedia.page, main_page_title, auto_suggest=False, redirect=True
        )
        main_page_url = main_page.url
        if main_page.title != show_title:
            corrected_show_title = main_page.title
            canonical_title = main_page.title
            logger.info(
                f"[WIKI] Title corrected: '{show_title}' -> '{canonical_title}'."
            )
        logger.debug(f"[WIKI] Main page URL: {main_page_url}")
    except Exception:
        logger.error(
            f"[WIKI] Failed resolving main page for '{show_title}'. Continuing without correction."
        )
        return {}, None

    # Step 2: Prefer dedicated list page; fallback to main page
    html_to_scrape: str | None = None
    try:
        direct_query = f"List of {canonical_title} episodes"
        logger.info(f"[WIKI] Attempting dedicated list page lookup: '{direct_query}'.")
        list_page = await asyncio.to_thread(
            wikipedia.page, direct_query, auto_suggest=False, redirect=True
        )
        logger.debug(f"[WIKI] List page URL: {list_page.url}")
        html_to_scrape = await _get_page_html(list_page.url)
    except Exception:
        if main_page_url:
            logger.info(
                f"[WIKI] Dedicated list page not found. Falling back to main page for '{canonical_title}'."
            )
            html_to_scrape = await _get_page_html(main_page_url)

    if not html_to_scrape:
        logger.warning(
            f"[WIKI] No HTML retrieved for '{canonical_title}'. Returning empty titles."
        )
        return {}, corrected_show_title

    soup = BeautifulSoup(html_to_scrape, "lxml")
    titles_map = await _extract_titles_for_season(soup, season)
    if titles_map:
        logger.info(
            f"[WIKI] Parsed {len(titles_map)} episode titles for '{canonical_title}' S{season:02d}."
        )
        _WIKI_TITLES_CACHE[cache_key] = (titles_map, corrected_show_title)
    return titles_map, corrected_show_title


async def _parse_episode_tables(
    soup: BeautifulSoup, season: int, episode: int
) -> str | None:
    """
    Orchestrates different strategies to parse episode titles from tables.

    This function now intelligently calls different, specialized parsing functions
    based on the likely structure of the page (dedicated vs. embedded).
    """
    logger.info("[WIKI] Parsing HTML for episode tables.")

    # --- Strategy for DEDICATED pages: Look for explicit Season headers/links ---
    season_header_pattern = re.compile(rf"Season\s+{season}", re.IGNORECASE)
    header_tag = soup.find(
        lambda tag: tag.name in ["h2", "h3"]
        and bool(season_header_pattern.search(tag.get_text()))
    )
    if isinstance(header_tag, Tag):
        target_table = header_tag.find_next("table", class_="wikitable")
        if isinstance(target_table, Tag):
            logger.info("[WIKI] Found explicit season header. Using DEDICATED parser.")
            return await _extract_title_from_dedicated_table(
                target_table, season, episode
            )

    # --- Strategy for EMBEDDED lists: Look for a generic "Episodes" header ---
    episodes_header_pattern = re.compile(r"Episodes", re.IGNORECASE)
    episodes_header_tag = soup.find(
        lambda tag: tag.name in ["h2", "h3"]
        and bool(episodes_header_pattern.search(tag.get_text()))
    )
    if isinstance(episodes_header_tag, Tag):
        target_table = episodes_header_tag.find_next("table", class_="wikitable")
        if isinstance(target_table, Tag):
            logger.info(
                "[WIKI] Found generic 'Episodes' header. Using EMBEDDED parser."
            )
            return await _extract_title_from_embedded_table(
                target_table, season, episode
            )

    logger.warning(
        f"[WIKI] All parsing strategies failed to find S{season:02d}E{episode:02d}."
    )
    return None


async def _extract_titles_for_season(
    soup: BeautifulSoup, season: int
) -> dict[int, str]:
    """Extracts a mapping of episode number -> title for a given season.

    Uses the same heuristics as _parse_episode_tables, but returns all titles
    for the season in one pass.
    """
    results: dict[int, str] = {}

    # Strategy 1: dedicated season table under a specific Season header
    season_header_pattern = re.compile(rf"Season\s+{season}", re.IGNORECASE)
    header_tag = soup.find(
        lambda tag: tag.name in ["h2", "h3"]
        and bool(season_header_pattern.search(tag.get_text()))
    )
    if isinstance(header_tag, Tag):
        target_table = header_tag.find_next("table", class_="wikitable")
        if isinstance(target_table, Tag):
            for row in target_table.find_all("tr")[1:]:
                if not isinstance(row, Tag):
                    continue
                cells = row.find_all(["th", "td"])
                if len(cells) < 3:
                    continue
                try:
                    ep_num = extract_first_int(cells[1].get_text(strip=True))
                    if not ep_num:
                        continue
                    title_cell = cells[2]
                    if not isinstance(title_cell, Tag):
                        continue
                    found_text = title_cell.find(string=re.compile(r'"([^"]+)"'))
                    if found_text:
                        results[ep_num] = str(found_text).strip().strip('"')
                        continue
                    italic_text = title_cell.find("i")
                    if italic_text:
                        results[ep_num] = italic_text.get_text(strip=True)
                        continue
                    results[ep_num] = title_cell.get_text(strip=True).strip('"')
                except Exception:
                    continue
            if results:
                return results

    # Strategy 2: simpler embedded episodes table under a generic header
    episodes_header_pattern = re.compile(r"Episodes", re.IGNORECASE)
    episodes_header_tag = soup.find(
        lambda tag: tag.name in ["h2", "h3"]
        and bool(episodes_header_pattern.search(tag.get_text()))
    )
    if isinstance(episodes_header_tag, Tag):
        target_table = episodes_header_tag.find_next("table", class_="wikitable")
        if isinstance(target_table, Tag):
            for row in target_table.find_all("tr")[1:]:
                if not isinstance(row, Tag):
                    continue
                cells = row.find_all(["td", "th"])
                if len(cells) < 2:
                    continue
                try:
                    ep_num = extract_first_int(cells[0].get_text(strip=True))
                    if not ep_num:
                        continue
                    title_cell = cells[1]
                    if not isinstance(title_cell, Tag):
                        continue
                    found_text = title_cell.find(string=re.compile(r'"([^"]+)"'))
                    if found_text:
                        results[ep_num] = str(found_text).strip().strip('"')
                        continue
                    italic_text = title_cell.find("i")
                    if italic_text:
                        results[ep_num] = italic_text.get_text(strip=True)
                        continue
                    results[ep_num] = title_cell.get_text(strip=True).strip('"')
                except Exception:
                    continue

    return results


async def _extract_title_from_dedicated_table(
    table: Tag, season: int, episode: int
) -> str | None:
    """
    (FOR DEDICATED PAGES) Extracts a title from a complex wikitable, typically
    found on "List of..." episode pages. It assumes a multi-column layout
    that may include overall episode numbers.
    """
    # This logic is restored from the previously working version for dedicated pages
    for row in table.find_all("tr")[1:]:  # Skip header row
        if not isinstance(row, Tag):
            continue

        cells = row.find_all(["th", "td"])
        if len(cells) < 3:
            continue

        try:
            # Column 1 is "No. in season" on these pages
            episode_cell_text = cells[1].get_text(strip=True)
            if extract_first_int(episode_cell_text) != episode:
                continue

            # Column 2 is the title
            title_cell = cells[2]
            if not isinstance(title_cell, Tag):
                continue

            # Find title (preferring text in quotes)
            found_text = title_cell.find(string=re.compile(r'"([^"]+)"'))
            if found_text:
                return str(found_text).strip().strip('"')
            italic_text = title_cell.find("i")
            if italic_text:
                return italic_text.get_text(strip=True)
            return title_cell.get_text(strip=True).strip('"')
        except (ValueError, IndexError):
            continue
    return None


async def _extract_title_from_embedded_table(
    table: Tag, season: int, episode: int
) -> str | None:
    """
    (FOR EMBEDDED PAGES) Extracts a title from a simpler table structure,
    typically found on a show's main page under an "Episodes" header.
    """
    for row in table.find_all("tr")[1:]:  # Skip header
        if not isinstance(row, Tag):
            continue

        cells = row.find_all(["td", "th"])
        if len(cells) < 2:  # Embedded tables often have just "No." and "Title"
            continue

        try:
            # Column 0 is "No." in this simpler format
            episode_cell_text = cells[0].get_text(strip=True)
            if extract_first_int(episode_cell_text) != episode:
                continue

            # Column 1 is the title
            title_cell = cells[1]
            if not isinstance(title_cell, Tag):
                continue

            # Same reliable title extraction logic
            found_text = title_cell.find(string=re.compile(r'"([^"]+)"'))
            if found_text:
                return str(found_text).strip().strip('"')
            italic_text = title_cell.find("i")
            if italic_text:
                return italic_text.get_text(strip=True)
            return title_cell.get_text(strip=True).strip('"')
        except (ValueError, IndexError):
            continue
    return None


async def fetch_season_episode_count_from_wikipedia(
    show_title: str, season: int
) -> int | None:
    """Fetches the number of episodes for a given season from Wikipedia."""
    logger.info(
        f"[WIKI] Fetching episode count for '{show_title}' S{season:02d} from Wikipedia."
    )
    html_to_scrape = None
    try:
        # Attempt to get the dedicated "List of..." page first
        logger.debug(
            f"[WIKI] Trying dedicated list page for '{show_title}': 'List of {show_title} episodes'."
        )
        list_page = await asyncio.to_thread(
            wikipedia.page,
            f"List of {show_title} episodes",
            auto_suggest=False,
            redirect=True,
        )
        logger.debug(f"[WIKI] List page URL: {list_page.url}")
        html_to_scrape = await _get_page_html(list_page.url)
    except wikipedia.exceptions.PageError:
        # Fallback to the main show page if the list page doesn't exist
        try:
            logger.debug(
                f"[WIKI] Dedicated list page missing. Falling back to main page for '{show_title}'."
            )
            main_page = await asyncio.to_thread(
                wikipedia.page, show_title, auto_suggest=True, redirect=True
            )
            logger.debug(f"[WIKI] Main page URL: {main_page.url}")
            html_to_scrape = await _get_page_html(main_page.url)
        except Exception as e:
            logger.error(f"[WIKI] Failed to fetch page for '{show_title}': {e}")
            return None
    except Exception as e:
        logger.error(f"[WIKI] Unexpected error when fetching page: {e}")
        return None

    if not html_to_scrape:
        logger.warning(
            f"[WIKI] No HTML retrieved for '{show_title}' S{season:02d}. Unable to determine episode count."
        )
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
        logger.debug(f"[WIKI] 'Series overview' table not found for '{show_title}'.")
        return None

    # Find the column index for "Episodes"
    header_row = overview_table.find("tr")
    if not isinstance(header_row, Tag):
        logger.debug(
            f"[WIKI] Header row not found in overview table for '{show_title}'."
        )
        return None

    header_cells = [th.get_text(strip=True).lower() for th in header_row.find_all("th")]
    episodes_col_index = -1
    for idx, text in enumerate(header_cells):
        if "episode" in text:
            episodes_col_index = idx
            break

    if episodes_col_index == -1:
        logger.debug(
            f"[WIKI] Could not locate 'Episodes' column in overview table for '{show_title}'."
        )
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
            logger.info(
                f"[WIKI] Episode count for '{show_title}' S{season:02d}: {count}"
            )
            return count  # Return the extracted episode count

    return None


# --- Torrent Site Scraping ---


async def scrape_1337x(
    query: str,
    media_type: str,
    search_url_template: str,
    context: ContextTypes.DEFAULT_TYPE,
    limit: int = 15,
    **kwargs,
) -> list[dict[str, Any]]:
    """Scrape 1337x using the generic scraper framework."""

    prefs_key = "movies" if "movie" in media_type else "tv"
    preferences = (
        context.bot_data.get("SEARCH_CONFIG", {})
        .get("preferences", {})
        .get(prefs_key, {})
    )
    if not preferences:
        logger.warning(
            f"[SCRAPER] No preferences found for '{prefs_key}'. Cannot score 1337x results."
        )
        return []

    try:
        config_path = (
            Path(__file__).resolve().parent.parent
            / "scrapers"
            / "configs"
            / "1337x.yaml"
        )
        site_config = load_site_config(config_path)
    except Exception as exc:
        logger.error(f"[SCRAPER] Failed to load 1337x config: {exc}")
        return []

    scraper = GenericTorrentScraper(site_config)
    base_filter = kwargs.get("base_query_for_filter")
    raw_results = await scraper.search(
        query, media_type, base_query_for_filter=base_filter, limit=limit
    )

    results: list[dict[str, Any]] = []
    for item in raw_results:
        score = score_torrent_result(
            item.name, item.uploader or "", preferences, seeders=item.seeders
        )
        if score <= 0:
            continue
        parsed_name = parse_torrent_name(item.name)
        results.append(
            {
                "title": item.name,
                "page_url": item.magnet_url,
                "score": score,
                "source": item.source_site,
                "uploader": item.uploader or "Anonymous",
                "size_gb": item.size_bytes / (1024**3),
                "codec": _parse_codec(item.name),
                "seeders": item.seeders,
                "leechers": item.leechers,
                "year": parsed_name.get("year"),
            }
        )

    logger.info(f"[SCRAPER] 1337x: Found {len(results)} torrents for query '{query}'.")
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

            # Stage 3: Call the YTS API with the movie ID and validate
            api_url = f"https://yts.mx/api/v2/movie_details.json?movie_id={movie_id}"
            api_data: dict[str, Any] | None = None
            movie_data: dict[str, Any] | None = None
            torrents: list[dict[str, Any]] = []

            for attempt in range(1, 4):
                delay = 2 ** (attempt - 1)
                api_start = time.perf_counter()
                try:
                    response = await client.get(api_url)
                    duration = time.perf_counter() - api_start
                    response.raise_for_status()
                    api_data = response.json()
                except Exception as e:
                    logger.debug(
                        (
                            "[SCRAPER] YTS API attempt %s request error: %s. "
                            "Retrying in %ss."
                        ),
                        attempt,
                        e,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                movie_data = api_data.get("data", {}).get("movie")
                torrents = movie_data.get("torrents", []) if movie_data else []
                conditions = []
                status = api_data.get("status")
                if status != "ok":
                    conditions.append(f"status != 'ok' (got {status!r})")
                if not movie_data:
                    conditions.append("missing 'movie' object")
                if movie_data and not torrents:
                    conditions.append("missing 'torrents' entries")

                if conditions:
                    logger.debug(
                        (
                            "[SCRAPER] YTS API attempt %s failed validation: %s. "
                            "Found %s torrents, expected >=1. Retrying in %ss."
                        ),
                        attempt,
                        "; ".join(conditions),
                        len(torrents),
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                logger.debug(
                    (
                        "[SCRAPER] YTS API attempt %s succeeded in %.2fs "
                        "with %d torrents."
                    ),
                    attempt,
                    duration,
                    len(torrents),
                )
                break
            else:
                logger.error(
                    (
                        "[SCRAPER ERROR] YTS API validation failed after 3 attempts "
                        f"for movie id {movie_id}."
                    )
                )
                return []

            # Stage 4: Parse the API response
            results: list[dict[str, Any]] = []
            movie_title = movie_data.get("title_long", query)

            for torrent in torrents:
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


# --- Generic Web Scraper Strategies ---


def _strategy_find_direct_links(soup: BeautifulSoup) -> set[str]:
    """Find anchors that directly link to magnet or ``.torrent`` files."""

    found_links: set[str] = set()
    # Anchor tags are the most reliable indicators of downloadable content.
    for tag in soup.find_all("a", href=True):
        if isinstance(tag, Tag):  # Add this check
            href = tag.get("href")
            if not isinstance(href, str):
                continue
            if href.startswith("magnet:"):
                found_links.add(href)
            elif href.endswith(".torrent"):
                # Relative ``.torrent`` paths are returned as-is; the caller may resolve them.
                found_links.add(href)
    return found_links


def _strategy_contextual_search(soup: BeautifulSoup, query: str) -> set[str]:
    """Find links whose surrounding text hints at a torrent download."""

    if not isinstance(query, str) or not query.strip():
        return set()

    potential_links: set[str] = set()
    keywords = {"magnet", "torrent", "download", "1080p", "720p", "x265"}
    query_lc = query.lower()

    for tag in soup.find_all("a", href=True):
        if not isinstance(tag, Tag):
            continue

        href = tag.get("href")
        if not isinstance(href, str):
            continue

        text_lc = tag.get_text(strip=True).lower()
        parent_text_lc = (
            tag.parent.get_text(" ", strip=True).lower()
            if isinstance(tag.parent, Tag)
            else ""
        )
        href_lc = href.lower()

        keyword_match = any(
            kw in text_lc or kw in parent_text_lc or kw in href_lc for kw in keywords
        )
        query_match = max(
            fuzz.partial_ratio(query_lc, text_lc),
            fuzz.partial_ratio(query_lc, parent_text_lc),
        )

        if keyword_match or query_match > 80:
            potential_links.add(href)

    return potential_links


def _strategy_find_in_tables(soup: BeautifulSoup, query: str) -> dict[str, float]:
    """Inspect tables for rows relevant to ``query`` and score their links."""

    if not isinstance(query, str) or not query.strip():
        return {}

    scored_links: dict[str, float] = {}
    query_lc = query.lower()

    for table in soup.find_all("table"):
        if not isinstance(table, Tag):
            continue

        for row in table.find_all("tr"):
            if not isinstance(row, Tag):
                continue

            row_text = row.get_text(" ", strip=True)
            match_score = fuzz.partial_ratio(query_lc, row_text.lower())
            if match_score <= 75:
                continue
            first_link = row.find("a", href=True)
            if first_link and isinstance(first_link, Tag):
                href = first_link.get("href")
                if isinstance(href, str):
                    scored_links[href] = float(match_score)

    return scored_links


def _score_candidate_links(
    links: set[str],
    query: str,
    table_links_scored: dict[str, float],
    soup: BeautifulSoup,
) -> str | None:
    """Score candidate links and return the highest scoring URL."""

    if not links or not isinstance(query, str) or not query.strip():
        return None

    query_lc = query.lower()
    best_link: str | None = None
    best_score = -1.0

    for link in links:
        score = 0.0

        if link.startswith("magnet:"):
            score += 100
        elif link.endswith(".torrent"):
            score += 50

        score += table_links_scored.get(link, 0)

        anchor = soup.find("a", href=link)
        if anchor:
            link_text_lc = anchor.get_text(strip=True).lower()
            score += fuzz.partial_ratio(query_lc, link_text_lc)

            # Penalise links that live inside obvious ad/comment containers.
            parent = anchor.parent
            while isinstance(parent, Tag):
                classes = " ".join(parent.get("class") or []).lower()
                element_id = str(parent.get("id") or "").lower()
                if "ad" in classes or "ads" in classes or "comment" in element_id:
                    score -= 50
                    break
                parent = parent.parent

        if score > best_score:
            best_score = score
            best_link = link

    return best_link


async def scrape_generic_page(
    query: str, media_type: str, search_url: str
) -> list[dict[str, Any]]:
    """High-level orchestrator that runs all strategies and selects the best link."""

    if not query.strip() or not search_url.strip():
        return []

    html = await _get_page_html(search_url)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    direct_links = _strategy_find_direct_links(soup)
    context_links = _strategy_contextual_search(soup, query)
    table_links_scored = _strategy_find_in_tables(soup, query)

    all_candidates = direct_links | context_links | set(table_links_scored)
    best_link = _score_candidate_links(all_candidates, query, table_links_scored, soup)

    if best_link:
        return [{"page_url": best_link, "source": "generic"}]
    return []


# --- YAML-backed Generic Scraper ---


def _find_site_config_by_name(site_name: str) -> Path | None:
    """Locate a YAML config whose 'site_name' matches the given name.

    This scans telegram_bot/scrapers/configs for .yaml files and reads only the
    'site_name' field to match quickly.
    """
    base = Path(__file__).resolve().parent.parent / "scrapers" / "configs"
    if not base.exists():
        return None
    for path in base.glob("*.yaml"):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict) and data.get("site_name") == site_name:
                return path
        except Exception:
            continue
    return None


async def scrape_yaml_site(
    query: str,
    media_type: str,
    _search_url_template: str,  # unused; present for call compatibility
    context: ContextTypes.DEFAULT_TYPE,
    *,
    site_name: str,
    limit: int = 15,
    base_query_for_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Generic path that uses a YAML config (GenericTorrentScraper).

    It looks up a YAML file whose 'site_name' matches and delegates scraping to
    GenericTorrentScraper. Results are mapped to the unified result schema.
    """
    prefs_key = "movies" if "movie" in media_type else "tv"
    preferences = (
        context.bot_data.get("SEARCH_CONFIG", {})
        .get("preferences", {})
        .get(prefs_key, {})
    )
    if not preferences:
        return []

    config_path = _find_site_config_by_name(site_name)
    if not isinstance(config_path, Path):
        logger.warning(
            "[SCRAPER] No YAML config found for site '%s' — skipping.", site_name
        )
        return []

    try:
        site_config = load_site_config(config_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("[SCRAPER] Failed to load YAML config for %s: %s", site_name, exc)
        return []

    scraper = GenericTorrentScraper(site_config)
    raw_results = await scraper.search(
        query, media_type, base_query_for_filter=base_query_for_filter, limit=limit
    )

    results: list[dict[str, Any]] = []
    for item in raw_results:
        score = score_torrent_result(
            item.name, item.uploader or "", preferences, seeders=item.seeders
        )
        if score <= 0:
            continue
        parsed_name = parse_torrent_name(item.name)
        results.append(
            {
                "title": item.name,
                "page_url": item.magnet_url,
                "score": score,
                "source": item.source_site,
                "uploader": item.uploader or "Anonymous",
                "size_gb": item.size_bytes / (1024**3),
                "codec": _parse_codec(item.name),
                "seeders": item.seeders,
                "leechers": item.leechers,
                "year": parsed_name.get("year"),
            }
        )

    logger.info(
        "[SCRAPER] %s: Found %d torrents for query '%s'.",
        site_name,
        len(results),
        query,
    )
    return results
