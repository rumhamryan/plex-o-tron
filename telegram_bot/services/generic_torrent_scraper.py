from __future__ import annotations

import re
import urllib.parse
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml  # type: ignore[import-untyped]
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
        self.base_url_candidates = self._build_base_url_rotation(site_config)
        self.base_url: str = self.base_url_candidates[0]
        self._last_successful_base_url: str | None = self.base_url
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

        search_html: str | None = None
        active_base_url: str | None = None
        for base_url in self._iter_base_url_candidates():
            search_url = urllib.parse.urljoin(f"{base_url}/", search_path.lstrip("/"))
            logger.info(
                f"[SCRAPER] {self.site_name}: Fetching search results from {search_url}"
            )
            page_html = await self._fetch_page(search_url, referer_base_url=base_url)
            if page_html:
                search_html = page_html
                active_base_url = base_url
                break
            logger.warning(
                f"[SCRAPER] {self.site_name}: Failed to retrieve search results from {search_url}; "
                "trying next mirror"
            )

        if not search_html or not active_base_url:
            logger.error(
                f"[SCRAPER] {self.site_name}: Unable to retrieve search results from any configured base URL"
            )
            return []

        self.base_url = active_base_url
        self._last_successful_base_url = active_base_url

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
        target_details = parse_torrent_name(base_query_for_filter or query)
        target_season = (
            target_details.get("season")
            if isinstance(target_details.get("season"), int)
            else None
        )
        target_episode = (
            target_details.get("episode")
            if isinstance(target_details.get("episode"), int)
            else None
        )
        if target_episode is not None and target_season is None:
            target_episode = None

        season_tokens: set[str] = set()
        episode_tokens: set[str] = set()
        if target_season is not None:
            season_tokens.update(
                {
                    f"S{target_season:02d}",
                    f"S{target_season}",
                    f"SEASON {target_season}",
                }
            )
        if target_season is not None and target_episode is not None:
            episode_tokens.update(
                {
                    f"S{target_season:02d}E{target_episode:02d}",
                    f"S{target_season}E{target_episode}",
                    f"{target_season}X{target_episode:02d}",
                    f"{target_season}X{target_episode}",
                }
            )

        def _title_contains_tokens(raw_title: str, tokens: set[str]) -> bool:
            if not tokens:
                return False
            upper = raw_title.upper()
            compact = re.sub(r"[^A-Z0-9]", "", upper)
            return any(token in upper or token in compact for token in tokens)

        def _matches_target_episode(
            parsed_info: dict[str, Any], raw_title: str
        ) -> bool:
            parsed_season = parsed_info.get("season")
            parsed_episode = parsed_info.get("episode")
            parsed_is_pack = bool(parsed_info.get("is_season_pack"))

            if target_season is not None and target_episode is not None:
                if (
                    isinstance(parsed_season, int)
                    and isinstance(parsed_episode, int)
                    and parsed_season == target_season
                    and parsed_episode == target_episode
                ):
                    return True
                if (
                    parsed_is_pack
                    and isinstance(parsed_season, int)
                    and parsed_season == target_season
                ):
                    return True
                return _title_contains_tokens(raw_title, episode_tokens)

            if target_season is not None:
                if isinstance(parsed_season, int) and parsed_season == target_season:
                    return True
                if (
                    parsed_is_pack
                    and isinstance(parsed_season, int)
                    and parsed_season == target_season
                ):
                    return True
                return _title_contains_tokens(raw_title, season_tokens)

            return True

        # Stage 1: lenient fuzzy match to gather viable candidates
        candidates: list[tuple[TorrentData, str]] = []
        for res in results:
            parsed_info = parse_torrent_name(res.name)
            if not _matches_target_episode(parsed_info, res.name):
                continue
            base_name = parsed_info.get("title", "").lower().strip()
            if not base_name:
                base_name = re.sub(r"[\._]", " ", res.name).strip().lower()
            candidates.append((res, base_name))

        if not candidates:
            if target_episode is not None or target_season is not None:
                logger.info(
                    f"[SCRAPER] {self.site_name}: No torrents matched season/episode filters for '{query}'"
                )
            else:
                logger.info(
                    f"[SCRAPER] {self.site_name}: No viable candidates after initial parsing for '{query}'"
                )
            return []

        # Token helpers to allow simple, fast guardrails against known mis-matches
        def _normalize_local(text: str) -> str:
            t = re.sub(r"[^a-z0-9\s]", " ", text.lower())
            t = re.sub(r"\s+", " ", t).strip()
            return t

        LOCAL_STOP = {"the", "a", "an", "of", "and"}

        def _tokens_no_stop_local(text: str) -> set[str]:
            return {
                tok
                for tok in _normalize_local(text).split()
                if tok and tok not in LOCAL_STOP
            }

        q_tokens = _tokens_no_stop_local(base_query_for_filter or query)

        # For TV searches, exclude candidates that contain certain disallowed tokens
        # that are not present in the user's query (e.g., "anime" for spin-off titles).
        disallowed_tokens_tv = {"anime"}
        disallowed_active = (
            isinstance(media_type, str)
            and media_type.lower().startswith("tv")
            and disallowed_tokens_tv.isdisjoint(q_tokens)
        )

        def _allowed_base(base: str) -> bool:
            if not base:
                return False
            if not disallowed_active:
                return True
            base_tokens = _tokens_no_stop_local(base)
            return disallowed_tokens_tv.isdisjoint(base_tokens)

        candidate_metrics: list[tuple[str, str, int, bool]] = []
        strong_candidates: list[tuple[TorrentData, str]] = []
        for res, base in candidates:
            fuzz_score = self._fuzz_scorer(filter_query, base)
            allowed = _allowed_base(base)
            if allowed and fuzz_score >= self._fuzz_threshold:
                strong_candidates.append((res, base))
            candidate_metrics.append((res.name, base, fuzz_score, allowed))
        if not strong_candidates:
            if candidate_metrics:
                self._log_candidate_filter_metrics(
                    filter_query, candidate_metrics, self._fuzz_threshold
                )
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

        # Prefer a consensus base name without disallowed tokens when active
        if disallowed_active:
            filtered_bases = [b for b in base_name_counts if _allowed_base(b)]
            if filtered_bases:
                best_name = max(filtered_bases, key=lambda b: base_name_counts[b])
            else:
                best_name, _ = base_name_counts.most_common(1)[0]
        else:
            best_name, _ = base_name_counts.most_common(1)[0]
        final_results = [r for r, base in strong_candidates if base == best_name]

        # Additional precision filter: avoid overly broad matches for single-token queries.
        # Example: when the query is "Superman", do not match "The Death of Superman".
        try:

            def _normalize(text: str) -> str:
                t = re.sub(r"[^a-z0-9\s]", " ", text.lower())
                t = re.sub(r"\s+", " ", t).strip()
                return t

            STOP_WORDS = {"the", "a", "an", "of", "and"}

            def _tokens_no_stop(text: str) -> set[str]:
                return {
                    tok
                    for tok in _normalize(text).split()
                    if tok and tok not in STOP_WORDS
                }

            q_tokens = _tokens_no_stop(base_query_for_filter or query)
            best_tokens = _tokens_no_stop(best_name)

            # If user provided a single meaningful token and the chosen base name adds extra tokens,
            # try to select a stricter alternative; otherwise, drop results.
            if (
                len(q_tokens) == 1
                and q_tokens.issubset(best_tokens)
                and len(best_tokens - q_tokens) >= 1
            ):
                # Find bases that equal the query tokens or are very close in full-string ratio
                def _tier(base: str) -> int:
                    b_tokens = _tokens_no_stop(base)
                    b_norm = _normalize(base)
                    q_norm = _normalize(base_query_for_filter or query)
                    if b_tokens == q_tokens and b_tokens:
                        return 3
                    if b_norm == q_norm:
                        return 3
                    if q_tokens.issubset(b_tokens) and len(b_tokens - q_tokens) <= 1:
                        return 2
                    if fuzz.ratio(q_norm, b_norm) >= 90:
                        return 2
                    return 1

                candidates_ok = [b for b in base_name_counts if _tier(b) >= 2]
                if candidates_ok:
                    # Choose the most precise, then most frequent
                    new_best = max(
                        candidates_ok, key=lambda b: (_tier(b), base_name_counts[b])
                    )
                    if new_best != best_name:
                        best_name = new_best
                        final_results = [
                            r for r, base in strong_candidates if base == best_name
                        ]
                else:
                    # No precise alternative; drop to avoid a misleading match
                    final_results = []
        except Exception:
            # If anything goes wrong, keep the original selection rather than failing the search.
            pass

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
        """
        Fetch details pages in parallel to populate missing magnets or swarm stats.

        If a site (like EZTV) does not list leechers on the index but does on the
        detail page, we fetch it here to ensure swarm telemetry is complete.
        """
        tasks: list[tuple[TorrentData, asyncio.Task[str | None]]] = []

        # Check if we have a selector to parse leechers from the details page
        details_leecher_selector = self.details_selectors.get("leechers")

        for item in items:
            needs_magnet = not item.magnet_url
            # If we have 0 leechers (likely missing from index) and a selector exists, try fetching
            needs_leechers = item.leechers == 0 and details_leecher_selector

            if (needs_magnet or needs_leechers) and item.details_link:
                detail_url = urllib.parse.urljoin(self.base_url, item.details_link)
                tasks.append((item, asyncio.create_task(self._fetch_page(detail_url))))

        if not tasks:
            return

        responses = await asyncio.gather(*(task for _, task in tasks))
        for (item, _), html in zip(tasks, responses):
            if not html:
                continue
            detail_soup = BeautifulSoup(html, "lxml")

            # 1. Resolve Magnet
            if not item.magnet_url:
                magnet_link = self._extract_href(
                    detail_soup, self.details_selectors.get("magnet_url")
                )
                if magnet_link:
                    item.magnet_url = magnet_link

            # 2. Resolve Leechers (if needed and configured)
            if item.leechers == 0 and details_leecher_selector:
                # Use the existing integer extraction helper
                val = self._extract_int(detail_soup, details_leecher_selector)
                if val > 0:
                    item.leechers = val

    def _log_candidate_filter_metrics(
        self,
        filter_query: str,
        metrics: list[tuple[str, str, int, bool]],
        threshold: int,
        *,
        max_entries: int = 10,
    ) -> None:
        """
        Emits diagnostics showing how each parsed torrent compared to the fuzzy filter.

        This is especially helpful when the scraper returns rows but the precision
        filters drop them all (e.g., EZTV returning new-season episodes when an
        older season was requested).
        """
        if not metrics:
            return
        lines = [
            f"--- {self.site_name} Candidate Filter Diagnostics ---",
            f"Filter query: {filter_query}",
            f"Fuzz threshold: {threshold}",
        ]
        for idx, (raw_title, base_name, fuzz_score, allowed) in enumerate(
            metrics[:max_entries], start=1
        ):
            lines.append(f"Candidate {idx}:")
            lines.append(f"  raw_title: {raw_title}")
            lines.append(f"  normalized_base: {base_name or '<empty>'}")
            lines.append(f"  allowed_after_tokens: {allowed}")
            lines.append(f"  fuzz_score: {fuzz_score}")
        if len(metrics) > max_entries:
            lines.append(
                f"... {len(metrics) - max_entries} additional candidates omitted ..."
            )
        lines.append("--------------------")
        logger.info("\n".join(lines))

    async def _fetch_page(
        self, url: str, *, referer_base_url: str | None = None
    ) -> str | None:
        """Fetch ``url`` and return the response text, handling errors."""

        referer_base = (referer_base_url or self.base_url or "").rstrip("/")
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
        }
        if referer_base:
            headers["Referer"] = f"{referer_base}/"

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
            # logger.error(f"[SCRAPER] HTTP error fetching {url}: {exc}")
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
        # Some sites format counts with thousand separators (e.g., "1,927" or "2.141").
        # Strip all non-digit characters to robustly parse these values.
        cleaned = re.sub(r"[^\d]", "", text)
        return int(cleaned) if cleaned else 0

    def _build_base_url_rotation(self, site_config: dict[str, Any]) -> list[str]:
        """Normalize the primary base URL plus any mirrors into a unique list."""

        def _normalize(url: str) -> str:
            return url.strip().rstrip("/")

        raw_base = site_config.get("base_url")
        base_urls: list[str] = []
        if isinstance(raw_base, str):
            base_urls.append(raw_base)
        elif isinstance(raw_base, list):
            base_urls.extend([item for item in raw_base if isinstance(item, str)])
        else:
            raise ValueError("base_url must be a string or list of strings")

        mirrors = site_config.get("mirror_base_urls", [])
        if isinstance(mirrors, list):
            base_urls.extend([item for item in mirrors if isinstance(item, str)])
        elif mirrors:
            logger.warning(
                "[SCRAPER] mirror_base_urls expected list of strings for %s",
                site_config.get("site_name", ""),
            )

        normalized: list[str] = []
        for candidate in base_urls:
            cleaned = _normalize(candidate)
            if cleaned and cleaned not in normalized:
                normalized.append(cleaned)

        if not normalized:
            raise ValueError("At least one valid base URL must be provided")
        return normalized

    def _iter_base_url_candidates(self) -> list[str]:
        """Return base URLs ordered with the last success first."""
        ordered: list[str] = []
        seen: set[str] = set()
        if isinstance(self._last_successful_base_url, str):
            ordered.append(self._last_successful_base_url)
            seen.add(self._last_successful_base_url)
        for candidate in self.base_url_candidates:
            if candidate not in seen:
                ordered.append(candidate)
                seen.add(candidate)
        return ordered
