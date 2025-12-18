# telegram_bot/services/scraping_service.py

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Hashable, TypedDict

from ..config import logger
from .scrapers import (
    fetch_episode_title_from_wikipedia,
    fetch_movie_years_from_wikipedia as _raw_fetch_movie_years,
    fetch_movie_franchise_details_from_wikipedia as _raw_fetch_franchise_details,
    fetch_episode_titles_for_season as _raw_fetch_episode_titles_for_season,
    fetch_total_seasons_from_wikipedia as _raw_fetch_total_seasons,
    fetch_season_episode_count_from_wikipedia as _raw_fetch_episode_count,
    scrape_yts,
    scrape_1337x,
    scrape_tpb,
    find_magnet_link_on_page,
    scrape_generic_page,
    scrape_yaml_site,
    _get_page_html,
    _strategy_find_direct_links,
    _strategy_contextual_search,
    _strategy_find_in_tables,
    _score_candidate_links,
    _WIKI_TITLES_CACHE,
    _WIKI_SOUP_CACHE,
    _WIKI_MOVIE_CACHE,
)
from .generic_torrent_scraper import GenericTorrentScraper, load_site_config

WIKI_CACHE_MAX_ENTRIES = 100
WIKI_CACHE_TTL_SECONDS = 30 * 60  # 30 minutes
WIKI_CACHE_FAILURE_TTL_SECONDS = 5 * 60  # Negative cache entries expire quickly


class ScraperResult(TypedDict):
    """
    Standardized schema for a torrent search result.

    All scrapers must return a list of dictionaries confirming to this shape.
    Leechers are mandatory to support swarm health scoring.
    """

    title: str
    page_url: str  # Usually a magnet link or .torrent URL
    info_url: str | None  # URL to the torrent's detail/info page for auditing
    score: int
    source: str
    uploader: str | None
    size_gb: float
    codec: str | None
    seeders: int
    leechers: int
    year: int | None


def _coerce_swarm_counts(result: dict[str, Any]) -> dict[str, Any]:
    """
    Ensures seeders and leechers are non-negative integers.

    If 'leechers' is missing, it logs a warning (fail-fast during dev)
    and defaults to 0 to prevent downstream crashes.
    """
    if "leechers" not in result:
        logger.warning(
            "[SCRAPER] Result from '%s' missing 'leechers' field: %s",
            result.get("source", "unknown"),
            result.get("title", "unknown"),
        )

    try:
        s = int(result.get("seeders", 0))
        result["seeders"] = s if s >= 0 else 0
    except (ValueError, TypeError):
        result["seeders"] = 0

    try:
        leechers = int(result.get("leechers", 0))
        result["leechers"] = leechers if leechers >= 0 else 0
    except (ValueError, TypeError):
        result["leechers"] = 0

    return result


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


class WikiCache:
    """Simple TTL-based LRU cache for Wikipedia helper functions."""

    MISS = object()

    def __init__(
        self,
        *,
        max_entries: int = WIKI_CACHE_MAX_ENTRIES,
        ttl: float = WIKI_CACHE_TTL_SECONDS,
        failure_ttl: float = WIKI_CACHE_FAILURE_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.max_entries = max(1, max_entries)
        self.ttl = ttl
        self.failure_ttl = failure_ttl
        self._clock = clock or time.monotonic
        self._entries: OrderedDict[Hashable, _CacheEntry] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> Any:
        with self._lock:
            entry = self._entries.get(key)
            if not entry:
                return WikiCache.MISS
            if entry.expires_at <= self._clock():
                del self._entries[key]
                return WikiCache.MISS
            self._entries.move_to_end(key)
            return entry.value

    def set(self, key: Hashable, value: Any, *, ttl: float | None = None) -> None:
        expires_at = self._clock() + (ttl if ttl is not None else self.ttl)
        with self._lock:
            self._entries[key] = _CacheEntry(value=value, expires_at=expires_at)
            self._entries.move_to_end(key)
            self._evict_if_needed()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:  # pragma: no cover - convenience
        with self._lock:
            return len(self._entries)

    def _evict_if_needed(self) -> None:
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)


_WIKI_LOOKUP_CACHE = WikiCache()


def _display_title(value: str) -> str:
    stripped = value.strip()
    return stripped or value


def _normalize_cache_key(value: str) -> str:
    return _display_title(value).casefold()


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _movie_years_cache_key(title: str) -> tuple[str, str]:
    return ("movie_years", _normalize_cache_key(title))


def _franchise_cache_key(title: str) -> tuple[str, str]:
    return ("movie_franchise", _normalize_cache_key(title))


def _season_count_cache_key(title: str) -> tuple[str, str]:
    return ("season_count", _normalize_cache_key(title))


def _episode_count_cache_key(title: str, season: int) -> tuple[str, str, int]:
    return ("episode_count", _normalize_cache_key(title), _safe_int(season))


def _episode_titles_cache_key(title: str, season: int) -> tuple[str, str, int]:
    return ("episode_titles", _normalize_cache_key(title), _safe_int(season))


def _season_label(title: str, season: int) -> str:
    try:
        return f"{title} S{int(season):02d}"
    except (TypeError, ValueError):
        return title


def _log_cache_event(hit: bool, bucket: str, identifier: str) -> None:
    action = "HIT" if hit else "MISS"
    logger.info("[WIKI_CACHE] %s %s for '%s'", action, bucket, identifier or "?")


def _store_lookup_value(key: Hashable, value: Any, *, success: bool) -> None:
    ttl = _WIKI_LOOKUP_CACHE.ttl if success else _WIKI_LOOKUP_CACHE.failure_ttl
    _WIKI_LOOKUP_CACHE.set(key, value, ttl=ttl)


def clear_wiki_cache() -> None:
    """Clears the process-wide Wikipedia lookup cache (used in tests)."""
    _WIKI_LOOKUP_CACHE.clear()


def get_cached_movie_years(title: str) -> tuple[list[int], str | None] | None:
    """Returns cached movie year data without performing a fresh lookup."""
    cached = _WIKI_LOOKUP_CACHE.get(_movie_years_cache_key(title))
    if cached is WikiCache.MISS:
        return None
    return cached


async def fetch_movie_years_from_wikipedia(
    movie_title: str, _last_resort: bool = False
) -> tuple[list[int], str | None]:
    normalized_title = _display_title(movie_title)
    cache_key = _movie_years_cache_key(movie_title)
    cached = _WIKI_LOOKUP_CACHE.get(cache_key)
    if cached is not WikiCache.MISS:
        _log_cache_event(True, "movie_years", normalized_title)
        return cached

    _log_cache_event(False, "movie_years", normalized_title)
    result = await _raw_fetch_movie_years(movie_title, _last_resort=_last_resort)
    years: list[int] | None = None
    if isinstance(result, tuple) and result:
        years = result[0]
    _store_lookup_value(cache_key, result, success=bool(years))
    return result


async def fetch_movie_franchise_details(
    movie_title: str,
) -> tuple[str, list[dict[str, Any]]] | None:
    """Returns a franchise name and list of movies, when available."""
    normalized_title = _display_title(movie_title)
    cache_key = _franchise_cache_key(normalized_title)
    cached = _WIKI_LOOKUP_CACHE.get(cache_key)
    if cached is not WikiCache.MISS:
        _log_cache_event(True, "movie_franchise", normalized_title)
        return cached

    _log_cache_event(False, "movie_franchise", normalized_title)
    result = await _raw_fetch_franchise_details(movie_title)
    success = bool(result and isinstance(result, tuple) and len(result) == 2)
    _store_lookup_value(cache_key, result, success=success)
    return result


async def fetch_total_seasons_from_wikipedia(
    show_title: str, _last_resort: bool = False
) -> int | None:
    normalized_title = _display_title(show_title)
    cache_key = _season_count_cache_key(show_title)
    cached = _WIKI_LOOKUP_CACHE.get(cache_key)
    if cached is not WikiCache.MISS:
        _log_cache_event(True, "season_count", normalized_title)
        return cached

    _log_cache_event(False, "season_count", normalized_title)
    result = await _raw_fetch_total_seasons(show_title, _last_resort=_last_resort)
    success = isinstance(result, int) and result > 0
    _store_lookup_value(cache_key, result, success=success)
    return result


async def fetch_season_episode_count_from_wikipedia(
    show_title: str, season: int, _last_resort: bool = False
) -> int | None:
    normalized_title = _display_title(show_title)
    cache_key = _episode_count_cache_key(show_title, season)
    season_tag = _season_label(normalized_title, season)
    cached = _WIKI_LOOKUP_CACHE.get(cache_key)
    if cached is not WikiCache.MISS:
        _log_cache_event(True, "episode_count", season_tag)
        return cached

    _log_cache_event(False, "episode_count", season_tag)
    result = await _raw_fetch_episode_count(
        show_title, season, _last_resort=_last_resort
    )
    success = isinstance(result, int) and result > 0
    _store_lookup_value(cache_key, result, success=success)
    return result


async def fetch_episode_titles_for_season(
    show_title: str, season: int, _last_resort: bool = False
) -> tuple[dict[int, str], str | None]:
    normalized_title = _display_title(show_title)
    cache_key = _episode_titles_cache_key(show_title, season)
    season_tag = _season_label(normalized_title, season)
    cached = _WIKI_LOOKUP_CACHE.get(cache_key)
    if cached is not WikiCache.MISS:
        _log_cache_event(True, "episode_titles", season_tag)
        return cached

    _log_cache_event(False, "episode_titles", season_tag)
    result = await _raw_fetch_episode_titles_for_season(
        show_title, season, _last_resort=_last_resort
    )
    titles = result[0] if isinstance(result, tuple) and result else {}
    _store_lookup_value(cache_key, result, success=bool(titles))
    return result


__all__ = [
    "WikiCache",
    "clear_wiki_cache",
    "get_cached_movie_years",
    "fetch_episode_title_from_wikipedia",
    "fetch_movie_years_from_wikipedia",
    "fetch_movie_franchise_details",
    "fetch_episode_titles_for_season",
    "fetch_total_seasons_from_wikipedia",
    "fetch_season_episode_count_from_wikipedia",
    "scrape_yts",
    "scrape_1337x",
    "scrape_tpb",
    "find_magnet_link_on_page",
    "scrape_generic_page",
    "scrape_yaml_site",
    "_get_page_html",
    "_strategy_find_direct_links",
    "_strategy_contextual_search",
    "_strategy_find_in_tables",
    "_score_candidate_links",
    "_WIKI_TITLES_CACHE",
    "_WIKI_SOUP_CACHE",
    "_WIKI_MOVIE_CACHE",
    "GenericTorrentScraper",
    "load_site_config",
    "ScraperResult",
    "_coerce_swarm_counts",
]
