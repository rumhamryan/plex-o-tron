from __future__ import annotations

import re
import urllib.parse
import asyncio
from dataclasses import dataclass
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
    magnet_url: str | None = None
    seeders: int = 0
    leechers: int = 0
    size_bytes: int = 0
    source_site: str = ""
    uploader: str | None = None
    # The detail page is optional and used only if the magnet link is absent
    details_link: str | None = None


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
        limit: int = 15,
    ) -> list[TorrentData]:
        """Search the site for ``query`` and return scraped torrent data.

        The ``limit`` parameter controls how many of the highest seeder-count
        torrents are fully parsed. Limiting this reduces time spent on low
        quality results, which in turn lowers overall scraping latency.
        """
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

        logger.info(
            f"[SCRAPER] {self.site_name}: Fetching search results from {search_url}"
        )
        search_html = await self._fetch_page(search_url)
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

        # Efficiently parse only the most promising rows. Seeders are used as a
        # quick proxy for quality, so we avoid expending effort on results that
        # are unlikely to be worth downloading.
        results = self._parse_and_select_top_results(search_area, limit)
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

        # Fetch magnet links for remaining results concurrently. Only results
        # that pass filtering trigger additional network requests, reducing
        # overall scraping time.
        await self._resolve_magnets(final_results)
        final_results = [r for r in final_results if r.magnet_url]

        logger.info(
            f"[SCRAPER] {self.site_name}: Parsed {len(final_results)} torrents for '{query}'"
        )
        return final_results

    def _extract_data_from_row(self, row: Tag) -> Optional[TorrentData]:
        """Extract all relevant fields from a single result row.

        This method performs the expensive parsing of a row and is therefore
        called only on a small subset of high-quality results. Keeping this
        logic isolated makes the selection strategy in
        ``_parse_and_select_top_results`` easier to understand and modify.
        """

        name = self._extract_text(row, self.results_selectors.get("name"))
        if not name:
            return None

        link = self._extract_href(row, self.results_selectors.get("magnet"))
        magnet_url = (
            link if isinstance(link, str) and link.startswith("magnet:") else None
        )
        details_href = None if magnet_url else link

        seeders = self._extract_int(row, self.results_selectors.get("seeders"))
        leechers = self._extract_int(row, self.results_selectors.get("leechers"))
        size_text = self._extract_text(row, self.results_selectors.get("size"))
        size_bytes = _parse_size_to_bytes(size_text)
        uploader = self._extract_text(row, self.results_selectors.get("uploader"))

        return TorrentData(
            name=name,
            magnet_url=magnet_url,
            seeders=seeders,
            leechers=leechers,
            size_bytes=size_bytes,
            uploader=uploader,
            source_site=self.site_name,
            details_link=details_href,
        )

    def _parse_and_select_top_results(
        self, search_area: BeautifulSoup | Tag, limit: int
    ) -> list[TorrentData]:
        """Parse rows and return only the top ``limit`` results by seeders."""

        row_selector = self.results_selectors.get("result_row")
        if not isinstance(row_selector, str):
            logger.error("[SCRAPER] 'result_row' selector missing in config")
            return []

        rows = [r for r in search_area.select(row_selector) if isinstance(r, Tag)]
        logger.debug(
            f"[SCRAPER] {self.site_name}: Found {len(rows)} rows using selector '{row_selector}'"
        )

        # Extract seeders for each row in a lightweight pass.
        seeders_selector = self.results_selectors.get("seeders")
        scored_rows: list[tuple[int, Tag]] = []
        for row in rows:
            seeders = self._extract_int(row, seeders_selector)
            scored_rows.append((seeders, row))

        # Sort rows by seeder count (descending) and parse only the top subset.
        top_rows = sorted(scored_rows, key=lambda x: x[0], reverse=True)[:limit]

        selected: list[TorrentData] = []
        for _, row in top_rows:
            try:
                parsed = self._extract_data_from_row(row)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[SCRAPER] Failed to parse row: {exc}")
                continue
            if parsed is not None:
                selected.append(parsed)

        return selected

    async def _resolve_magnets(self, items: list[TorrentData]) -> None:
        """Fetch magnet links for items missing them in parallel."""
        tasks: list[tuple[TorrentData, asyncio.Task[str | None]]] = []
        for item in items:
            if item.magnet_url or not item.details_link:
                continue
            detail_url = urllib.parse.urljoin(self.base_url, item.details_link)
            tasks.append((item, asyncio.create_task(self._fetch_page(detail_url))))

        if not tasks:
            return

        responses = await asyncio.gather(*(task for _, task in tasks))
        for (item, _), html in zip(tasks, responses):
            if not html:
                continue
            detail_soup = BeautifulSoup(html, "lxml")
            magnet_link = self._extract_href(
                detail_soup, self.details_selectors.get("magnet_url")
            )
            if magnet_link:
                item.magnet_url = magnet_link

    async def _fetch_page(self, url: str) -> str | None:
        """Fetch ``url`` and return the response text, handling errors."""

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
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
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
