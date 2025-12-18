import re
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag
from thefuzz import fuzz

from ...config import logger
from .utils import _get_page_html


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
                "score": 100,  # Generic scraper implies we found something relevant manually
                "source": "generic",
                "uploader": None,
                "size_gb": 0.0,
                "codec": None,
                "seeders": seeders,
                "leechers": leechers,
                "year": None,
            }
        ]
    return []


def _extract_metadata(
    soup: BeautifulSoup, link: str, query: str
) -> tuple[str, int, int]:
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
                headers = [
                    th.get_text(strip=True).lower() for th in table.find_all("th")
                ]
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
