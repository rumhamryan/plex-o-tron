from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml
from bs4 import BeautifulSoup, Tag
from thefuzz import fuzz

from ..config import logger


@dataclass
class TorrentData:
    """Container for data extracted from a torrent index."""

    name: str
    magnet_url: str
    seeders: int
    leechers: int
    size_bytes: int
    source_site: str
    uploader: str | None = None


def load_site_config(config_path: Path) -> dict[str, Any]:
    """Load and minimally validate a YAML site configuration."""
    if not config_path.exists():
        raise FileNotFoundError(f"Scraper config not found: {config_path}")
    with config_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    required = {
        "site_name",
        "base_url",
        "search_path",
        "category_mapping",
        "results_page_selectors",
    }
    missing = required - data.keys()
    if missing:
        raise ValueError(f"Config missing keys: {', '.join(sorted(missing))}")
    return data


def _parse_size_to_bytes(size_str: str) -> int:
    """Convert strings like ``'1.5 GB'`` or ``'500 MB'`` to bytes."""
    size_str = size_str.lower().replace(",", "")
    match = re.search(r"([\d.]+)", size_str)
    if not match:
        return 0
    value = float(match.group(1))
    if "gb" in size_str:
        return int(value * 1024**3)
    if "mb" in size_str:
        return int(value * 1024**2)
    if "kb" in size_str:
        return int(value * 1024)
    return int(value)


class GenericTorrentScraper:
    """Scrape torrent sites based on a configuration file.

    The scraper uses CSS selectors defined in a YAML file to extract
    torrent information from a site's search results and, if necessary,
    a detail page for magnet links. Configuration allows per-site
    customisation without changing the scraper code.
    """

    def __init__(self, site_config: dict[str, Any]) -> None:
        self.config = site_config
        self.base_url: str = site_config["base_url"].rstrip("/")
        self.search_path: str = site_config["search_path"]
        self.category_mapping: dict[str, str] = site_config["category_mapping"]
        self.results_selectors: dict[str, Any] = site_config["results_page_selectors"]
        self.details_selectors: dict[str, Any] = site_config.get(
            "details_page_selectors", {}
        )
        self.advanced_features: dict[str, Any] = site_config.get(
            "advanced_features", {}
        )
        self.site_name: str = site_config["site_name"]

    async def search(
        self,
        query: str,
        media_type: str,
        base_query_for_filter: str | None = None,
    ) -> list[TorrentData]:
        """Search the site for ``query`` and return scraped torrent data."""
        if not isinstance(query, str) or not query.strip():
            logger.warning("[SCRAPER] Empty query provided to GenericTorrentScraper")
            return []

        category_path = self.category_mapping.get(media_type)
        if not category_path:
            logger.error(
                f"[SCRAPER] Media type '{media_type}' not mapped for {self.site_name}"
            )
            return []

        formatted_query = urllib.parse.quote_plus(query)
        search_path = self.search_path.format(
            query=formatted_query, category=category_path, page=1
        )
        search_url = urllib.parse.urljoin(self.base_url, search_path)

        search_html = await self._fetch_page(search_url)
        if not search_html:
            return []

        soup = BeautifulSoup(search_html, "lxml")
        row_selector = self.results_selectors.get("rows")
        if not isinstance(row_selector, str):
            logger.error("[SCRAPER] 'rows' selector missing in config")
            return []

        results: list[TorrentData] = []
        for row in soup.select(row_selector):
            if not isinstance(row, Tag):
                continue
            try:
                parsed = await self._parse_row(row)
                if parsed:
                    results.append(parsed)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[SCRAPER] Failed to parse row: {exc}")
                continue

        if self.advanced_features.get("enable_fuzzy_filter") and results:
            base = (base_query_for_filter or query).lower()
            threshold = int(self.advanced_features.get("fuzzy_filter_ratio", 85))
            results = [
                r
                for r in results
                if fuzz.partial_ratio(base, r.name.lower()) >= threshold
            ]

        return results

    async def _parse_row(self, row: Tag) -> Optional[TorrentData]:
        """Parse a single search result row into ``TorrentData``."""
        name = self._extract_text(row, self.results_selectors.get("name"))
        if not name:
            return None

        magnet_link = self._extract_href(row, self.results_selectors.get("magnet_url"))
        if not magnet_link:
            details_href = self._extract_href(
                row, self.results_selectors.get("details_page_link")
            )
            if details_href:
                detail_url = urllib.parse.urljoin(self.base_url, details_href)
                detail_html = await self._fetch_page(detail_url)
                if detail_html:
                    detail_soup = BeautifulSoup(detail_html, "lxml")
                    magnet_link = self._extract_href(
                        detail_soup, self.details_selectors.get("magnet_url")
                    )
        if not magnet_link:
            return None

        seeders = self._extract_int(row, self.results_selectors.get("seeds"))
        leechers = self._extract_int(row, self.results_selectors.get("leechers"))
        size_text = self._extract_text(row, self.results_selectors.get("size"))
        size_bytes = _parse_size_to_bytes(size_text)
        uploader = self._extract_text(row, self.results_selectors.get("uploader"))

        return TorrentData(
            name=name,
            magnet_url=magnet_link,
            seeders=seeders,
            leechers=leechers,
            size_bytes=size_bytes,
            uploader=uploader,
            source_site=self.site_name,
        )

    async def _fetch_page(self, url: str) -> str | None:
        """Fetch ``url`` and return the response text, handling errors."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/115.0 Safari/537.36"
            )
        }
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                return resp.text
        except httpx.HTTPError as exc:  # noqa: BLE001
            logger.warning(f"[SCRAPER] HTTP error fetching {url}: {exc}")
        return None

    def _extract_text(self, root: Tag, selector: Any) -> str:
        tag = root.select_one(selector) if isinstance(selector, str) else None
        return tag.get_text(strip=True) if isinstance(tag, Tag) else ""

    def _extract_href(self, root: Tag, selector: Any) -> str | None:
        tag = root.select_one(selector) if isinstance(selector, str) else None
        href = tag.get("href") if isinstance(tag, Tag) else None
        return href if isinstance(href, str) else None

    def _extract_int(self, root: Tag, selector: Any) -> int:
        text = self._extract_text(root, selector)
        return int(text) if text.isdigit() else 0
