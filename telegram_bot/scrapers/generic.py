from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml
from bs4 import BeautifulSoup

from ..config import logger


@dataclass
class TorrentData:
    """Represents a single scraped torrent entry."""

    title: str
    magnet_link: str | None
    seeders: int
    leechers: int
    size_bytes: int
    uploader: str | None


class ConfigurationError(Exception):
    """Raised when a scraper configuration is missing required fields."""


def load_site_config(path: Path) -> dict[str, Any]:
    """Load and validate a YAML configuration for a torrent site."""
    if not path.exists():
        raise FileNotFoundError(f"Scraper config not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    required_keys = {"site_name", "base_url", "search_path", "selectors"}
    missing = required_keys - config.keys()
    if missing:
        raise ConfigurationError(f"Missing required config keys: {missing}")

    return config


class GenericTorrentScraper:
    """Scrapes torrent data using a configuration-driven approach."""

    def __init__(self, site_config: dict[str, Any]) -> None:
        self.config = site_config
        self.base_url: str = site_config["base_url"]
        self.search_path: str = site_config["search_path"]
        self.selectors: dict[str, str | None] = site_config.get("selectors", {})
        self.detail_selectors: dict[str, str | None] = site_config.get(
            "details_page_selectors", {}
        )
        self.headers = {
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
            )
        }

    async def search(self, query: str) -> list[TorrentData]:
        """Search the configured site and return parsed torrent entries."""
        formatted = urllib.parse.quote_plus(query)
        search_url = urllib.parse.urljoin(
            self.base_url, self.search_path.format(query=formatted)
        )
        logger.info(
            f"[SCRAPER] Generic scraper for '{self.config.get('site_name')}' hitting {search_url}"
        )
        results: list[TorrentData] = []

        async with httpx.AsyncClient(
            headers=self.headers, timeout=30, follow_redirects=True
        ) as client:
            response = await client.get(search_url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")

            for element in soup.select(self.selectors.get("results_container") or ""):
                torrent = await self._parse_result(element, client)
                if torrent:
                    results.append(torrent)

        return results

    async def _parse_result(
        self, element: Any, client: httpx.AsyncClient
    ) -> Optional[TorrentData]:
        """Parse a single result row into ``TorrentData``."""
        title_sel = self.selectors.get("title")
        title_el = element.select_one(title_sel) if title_sel else None
        title = title_el.get_text(strip=True) if title_el else None
        if not title:
            return None

        seeders = self._extract_int(element, self.selectors.get("seeders"))
        leechers = self._extract_int(element, self.selectors.get("leechers"))
        size_str = self._extract_text(element, self.selectors.get("size"))
        size_bytes = self._parse_size_to_bytes(size_str)
        uploader = self._extract_text(element, self.selectors.get("uploader"))

        magnet_link: str | None = None
        magnet_sel = self.selectors.get("magnet_link")
        if magnet_sel:
            magnet_el = element.select_one(magnet_sel)
            magnet_link = magnet_el.get("href") if magnet_el else None
        else:
            details_sel = self.selectors.get("details_page_link")
            details_el = element.select_one(details_sel) if details_sel else None
            href = details_el.get("href") if details_el else None
            if href:
                detail_url = urllib.parse.urljoin(self.base_url, href)
                detail_resp = await client.get(detail_url)
                if detail_resp.status_code == 200:
                    detail_soup = BeautifulSoup(detail_resp.text, "lxml")
                    magnet_detail_sel = self.detail_selectors.get("magnet_link")
                    if magnet_detail_sel:
                        magnet_detail_el = detail_soup.select_one(magnet_detail_sel)
                        href_val = (
                            magnet_detail_el.get("href") if magnet_detail_el else None
                        )
                        magnet_link = href_val if isinstance(href_val, str) else None
                else:
                    logger.warning(
                        "[SCRAPER] Detail page request failed for %s with status %s",
                        detail_url,
                        detail_resp.status_code,
                    )

        return TorrentData(
            title=title,
            magnet_link=magnet_link,
            seeders=seeders,
            leechers=leechers,
            size_bytes=size_bytes,
            uploader=uploader,
        )

    @staticmethod
    def _extract_int(element: Any, selector: str | None) -> int:
        text = GenericTorrentScraper._extract_text(element, selector)
        return int(text) if text and text.isdigit() else 0

    @staticmethod
    def _extract_text(element: Any, selector: str | None) -> str | None:
        if not selector:
            return None
        found = element.select_one(selector)
        return found.get_text(strip=True) if found else None

    @staticmethod
    def _parse_size_to_bytes(size: str | None) -> int:
        if not size:
            return 0
        match = re.match(r"([\d.]+)\s*(KB|MB|GB|TB)", size, re.IGNORECASE)
        if not match:
            return 0
        value = float(match.group(1))
        unit = match.group(2).upper()
        factor = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
        return int(value * factor.get(unit, 0))
