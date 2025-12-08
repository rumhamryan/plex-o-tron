# telegram_bot/services/scrapers/generic_web_scraper.py
from pathlib import Path
import re
from typing import Any

import httpx
import yaml
from bs4 import BeautifulSoup, Tag
from telegram.ext import ContextTypes
from thefuzz import fuzz

from ...config import logger
from ...utils import parse_torrent_name
from .scoring import parse_codec, score_torrent_result
from ...services.generic_torrent_scraper import GenericTorrentScraper, load_site_config


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
                "codec": parse_codec(item.name),
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
