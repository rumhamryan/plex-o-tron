# telegram_bot/services/scrapers/generic_scraper.py

import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from ...config import logger
from .adapters import fetch_page
from .generic_strategies import (
    _score_candidate_links,
    _strategy_contextual_search,
    _strategy_find_direct_links,
    _strategy_find_in_tables,
)
from .utils import _get_page_html


async def find_magnet_link_on_page(url: str) -> list[str]:
    """
    Fetches a web page and finds all unique magnet links.
    """
    unique_magnet_links: set[str] = set()
    logger.info(f"[WEBSCRAPE] Fetching URL: {url}")

    try:
        response = await fetch_page(url, timeout=30, follow_redirects=True)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "lxml")
        magnet_link_tags = soup.find_all("a", href=re.compile(r"^magnet:"))

        for tag in magnet_link_tags:
            if isinstance(tag, Tag):
                if href := tag.get("href"):
                    if isinstance(href, str):
                        unique_magnet_links.add(href)

        if unique_magnet_links:
            logger.info(f"Found {len(unique_magnet_links)} unique magnet link(s) on page.")
        else:
            logger.warning(f"No magnet links found on page: {url}")

    except Exception as e:
        logger.error(f"[WEBSCRAPE ERROR] HTTP Request failed for {url}: {e}")
    except Exception as e:
        logger.error(f"[WEBSCRAPE ERROR] Unexpected error scraping {url}: {e}", exc_info=True)

    return list(unique_magnet_links)


async def scrape_generic_page(query: str, media_type: str, search_url: str) -> list[dict[str, Any]]:
    """High-level orchestrator that runs all strategies and selects the best link."""

    if not query.strip() or not search_url.strip():
        return []

    logger.info(f"[SCRAPER] Generic: Fetching search results from {search_url}")

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
        # Attempt to extract metadata (title, seeders, leechers) from the best link's context
        title, seeders, leechers = _extract_metadata(soup, best_link, query)

        return [
            {
                "title": title,
                "page_url": best_link,
                "info_url": search_url,
                "score": 100,  # Generic scraper implies we found something relevant manually
                "source": "generic",
                "uploader": None,
                "size_gib": 0.0,
                "codec": None,
                "seeders": seeders,
                "leechers": leechers,
                "year": None,
            }
        ]

    return []


def _extract_metadata(soup: BeautifulSoup, link: str, query: str) -> tuple[str, int, int]:
    """
    Best-effort extraction of title and swarm stats from the link's context.
    """
    seeders = 0
    leechers = 0
    title = query  # Default to query if no text found

    anchor = soup.find("a", href=link)
    if isinstance(anchor, Tag):
        text = anchor.get_text(strip=True)
        if text:
            title = text

        # Try to find stats in parent row if it's a table
        row = anchor.find_parent("tr")
        if isinstance(row, Tag):
            # Simple heuristic: look for numbers in other cells
            # This is very fragile but fits "generic" requirements
            cells = row.find_all("td")
            numbers = []
            for cell in cells:
                # Skip the cell containing the title/link
                if anchor in cell.descendants:
                    continue

                # Check for numeric content
                cell_text = cell.get_text(strip=True)
                # Remove common separators
                clean_text = re.sub(r"[,.]", "", cell_text)
                if clean_text.isdigit():
                    numbers.append(int(clean_text))

            # Heuristic: if we found 2+ numbers, assume the first two are Seeders/Leechers
            # or Size/Seeders/Leechers. This is a wild guess.
            # A safer guess for generic scraping: if headers exist, use them?
            # For now, if we found numbers, take the ones that look like swarm counts (usually integers)
            # Many sites: Size, Files, Added, Seed, Leech.
            # If we match "Seed" or "Leech" in header, that's better.

            # Try to map by headers
            table = row.find_parent("table")
            if isinstance(table, Tag):
                headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
                if headers and len(headers) == len(cells):
                    for h, cell in zip(headers, cells):
                        val_text = re.sub(r"[^\d]", "", cell.get_text(strip=True))
                        if not val_text:
                            continue
                        val = int(val_text)

                        if "seed" in h:
                            seeders = val
                        elif "leech" in h or "peer" in h:
                            leechers = val

            # If header mapping failed but we have numbers, default to 0 to avoid false positives
            # unless we are sure.

    return title, seeders, leechers
