from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
import asyncio
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml
from bs4 import BeautifulSoup, Tag
from collections import Counter
from thefuzz import fuzz

from ..config import logger
from ..utils import parse_torrent_name


# Cache for site configurations to avoid repeated disk reads.
_config_cache: dict[Path, dict[str, Any]] = {}


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
    """Load and minimally validate a YAML site configuration.

    To improve performance, configuration files are cached in-memory after the
    first load. Subsequent calls with the same ``config_path`` return the cached
    data, avoiding repeated disk I/O.
    """

    resolved_path = config_path.resolve()
    cached = _config_cache.get(resolved_path)
    if cached is not None:
        return cached

    if not resolved_path.exists():
        raise FileNotFoundError(f"Scraper config not found: {resolved_path}")

    with resolved_path.open("r", encoding="utf-8") as fh:
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

    _config_cache[resolved_path] = data
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
        self.matching: dict[str, Any] = site_config.get("matching", {})
        scorer_name = self.matching.get("fuzz_scorer", "ratio")
        self._fuzz_scorer = getattr(fuzz, scorer_name, fuzz.ratio)
        self._fuzz_threshold = int(self.matching.get("fuzz_threshold", 75))
        if not hasattr(fuzz, scorer_name):
            logger.warning(
                "[SCRAPER] Unknown fuzz scorer '%s'; defaulting to 'ratio'", scorer_name
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

        results: list[TorrentData] = []
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            logger.info(
                f"[SCRAPER] {self.site_name}: Fetching search results from {search_url}"
            )
            search_html = await self._fetch_page(search_url, client)
            if not search_html:
                logger.error(
                    f"[SCRAPER] {self.site_name}: Failed to retrieve search results from {search_url}"
                )
                return []

            soup = BeautifulSoup(search_html, "lxml")

            # Narrow the parsing scope to a configured results container. This avoids
            # scanning the entire document when only a specific section is relevant.
            results_container_selector = self.results_selectors.get("results_container")
            search_area: BeautifulSoup | Tag = soup
            if isinstance(results_container_selector, str):
                container = soup.select_one(results_container_selector)
                if container is not None:
                    search_area = container
                else:
                    logger.debug(
                        f"[SCRAPER] {self.site_name}: Results container selector "
                        f"'{results_container_selector}' not found; using full page"
                    )

            row_selector = self.results_selectors.get("rows")
            if not isinstance(row_selector, str):
                logger.error("[SCRAPER] 'rows' selector missing in config")
                return []

            rows = search_area.select(row_selector)
            logger.debug(
                f"[SCRAPER] {self.site_name}: Found {len(rows)} rows using selector '{row_selector}'"
            )

            # Parse rows concurrently so detail-page requests do not block each other.
            parse_tasks = [
                self._parse_row(row, client) for row in rows if isinstance(row, Tag)
            ]
            parsed_rows = await asyncio.gather(*parse_tasks, return_exceptions=True)

            for parsed in parsed_rows:
                if isinstance(parsed, TorrentData):
                    results.append(parsed)
                elif isinstance(parsed, Exception):
                    logger.warning(f"[SCRAPER] Failed to parse row: {parsed}")

        if not results:
            logger.info(f"[SCRAPER] {self.site_name}: Parsed 0 torrents for '{query}'")
            return []

        # --- Two-stage filtering to improve precision ---
        filter_query = (base_query_for_filter or query).lower()

        # Stage 1: lenient fuzzy match to gather viable candidates
        candidates: list[tuple[TorrentData, str]] = []
        for res in results:
            parsed_info = parse_torrent_name(res.name)
            base_name = parsed_info.get("title", "").lower()
            candidates.append((res, base_name))

        strong_candidates = [
            (r, base)
            for r, base in candidates
            if base and self._fuzz_scorer(filter_query, base) >= self._fuzz_threshold
        ]
        if not strong_candidates:
            logger.info(
                f"[SCRAPER] {self.site_name}: No strong candidates for '{query}'"
            )
            return []

        # Stage 2: keep only results matching the most common base name
        base_name_counts = Counter(base for _, base in strong_candidates)
        if not base_name_counts:
            logger.info(
                f"[SCRAPER] {self.site_name}: Unable to determine consensus for '{query}'"
            )
            return []

        best_name, _ = base_name_counts.most_common(1)[0]
        final_results = [r for r, base in strong_candidates if base == best_name]

        logger.info(
            f"[SCRAPER] {self.site_name}: Parsed {len(final_results)} torrents for '{query}'"
        )
        return final_results

    async def _parse_row(
        self, row: Tag, client: httpx.AsyncClient
    ) -> Optional[TorrentData]:
        """Parse a single search result row into ``TorrentData``.

        A shared ``httpx.AsyncClient`` instance is passed in so that any
        additional requests (e.g. fetching a detail page for the magnet link)
        reuse existing connections. This significantly reduces overhead when
        parsing many rows.
        """

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
                detail_html = await self._fetch_page(detail_url, client)
                if detail_html:
                    detail_soup = BeautifulSoup(detail_html, "lxml")
                    magnet_link = self._extract_href(
                        detail_soup, self.details_selectors.get("magnet_url")
                    )
        if not magnet_link:
            logger.warning(
                f"[SCRAPER] {self.site_name}: No magnet link found for '{name}'"
            )
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

    async def _fetch_page(
        self, url: str, client: httpx.AsyncClient | None = None
    ) -> str | None:
        """Fetch ``url`` and return the response text, handling errors.

        A reusable :class:`httpx.AsyncClient` can be supplied to avoid the cost of
        creating a new connection for every request. When ``client`` is ``None``
        a short-lived client is created for the call. This function is small and
        self-contained so it's easy to mock in tests.
        """

        if client is None:
            async with httpx.AsyncClient(
                timeout=30, follow_redirects=True
            ) as temp_client:
                return await self._fetch_page(url, temp_client)

        headers = {
            # Some torrent sites (e.g. 1337x) return HTTP 403 unless common
            # browser headers are supplied. These values mimic a typical
            # desktop browser and keep scraping behaviour consistent.
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/115.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                "image/webp,*/*;q=0.8"
            ),
            "Referer": f"{self.base_url}/",
        }

        logger.debug(f"[SCRAPER] {self.site_name}: GET {url}")

        try:
            response = await client.get(url, headers=headers)
            logger.debug(
                f"[SCRAPER] {self.site_name}: GET {url} -> {response.status_code}"
            )
            response.raise_for_status()
            logger.debug(
                f"[SCRAPER] {self.site_name}: Response snippet: {response.text[:200]!r}"
            )
            return response.text
        except httpx.HTTPStatusError as exc:  # noqa: BLE001
            logger.error(f"[SCRAPER] HTTP error fetching {url}: {exc}")
            if exc.response is not None:
                logger.debug(
                    f"[SCRAPER] {self.site_name}: Error response body: {exc.response.text[:200]!r}"
                )
        except httpx.HTTPError as exc:  # noqa: BLE001
            logger.error(f"[SCRAPER] Request error fetching {url}: {exc}")
        return None

    def _extract_text(self, root: Tag, selector: Any) -> str:
        tag = root.select_one(selector) if isinstance(selector, str) else None
        return tag.get_text(strip=True) if isinstance(tag, Tag) else ""

    def _extract_href(self, root: Tag, selector: Any) -> str | None:
        tag = root.select_one(selector) if isinstance(selector, str) else None
        if not isinstance(tag, Tag):
            return None
        # Some sites expose the target URL via ``data-href`` instead of ``href``.
        href = tag.get("href") or tag.get("data-href")
        return href if isinstance(href, str) else None

    def _extract_int(self, root: Tag, selector: Any) -> int:
        text = self._extract_text(root, selector)
        return int(text) if text.isdigit() else 0
