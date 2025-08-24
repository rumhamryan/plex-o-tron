"""Generic, configuration-driven scraper for torrent index sites."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
import urllib.parse

import httpx
import yaml
from bs4 import BeautifulSoup, Tag
from telegram.ext import ContextTypes

from ..config import logger
from ..services.search_logic import (
    _parse_codec,
    _parse_size_to_gb,
    score_torrent_result,
)
from ..utils import parse_torrent_name


@dataclass
class SiteConfig:
    """Configuration describing how to scrape a torrent index site."""

    site_name: str
    base_url: str
    search_path: str
    selectors: dict[str, str | None]
    details_page_selectors: Optional[dict[str, str]] = None


def load_site_config(path: Path) -> SiteConfig:
    """Load and validate a YAML configuration for a scraper."""
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return SiteConfig(
        site_name=raw["site_name"],
        base_url=raw["base_url"],
        search_path=raw["search_path"],
        selectors=raw.get("selectors", {}),
        details_page_selectors=raw.get("details_page_selectors"),
    )


class GenericTorrentScraper:
    """Scrape torrent data based on a site configuration."""

    def __init__(self, config: SiteConfig):
        self.config = config

    async def search(
        self,
        query: str,
        media_type: str,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> list[dict[str, Any]]:
        """Search the configured site for torrents matching ``query``."""
        prefs_key = "movies" if "movie" in media_type else "tv"
        preferences = (
            context.bot_data.get("SEARCH_CONFIG", {})
            .get("preferences", {})
            .get(prefs_key, {})
        )
        if not preferences:
            logger.warning(
                f"[SCRAPER] No preferences found for '{prefs_key}'. Cannot score {self.config.site_name} results."
            )
            return []

        formatted_query = urllib.parse.quote_plus(query)
        search_url = urllib.parse.urljoin(
            self.config.base_url,
            self.config.search_path.replace("{query}", formatted_query),
        )

        headers = {
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
            )
        }

        results: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            headers=headers, timeout=30, follow_redirects=True
        ) as client:
            response = await client.get(search_url)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    f"[SCRAPER] {self.config.site_name}: {exc.response.status_code} from {search_url}"
                )
                return []
            soup = BeautifulSoup(response.text, "lxml")
            container_selector = self.config.selectors.get("results_container")
            if not container_selector:
                return []
            rows = soup.select(container_selector)
            for row in rows:
                if not isinstance(row, Tag):
                    continue
                title_selector = self.config.selectors.get("title")
                title_tag = row.select_one(title_selector) if title_selector else None
                title = (
                    title_tag.get_text(strip=True)
                    if isinstance(title_tag, Tag)
                    else None
                )
                if not title:
                    continue
                parsed_info = parse_torrent_name(title)
                details_link_selector = self.config.selectors.get("details_page_link")
                details_link_tag = (
                    row.select_one(details_link_selector)
                    if details_link_selector
                    else None
                )
                magnet_link: Optional[str] = None
                magnet_selector = self.config.selectors.get("magnet_link")
                if magnet_selector:
                    mag_tag = row.select_one(magnet_selector)
                    if isinstance(mag_tag, Tag):
                        href_value = mag_tag.get("href")
                        if isinstance(href_value, str):
                            magnet_link = href_value
                elif details_link_tag and (href := details_link_tag.get("href")):
                    if isinstance(href, str):
                        detail_url = urllib.parse.urljoin(self.config.base_url, href)
                        detail_resp = await client.get(detail_url)
                        if detail_resp.status_code != 200:
                            continue
                        detail_soup = BeautifulSoup(detail_resp.text, "lxml")
                        magnet_sel = (self.config.details_page_selectors or {}).get(
                            "magnet_link"
                        )
                        mag_tag = (
                            detail_soup.select_one(magnet_sel) if magnet_sel else None
                        )
                        if isinstance(mag_tag, Tag):
                            href_value = mag_tag.get("href")
                            if isinstance(href_value, str):
                                magnet_link = href_value
                if not magnet_link:
                    continue
                seeders_selector = self.config.selectors.get("seeders")
                seeders_tag = (
                    row.select_one(seeders_selector) if seeders_selector else None
                )
                seeders_text = (
                    seeders_tag.get_text(strip=True)
                    if isinstance(seeders_tag, Tag)
                    else "0"
                )
                seeders = int(seeders_text) if seeders_text.isdigit() else 0
                size_selector = self.config.selectors.get("size")
                size_tag = row.select_one(size_selector) if size_selector else None
                size_text = (
                    size_tag.get_text(strip=True) if isinstance(size_tag, Tag) else "0"
                )
                uploader_selector = self.config.selectors.get("uploader")
                uploader_tag = (
                    row.select_one(uploader_selector) if uploader_selector else None
                )
                uploader = (
                    uploader_tag.get_text(strip=True)
                    if isinstance(uploader_tag, Tag)
                    else "Anonymous"
                )
                size_gb = _parse_size_to_gb(size_text)
                score = score_torrent_result(
                    title, uploader, preferences, seeders=seeders
                )
                result = {
                    "title": title,
                    "page_url": magnet_link,
                    "score": score,
                    "source": self.config.site_name,
                    "uploader": uploader,
                    "size_gb": size_gb,
                    "codec": _parse_codec(title),
                    "seeders": seeders,
                    "year": parsed_info.get("year"),
                }
                results.append(result)
        return results
