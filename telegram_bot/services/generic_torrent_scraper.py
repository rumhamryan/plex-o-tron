from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml
from bs4 import BeautifulSoup, Tag

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
    required_keys = {"site_name", "base_url", "search_path", "selectors"}
    missing = required_keys - data.keys()
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
    a detail page for magnet links.
    """

    def __init__(self, site_config: dict[str, Any]) -> None:
        self.config = site_config
        self.base_url: str = site_config["base_url"].rstrip("/")
        self.search_path: str = site_config["search_path"]
        self.selectors: dict[str, Any] = site_config["selectors"]
        self.details_selectors: dict[str, Any] | None = site_config.get(
            "details_page_selectors"
        )
        self.site_name: str = site_config["site_name"]

    async def search(self, query: str) -> list[TorrentData]:
        """Search the site for ``query`` and return scraped torrent data."""
        if not isinstance(query, str) or not query.strip():
            logger.warning("[SCRAPER] Empty query provided to GenericTorrentScraper")
            return []

        formatted_query = urllib.parse.quote_plus(query)
        search_url = urllib.parse.urljoin(
            self.base_url, self.search_path.format(query=formatted_query)
        )

        results: list[TorrentData] = []
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(search_url)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "lxml")

                container_selector = self.selectors.get("results_container")
                if not isinstance(container_selector, str):
                    logger.error(
                        "[SCRAPER] 'results_container' selector missing or invalid"
                    )
                    return []

                for element in soup.select(container_selector):
                    if not isinstance(element, Tag):
                        continue
                    parsed = await self._parse_result(element, client)
                    if parsed:
                        results.append(parsed)
        except Exception as exc:
            logger.error(f"[SCRAPER] Generic scraper failed: {exc}")
            return []

        return results

    async def _parse_result(
        self, element: Tag, client: httpx.AsyncClient
    ) -> Optional[TorrentData]:
        """Parse a single search result row into ``TorrentData``."""
        selectors = self.selectors

        title_selector = selectors.get("title")
        if not isinstance(title_selector, str):
            return None
        title_tag = element.select_one(title_selector)
        if not isinstance(title_tag, Tag):
            return None
        title = title_tag.get_text(strip=True)
        if not title:
            return None

        magnet_link: str | None = None
        magnet_selector = selectors.get("magnet_link")
        if isinstance(magnet_selector, str):
            magnet_tag = element.select_one(magnet_selector)
            if isinstance(magnet_tag, Tag):
                href_val = magnet_tag.get("href")
                if isinstance(href_val, str):
                    magnet_link = href_val

        if not magnet_link:
            details_selector = selectors.get("details_page_link")
            details_tag = (
                element.select_one(details_selector)
                if isinstance(details_selector, str)
                else None
            )
            detail_href = (
                details_tag.get("href") if isinstance(details_tag, Tag) else None
            )
            if isinstance(detail_href, str) and self.details_selectors:
                detail_url = urllib.parse.urljoin(self.base_url, detail_href)
                try:
                    detail_resp = await client.get(detail_url)
                    detail_resp.raise_for_status()
                    detail_soup = BeautifulSoup(detail_resp.text, "lxml")
                    magnet_detail_sel = self.details_selectors.get("magnet_link")
                    if isinstance(magnet_detail_sel, str):
                        magnet_tag = detail_soup.select_one(magnet_detail_sel)
                        if isinstance(magnet_tag, Tag):
                            href_val = magnet_tag.get("href")
                            if isinstance(href_val, str):
                                magnet_link = href_val
                except httpx.HTTPError as exc:
                    logger.warning(
                        f"[SCRAPER] Failed fetching detail page {detail_url}: {exc}"
                    )
                    return None

        if not magnet_link:
            return None

        def _safe_int(sel_key: str) -> int:
            sel = selectors.get(sel_key)
            tag = element.select_one(sel) if isinstance(sel, str) else None
            text = tag.get_text(strip=True) if isinstance(tag, Tag) else ""
            return int(text) if text.isdigit() else 0

        seeders = _safe_int("seeders")
        leechers = _safe_int("leechers")

        size_selector = selectors.get("size")
        size_tag = (
            element.select_one(size_selector)
            if isinstance(size_selector, str)
            else None
        )
        size_text = size_tag.get_text(strip=True) if isinstance(size_tag, Tag) else ""
        size_bytes = _parse_size_to_bytes(size_text)

        uploader_selector = selectors.get("uploader")
        uploader_tag = (
            element.select_one(uploader_selector)
            if isinstance(uploader_selector, str)
            else None
        )
        uploader = (
            uploader_tag.get_text(strip=True) if isinstance(uploader_tag, Tag) else None
        )

        return TorrentData(
            name=title,
            magnet_url=magnet_link,
            seeders=seeders,
            leechers=leechers,
            size_bytes=size_bytes,
            uploader=uploader,
            source_site=self.site_name,
        )
