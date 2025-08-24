# telegram_bot/services/scraping_service.py

import asyncio
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
from ..utils import extract_first_int


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


def _extract_torrent_info(row: Tag | None, link: Tag) -> dict[str, Any]:
    """Extract basic torrent details from a table row and its link."""

    title = link.get_text(strip=True)
    href = link.get("href") if isinstance(link, Tag) else None
    seeders = leechers = 0
    size_text = ""
    uploader = "Unknown"

    if isinstance(row, Tag):
        cells = row.find_all(["td", "th"])
        for cell in cells[1:]:
            if not isinstance(cell, Tag):
                continue
            text = cell.get_text(strip=True)
            if not size_text and re.search(
                r"[\d.]+\s*(?:GB|MB|KB)", text, re.IGNORECASE
            ):
                size_text = text
                continue
            if seeders == 0 and text.isdigit():
                seeders = int(text)
                continue
            if leechers == 0 and text.isdigit():
                leechers = int(text)
                continue
            uploader = text or uploader

    return {
        "href": href,
        "title": title,
        "seeders": seeders,
        "leechers": leechers,
        "size": size_text,
        "uploader": uploader,
    }


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


def _strategy_find_direct_links(
    soup: BeautifulSoup, query: str
) -> list[dict[str, Any]]:
    """Locate direct magnet/``.torrent`` links and extract their info."""

    results: list[dict[str, Any]] = []
    for tag in soup.find_all("a", href=True):
        if not isinstance(tag, Tag):
            continue

        href = tag.get("href")
        if not isinstance(href, str):
            continue

        if href.startswith("magnet:") or href.endswith(".torrent"):
            row = tag.find_parent("tr")
            info = _extract_torrent_info(row, tag)
            results.append(info)

    return results


def _strategy_contextual_search(
    soup: BeautifulSoup, query: str
) -> list[dict[str, Any]]:
    """Find links whose surrounding text hints at a torrent download."""

    results: list[dict[str, Any]] = []
    if not isinstance(query, str) or not query.strip():
        return results

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
            if href.startswith("magnet:") or href.endswith(".torrent"):
                row = tag.find_parent("tr")
                results.append(_extract_torrent_info(row, tag))

    return results


def _strategy_find_in_tables(soup: BeautifulSoup, query: str) -> list[dict[str, Any]]:
    """Inspect tables for rows relevant to ``query`` and extract their links."""

    results: list[dict[str, Any]] = []
    if not isinstance(query, str) or not query.strip():
        return results

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
            link_tag = row.find("a", href=True)
            if not isinstance(link_tag, Tag):
                continue
            href = link_tag.get("href")
            if not isinstance(href, str):
                continue
            if href.startswith("magnet:") or href.endswith(".torrent"):
                results.append(_extract_torrent_info(row, link_tag))

    return results


async def scrape_generic_page(
    query: str, media_type: str, search_url: str
) -> list[dict[str, Any]]:
    """Scrape a generic HTML page and return structured torrent results."""

    if not query.strip() or not search_url.strip():
        return []

    html = await _get_page_html(search_url)
    if not html:
        return []

    soup = BeautifulSoup(html, "lxml")
    candidates = (
        _strategy_find_direct_links(soup, query)
        + _strategy_contextual_search(soup, query)
        + _strategy_find_in_tables(soup, query)
    )

    # Deduplicate by magnet/URL
    unique: dict[str, dict[str, Any]] = {}
    for item in candidates:
        href = item.get("href")
        if isinstance(href, str) and href not in unique:
            unique[href] = item

    preferences: dict[str, Any] = {}  # Preferences unavailable without context
    results = []
    for item in unique.values():
        href = item.get("href")
        title = item.get("title") or query
        uploader = item.get("uploader", "Unknown")
        seeders = item.get("seeders", 0)
        size_gb = _parse_size_to_gb(item.get("size", ""))
        score = score_torrent_result(title, uploader, preferences, seeders=seeders)

        results.append(
            {
                "title": title,
                "page_url": href,
                "score": score,
                "source": "generic",
                "uploader": uploader,
                "size_gb": size_gb,
                "codec": _parse_codec(title),
                "seeders": seeders,
                "year": item.get("year"),
            }
        )

    return results
