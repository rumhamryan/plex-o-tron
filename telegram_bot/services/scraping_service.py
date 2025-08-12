# telegram_bot/services/scraping_service.py

import asyncio
from collections import Counter
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
import wikipedia
from bs4 import BeautifulSoup, Tag
from telegram.ext import ContextTypes
from thefuzz import fuzz, process

from ..config import logger
from .search_logic import _parse_codec, _parse_size_to_gb, score_torrent_result
from ..utils import extract_first_int, parse_torrent_name


# --- Wikipedia Scraping ---


async def fetch_episode_title_from_wikipedia(
    show_title: str, season: int, episode: int
) -> Tuple[Optional[str], Optional[str]]:
    """
    Fetches an episode title from Wikipedia, trying a dedicated episode list page first,
    then falling back to the main show page.

    Returns:
        A tuple of (episode_title, corrected_show_title). The corrected title is
        returned if Wikipedia redirects the initial search.
    """
    html_to_scrape = None
    corrected_show_title: Optional[str] = None

    # Strategy 1: Try "List of..." page directly
    try:
        direct_query = f"List of {show_title} episodes"
        logger.info(
            f"[WIKI] Attempting to find dedicated episode page: '{direct_query}'"
        )
        page = await asyncio.to_thread(
            wikipedia.page, direct_query, auto_suggest=False, redirect=True
        )
        html_to_scrape = await asyncio.to_thread(page.html)
        logger.info("[WIKI] Found dedicated episode page.")

    except wikipedia.exceptions.PageError:
        # Strategy 2: Fallback to main show page
        logger.warning(
            f"[WIKI] No dedicated page found for '{show_title}'. Falling back to main show page."
        )
        try:
            page = await asyncio.to_thread(
                wikipedia.page, show_title, auto_suggest=True, redirect=True
            )
            html_to_scrape = await asyncio.to_thread(page.html)

            # Check if Wikipedia corrected the title for us
            if page.title != show_title:
                corrected_show_title = page.title
                logger.info(
                    f"[WIKI] Title was corrected: '{show_title}' -> '{corrected_show_title}'"
                )
            else:
                logger.info(
                    "[WIKI] Successfully found main show page with original title."
                )

        except Exception as e:
            logger.error(
                f"[WIKI] An unexpected error occurred during fallback page search: {e}"
            )
            return None, None

    except Exception as e:
        logger.error(
            f"[WIKI] An unexpected error occurred during direct Wikipedia search: {e}"
        )
        return None, None

    if not html_to_scrape:
        logger.error("[WIKI] All page search attempts failed.")
        return None, None

    # --- Step 2: Orchestrate the parsing strategies ---
    soup = BeautifulSoup(html_to_scrape, "lxml")
    episode_title = await _parse_dedicated_episode_page(soup, season, episode)
    if not episode_title:
        episode_title = await _parse_embedded_episode_page(soup, season, episode)

    if not episode_title:
        logger.warning(
            f"[WIKI] Both parsing strategies failed to find S{season:02d}E{episode:02d}."
        )

    return episode_title, corrected_show_title


async def _parse_dedicated_episode_page(
    soup: BeautifulSoup, season: int, episode: int
) -> Optional[str]:
    """
    (Primary Strategy) Parses a dedicated 'List of...' page by using the 'Series overview'
    table to calculate the exact index of the target season's table.
    """
    logger.info("[WIKI] Trying Primary Strategy: Index Calculation via Overview Table")
    all_tables = soup.find_all("table", class_="wikitable")
    if not all_tables:
        return None

    # Step 1: Find the "Series overview" table to use as an index
    index_table = None
    if isinstance(first_table := all_tables[0], Tag):
        if isinstance(first_row := first_table.find("tr"), Tag):
            headers = [th.get_text(strip=True) for th in first_row.find_all("th")]
            if headers and headers[0] == "Season":
                index_table = first_table

    if not isinstance(index_table, Tag):
        logger.info(
            "[WIKI] Could not find 'Series overview' table. Aborting Primary Strategy."
        )
        return None

    # Step 2: Calculate the target table's actual index
    target_table_index = -1
    current_table_index_counter = 0  # Index of table *after* the overview
    for row in index_table.find_all("tr")[1:]:
        if isinstance(row, Tag) and (cells := row.find_all(["th", "td"])):
            season_num_from_cell = extract_first_int(cells[0].get_text(strip=True))
            if season_num_from_cell == season:
                target_table_index = current_table_index_counter
                logger.info(
                    f"[WIKI] Match for Season {season} found. Calculated target table index: {target_table_index}"
                )
                break
        current_table_index_counter += 1

    if target_table_index == -1 or target_table_index >= len(all_tables):
        logger.warning(
            f"[WIKI] Could not find Season {season} in the index table or index is out of bounds."
        )
        return None

    # Step 3: Parse the correct table using the calculated index
    target_table = all_tables[target_table_index]
    if not isinstance(target_table, Tag):
        return None

    for row in target_table.find_all("tr")[1:]:
        if not isinstance(row, Tag):
            continue
        cells = row.find_all(["td", "th"])
        if len(cells) < 3:
            continue

        try:
            episode_num_from_cell = extract_first_int(cells[1].get_text(strip=True))
            if episode_num_from_cell == episode:
                title_cell = cells[2]
                if not isinstance(title_cell, Tag):
                    continue

                # Prefer title in quotes, fallback to full cell text
                found_text = title_cell.find(string=re.compile(r'"([^"]+)"'))
                if found_text:
                    cleaned_title = str(found_text).strip().strip('"')
                else:
                    cleaned_title = title_cell.get_text(strip=True)

                logger.info(
                    f"[SUCCESS] Found title via Primary Strategy: '{cleaned_title}'"
                )
                return cleaned_title
        except (ValueError, IndexError):
            continue

    logger.warning(
        "[WIKI] Primary Strategy failed to find the episode in the correct table."
    )
    return None


async def _parse_embedded_episode_page(
    soup: BeautifulSoup, season: int, episode: int
) -> Optional[str]:
    """
    (Fallback Strategy) Parses a page using flexible row searching for embedded episode lists.
    """
    logger.info("[WIKI] Trying Fallback Strategy: Flexible Row Search")
    tables = soup.find_all("table", class_="wikitable")

    for table in tables:
        if not isinstance(table, Tag):
            continue
        for row in table.find_all("tr")[1:]:  # Skip header
            if not isinstance(row, Tag):
                continue
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue

            try:
                cell_texts = [c.get_text(strip=True) for c in cells]
                row_text_for_match = " ".join(cell_texts[:2])

                # Heuristic 1: Look for season and episode number in the first two cells
                match_found = bool(
                    re.search(rf"\b{season}\b.*\b{episode}\b", row_text_for_match)
                )
                # Heuristic 2: For season 1, the first cell might just be the episode number
                if not match_found and season == 1:
                    match_found = re.fullmatch(str(episode), cell_texts[0]) is not None

                if match_found:
                    title_cell = cells[1]
                    if not isinstance(title_cell, Tag):
                        continue

                    found_text = title_cell.find(string=re.compile(r'"([^"]+)"'))
                    if found_text:
                        cleaned_title = str(found_text).strip().strip('"')
                    else:
                        cleaned_title = title_cell.get_text(strip=True)

                    logger.info(
                        f"[SUCCESS] Found title via Fallback Strategy: '{cleaned_title}'"
                    )
                    return cleaned_title
            except (ValueError, IndexError):
                continue

    logger.warning("[WIKI] Fallback Strategy failed.")
    return None


# --- Torrent Site Scraping ---


async def scrape_1337x(
    query: str,
    media_type: str,
    search_url_template: str,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    base_query_for_filter: Optional[str] = None,
    **kwargs,
) -> List[Dict[str, Any]]:
    """
    Scrapes 1337x.to for torrents using a more robust two-stage filtering process.

    1.  It scrapes all results and parses their "base media name".
    2.  It identifies the most common base name to determine the correct media.
    3.  It then gathers and scores all torrents belonging to that media.
    """
    search_config = context.bot_data.get("SEARCH_CONFIG", {})
    # FIX: Ensure consistent key lookup for preferences
    prefs_key = "movies" if "movie" in media_type else "tv"
    preferences = search_config.get("preferences", {}).get(prefs_key, {})

    if not preferences:
        logger.warning(
            f"[SCRAPER] No preferences found for '{prefs_key}'. Cannot score 1337x results."
        )
        return []

    formatted_query = urllib.parse.quote_plus(query)
    search_url = search_url_template.replace("{query}", formatted_query)
    logger.info(f"[SCRAPER] 1337x Stage 1: Scraping candidates from {search_url}")

    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
    }

    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=30, follow_redirects=True
        ) as client:
            response = await client.get(search_url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")

        # --- Stage 1: Scrape candidates and their parsed base names ---
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
            # Parse the torrent name to get a clean base name
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

        filter_query = base_query_for_filter or query
        candidates = [
            c
            for c in candidates
            # Use the new, safe 'filter_query' variable here
            if fuzz.ratio(filter_query.lower(), c["base_name"].lower()) > 90
        ]

        # Add a check in case the stricter filter removed all candidates.
        if not candidates:
            logger.warning(
                f"[SCRAPER] 1337x: No candidates survived the fuzzy filter for query '{query}'."
            )
            return []

        # --- Stage 2: Find the most common base name to identify the correct media ---
        base_name_counts = Counter(c["base_name"] for c in candidates)
        if not base_name_counts:
            return []

        # The most common name is our best match for the media itself
        best_match_base_name, _ = base_name_counts.most_common(1)[0]
        logger.info(
            f"[SCRAPER] 1337x Stage 2: Identified most common media name: '{best_match_base_name}'"
        )

        # --- Stage 3: Filter and process torrents belonging to the identified media ---
        results = []
        base_url = "https://1337x.to"
        for candidate in candidates:
            # Process only torrents that match our identified media name
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

                size_str = size_cell.get_text(strip=True)
                seeds_str = seeds_cell.get_text(strip=True)
                parsed_size_gb = _parse_size_to_gb(size_str)

                uploader = "Anonymous"
                if uploader_tag := uploader_cell.find("a"):
                    uploader = uploader_tag.get_text(strip=True)

                seeders_int = (
                    int(seeds_str) if seeds_str.isdigit() else 0
                )  # <--- ADD THIS
                score = score_torrent_result(
                    candidate["title"], uploader, preferences, seeders=seeders_int
                )  # <--- CHANGE THIS
                if score > 0:
                    results.append(
                        {
                            "title": candidate["title"],
                            "page_url": f"{base_url}{page_url_relative}",
                            "score": score,
                            "source": "1337x",
                            "uploader": uploader,
                            "size_gb": parsed_size_gb,
                            "codec": _parse_codec(candidate["title"]),
                            "seeders": seeders_int,
                            "year": candidate["parsed_info"].get(
                                "year"
                            ),  # <-- NEWLY ADDED
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
) -> List[Dict[str, Any]]:
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
                            _parse_codec(full_title) or "x264"
                        )  # Default YTS to x264
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


async def find_magnet_link_on_page(url: str) -> List[str]:
    """
    Fetches a web page and finds all unique magnet links.
    """
    unique_magnet_links: Set[str] = set()
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
