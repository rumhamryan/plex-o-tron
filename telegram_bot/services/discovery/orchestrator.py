from __future__ import annotations

import asyncio
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ...config import logger
from ...utils import compute_av_match_metadata, score_torrent_result
from .exceptions import ProviderSearchError
from .health import CircuitBreaker
from .providers.base import BaseProvider
from .providers.torznab import TorznabProvider
from .schemas import DiscoveryRequest, DiscoveryResult, ProviderConfig

DEFAULT_MIN_RESULT_SCORE = 6
_MOVIE_SCREENER_PATTERN = re.compile(
    r"(?ix)\b("
    r"web[-_.\s]*screener|"
    r"(?:dvd|bd)[-_.\s]*screener|"
    r"dvd[-_.\s]*scr|"
    r"dvdscr|"
    r"bdscr|"
    r"screener"
    r")\b"
)

PROVIDER_FACTORY: dict[str, type[BaseProvider]] = {
    "torznab": TorznabProvider,
}


@dataclass(slots=True)
class ProviderSearchStats:
    provider_name: str
    status: str = "pending"
    raw_count: int = 0
    deduplicated_count: int = 0
    filtered_count: int = 0
    scored_count: int = 0
    dropped_duplicate_count: int = 0
    dropped_low_seeders_count: int = 0
    dropped_too_large_count: int = 0
    dropped_screener_count: int = 0
    dropped_low_score_count: int = 0
    raw_samples: list[dict[str, Any]] | None = None
    error_type: str | None = None
    error_message: str | None = None


class DiscoveryOrchestrator:
    """Runs discovery providers, deduplicates results, and applies legacy scoring."""

    def __init__(
        self,
        provider_configs: Sequence[ProviderConfig | Mapping[str, Any]],
        *,
        preferences: Mapping[str, Any] | None = None,
        breaker: CircuitBreaker | None = None,
        min_result_score: int = DEFAULT_MIN_RESULT_SCORE,
    ) -> None:
        self.breaker = breaker or CircuitBreaker()
        self.preferences = dict(preferences or {})
        self.min_result_score = min_result_score
        self.providers: list[BaseProvider] = []
        self.last_provider_stats: dict[str, ProviderSearchStats] = {}

        for raw_config in provider_configs:
            cfg = self._coerce_provider_config(raw_config)
            if cfg is None or not cfg.enabled:
                continue

            provider_cls = PROVIDER_FACTORY.get(cfg.type.casefold())
            if provider_cls is None:
                logger.warning("[DISCOVERY] Unknown provider type %r for %s.", cfg.type, cfg.name)
                continue

            self.providers.append(provider_cls(cfg))

    async def search(
        self,
        request: DiscoveryRequest,
        *,
        preferences: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Executes discovery, deduplication, filtering, and scoring."""
        self.last_provider_stats = {
            provider.config.name: ProviderSearchStats(provider_name=provider.config.name)
            for provider in self.providers
        }
        raw_results = await self._execute_discovery(request)
        unique_results = self._deduplicate(raw_results)
        filtered_results = self._filter_results(unique_results, request)
        formatted_results = [
            self._format_for_legacy_scoring(result, self._preferences_for(request, preferences))
            for result in filtered_results
        ]
        return self._score_and_sort(
            formatted_results,
            request,
            self._preferences_for(request, preferences),
        )

    def _coerce_provider_config(
        self, raw_config: ProviderConfig | Mapping[str, Any]
    ) -> ProviderConfig | None:
        if isinstance(raw_config, ProviderConfig):
            return raw_config
        try:
            return ProviderConfig(**dict(raw_config))
        except (TypeError, ValueError) as exc:
            logger.warning("[DISCOVERY] Skipping invalid provider config %r: %s", raw_config, exc)
            return None

    async def _execute_discovery(self, request: DiscoveryRequest) -> list[DiscoveryResult]:
        task_entries: list[tuple[BaseProvider, asyncio.Task[list[DiscoveryResult]]]] = []

        for provider in self.providers:
            provider_name = provider.config.name
            if not self.breaker.is_healthy(provider_name):
                logger.warning("[DISCOVERY] Skipping %s because it is cooling down.", provider_name)
                self._mark_provider_failed(
                    provider_name,
                    error_type="CircuitBreakerOpen",
                    error_message="Provider is cooling down.",
                    status="skipped",
                )
                continue

            task_entries.append((provider, asyncio.create_task(provider.search(request))))

        if not task_entries:
            return []

        gathered = await asyncio.gather(
            *(task for _, task in task_entries),
            return_exceptions=True,
        )

        all_found: list[DiscoveryResult] = []
        for (provider, _), result in zip(task_entries, gathered):
            provider_name = provider.config.name
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, ProviderSearchError):
                logger.warning("[DISCOVERY] %s failed: %s", provider_name, result)
                self._mark_provider_failed(
                    provider_name,
                    error_type=type(result.__cause__ or result).__name__,
                    error_message=str(result.__cause__ or result),
                )
                self.breaker.record_failure(provider_name)
                continue
            if isinstance(result, Exception):
                logger.error("[DISCOVERY] %s failed unexpectedly: %s", provider_name, result)
                self._mark_provider_failed(
                    provider_name,
                    error_type=type(result).__name__,
                    error_message=str(result),
                )
                self.breaker.record_failure(provider_name)
                continue
            if isinstance(result, BaseException):
                raise result

            self.breaker.record_success(provider_name)
            stats = self.last_provider_stats.get(provider_name)
            if stats is not None:
                stats.status = "success"
                stats.raw_count = len(result)
                stats.raw_samples = [self._sample_result(item) for item in result[:5]]
            all_found.extend(result)

        return all_found

    def _deduplicate(self, results: Sequence[DiscoveryResult]) -> list[DiscoveryResult]:
        seen_keys: set[str] = set()
        unique: list[DiscoveryResult] = []

        for result in sorted(results, key=lambda item: item.seeders, reverse=True):
            dedupe_key = self._dedupe_key(result)
            if dedupe_key is not None:
                if dedupe_key in seen_keys:
                    stats = self.last_provider_stats.get(result.source)
                    if stats is not None:
                        stats.dropped_duplicate_count += 1
                    continue
                seen_keys.add(dedupe_key)
            stats = self.last_provider_stats.get(result.source)
            if stats is not None:
                stats.deduplicated_count += 1
            unique.append(result)

        return unique

    def _dedupe_key(self, result: DiscoveryResult) -> str | None:
        info_hash = (result.info_hash or self._extract_info_hash(result.magnet_url) or "").strip()
        if info_hash:
            return f"hash:{info_hash.casefold()}"
        if result.magnet_url:
            return f"magnet:{result.magnet_url.strip().casefold()}"
        return None

    def _extract_info_hash(self, magnet_url: str | None) -> str | None:
        if not magnet_url or not magnet_url.startswith("magnet:"):
            return None
        parsed = urllib.parse.urlsplit(magnet_url)
        params = urllib.parse.parse_qs(parsed.query)
        for xt_value in params.get("xt", []):
            lowered = xt_value.casefold()
            marker = "urn:btih:"
            if marker in lowered:
                return xt_value[lowered.index(marker) + len(marker) :].strip() or None
        return None

    def _filter_results(
        self,
        results: Sequence[DiscoveryResult],
        request: DiscoveryRequest,
    ) -> list[DiscoveryResult]:
        filtered: list[DiscoveryResult] = []
        for result in results:
            if result.seeders < request.min_seeders:
                stats = self.last_provider_stats.get(result.source)
                if stats is not None:
                    stats.dropped_low_seeders_count += 1
                continue
            if (
                request.max_size_gib is not None
                and result.size_bytes / (1024**3) > request.max_size_gib
            ):
                stats = self.last_provider_stats.get(result.source)
                if stats is not None:
                    stats.dropped_too_large_count += 1
                continue
            if request.media_type == "movie" and self._is_screener_movie_release(result):
                stats = self.last_provider_stats.get(result.source)
                if stats is not None:
                    stats.dropped_screener_count += 1
                continue
            stats = self.last_provider_stats.get(result.source)
            if stats is not None:
                stats.filtered_count += 1
            filtered.append(result)
        return filtered

    def _is_screener_movie_release(self, result: DiscoveryResult) -> bool:
        return bool(_MOVIE_SCREENER_PATTERN.search(result.title))

    def _preferences_for(
        self,
        request: DiscoveryRequest,
        override: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        preferences = dict(override or self.preferences)
        media_key = "movies" if request.media_type == "movie" else "tv"
        media_preferences = preferences.get(media_key)
        if isinstance(media_preferences, Mapping):
            return dict(media_preferences)
        return preferences

    def _format_for_legacy_scoring(
        self,
        result: DiscoveryResult,
        preferences: Mapping[str, Any],
    ) -> dict[str, Any]:
        av_metadata = compute_av_match_metadata(result.title, dict(preferences))
        return {
            "title": result.title,
            "page_url": result.magnet_url or result.download_url,
            "magnet_url": result.magnet_url,
            "info_url": result.info_url,
            "source": result.source,
            "size_gib": result.size_bytes / (1024**3),
            "seeders": result.seeders,
            "leechers": result.leechers,
            "uploader": result.uploader or "",
            "codec": result.codec,
            "year": result.year,
            "matched_video_formats": av_metadata["matched_video_formats"],
            "matched_audio_formats": av_metadata["matched_audio_formats"],
            "matched_audio_channels": av_metadata["matched_audio_channels"],
            "has_video_match": av_metadata["has_video_match"],
            "has_audio_match": av_metadata["has_audio_match"],
            "is_gold_av": av_metadata["is_gold_av"],
            "is_silver_av": av_metadata["is_silver_av"],
            "is_bronze_av": av_metadata["is_bronze_av"],
        }

    def _score_and_sort(
        self,
        results: Sequence[dict[str, Any]],
        request: DiscoveryRequest,
        preferences: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        scored: list[dict[str, Any]] = []
        for result in results:
            score = score_torrent_result(
                str(result.get("title") or ""),
                str(result.get("uploader") or ""),
                dict(preferences),
                seeders=int(result.get("seeders") or 0),
                leechers=int(result.get("leechers") or 0),
            )
            if score < self.min_result_score:
                source = result.get("source")
                stats = self.last_provider_stats.get(str(source))
                if stats is not None:
                    stats.dropped_low_score_count += 1
                continue
            result["score"] = score
            source = result.get("source")
            stats = self.last_provider_stats.get(str(source))
            if stats is not None:
                stats.scored_count += 1
            scored.append(result)

        return sorted(scored, key=lambda item: item.get("score", 0), reverse=True)

    def _sample_result(self, result: DiscoveryResult) -> dict[str, Any]:
        return {
            "title": result.title,
            "seeders": result.seeders,
            "leechers": result.leechers,
            "size_gib": round(result.size_bytes / (1024**3), 2),
            "info_url": result.info_url,
            "raw_attrs": result.raw_data.get("attrs"),
        }

    def _mark_provider_failed(
        self,
        provider_name: str,
        *,
        error_type: str,
        error_message: str,
        status: str = "failed",
    ) -> None:
        stats = self.last_provider_stats.get(provider_name)
        if stats is None:
            return
        stats.status = status
        stats.error_type = error_type
        stats.error_message = error_message
