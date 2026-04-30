# telegram_bot/services/search_logic/orchestrator.py

import asyncio
from typing import Any

from telegram.ext import ContextTypes

from ...config import logger, resolve_scraper_max_torrent_size_gib
from ..discovery import CircuitBreaker, DiscoveryOrchestrator, DiscoveryRequest
from ..discovery.orchestrator import PROVIDER_FACTORY

_DEFAULT_MIN_RESULT_SCORE = 6
_DEFAULT_MIN_RESULT_SEEDERS = 20
_DISCOVERY_CIRCUIT_BREAKER_KEY = "DISCOVERY_CIRCUIT_BREAKER"
_DISCOVERY_PROVIDER_KEYS = {
    "name",
    "type",
    "search_url",
    "enabled",
    "timeout_seconds",
    "categories",
}


def _coerce_non_negative_int(value: Any, *, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _coerce_positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _coerce_provider_config(raw_provider: dict[str, Any]) -> dict[str, Any] | None:
    provider_type = str(raw_provider.get("type", "")).strip().casefold()
    if not provider_type or provider_type not in PROVIDER_FACTORY:
        return None
    if raw_provider.get("enabled", True) is False:
        return None
    if not str(raw_provider.get("name", "")).strip():
        return None
    if not str(raw_provider.get("search_url", "")).strip():
        return None

    return {key: value for key, value in raw_provider.items() if key in _DISCOVERY_PROVIDER_KEYS}


def _configured_discovery_providers(
    search_config: dict[str, Any],
    media_type: str,
) -> list[dict[str, Any]]:
    """Returns supported discovery provider configs that apply to this media type."""
    config_key = "movies" if media_type == "movie" else "tv"
    provider_groups: list[Any] = []

    top_level_providers = search_config.get("providers")
    if isinstance(top_level_providers, list):
        provider_groups.append(top_level_providers)
    elif isinstance(top_level_providers, dict):
        provider_groups.extend(
            [
                top_level_providers.get("providers", []),
                top_level_providers.get(config_key, []),
            ]
        )

    providers: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for group in provider_groups:
        if not isinstance(group, list):
            continue
        for raw_item in group:
            if not isinstance(raw_item, dict):
                continue
            provider = _coerce_provider_config(raw_item)
            if provider is None:
                continue
            dedupe_key = (
                str(provider.get("name", "")).casefold(),
                str(provider.get("search_url", "")).casefold(),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            providers.append(provider)

    return providers


def has_configured_discovery_providers(
    search_config: dict[str, Any] | Any,
    media_type: str,
) -> bool:
    if not isinstance(search_config, dict):
        return False
    return bool(_configured_discovery_providers(search_config, media_type))


def _get_discovery_circuit_breaker(bot_data: dict[str, Any]) -> CircuitBreaker:
    breaker = bot_data.get(_DISCOVERY_CIRCUIT_BREAKER_KEY)
    if isinstance(breaker, CircuitBreaker):
        return breaker

    breaker = CircuitBreaker()
    bot_data[_DISCOVERY_CIRCUIT_BREAKER_KEY] = breaker
    return breaker


def _build_discovery_request(
    query: str,
    media_type: str,
    *,
    min_seeders: int,
    max_size_gib: float,
    kwargs: dict[str, Any],
) -> DiscoveryRequest | None:
    normalized_query = query.strip()
    if not normalized_query:
        return None

    year = kwargs.get("year")
    if media_type == "movie" and year is not None:
        year_text = str(year).strip()
        if year_text and year_text not in normalized_query:
            normalized_query = f"{normalized_query} {year_text}"

    return DiscoveryRequest(
        query=normalized_query,
        media_type="movie" if media_type == "movie" else "tv",
        year=year,
        season=kwargs.get("season"),
        episode=kwargs.get("episode"),
        resolution=kwargs.get("resolution"),
        max_size_gib=max_size_gib,
        min_seeders=min_seeders,
        base_query_for_filter=kwargs.get("base_query_for_filter"),
    )


async def _orchestrate_discovery_searches(
    query: str,
    media_type: str,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    search_config: dict[str, Any],
    min_result_score: int,
    min_result_seeders: int,
    max_size_gib: float,
    kwargs: dict[str, Any],
) -> list[dict[str, Any]] | None:
    providers = _configured_discovery_providers(search_config, media_type)
    if not providers:
        logger.warning(
            "[SEARCH] No discovery providers configured for %s searches. "
            "Legacy torrent scrapers have been removed.",
            media_type,
        )
        return []

    request = _build_discovery_request(
        query,
        media_type,
        min_seeders=min_result_seeders,
        max_size_gib=max_size_gib,
        kwargs=kwargs,
    )
    if request is None:
        logger.warning("[SEARCH] Empty query supplied for discovery search.")
        return []

    logger.info(
        "[SEARCH] Using discovery orchestrator with %d provider(s) for %s search.",
        len(providers),
        media_type,
    )
    orchestrator = DiscoveryOrchestrator(
        providers,
        preferences=search_config.get("preferences", {}),
        breaker=_get_discovery_circuit_breaker(context.bot_data),
        min_result_score=min_result_score,
    )
    try:
        results = await orchestrator.search(request)
    except asyncio.CancelledError:
        logger.info(
            "[SEARCH] Discovery search cancelled for %r (%s).",
            request.query,
            request.media_type,
        )
        raise

    for stats in orchestrator.last_provider_stats.values():
        if stats.status in {"failed", "skipped"}:
            logger.info(
                "[SEARCH] Discovery provider %s %s: %s",
                stats.provider_name,
                stats.status,
                stats.error_message or stats.error_type or "unknown error",
            )
        else:
            logger.info(
                "[SEARCH] Discovery provider %s: %d raw, %d viable.",
                stats.provider_name,
                stats.raw_count,
                stats.scored_count,
            )

    logger.info("[SEARCH] Discovery orchestration complete. Returning %d result(s).", len(results))
    return results


async def orchestrate_searches(
    query: str, media_type: str, context: ContextTypes.DEFAULT_TYPE, **kwargs
) -> list[dict[str, Any]]:
    """
    Coordinates torrent searches through provider-backed discovery.

    Legacy direct tracker HTML/API scrapers were removed in Phase 4. This
    function now only builds a DiscoveryRequest and delegates to
    DiscoveryOrchestrator-backed providers.
    """
    search_config = context.bot_data.get("SEARCH_CONFIG", {})
    min_result_score = _coerce_non_negative_int(
        kwargs.get("min_score"),
        default=_DEFAULT_MIN_RESULT_SCORE,
    )
    min_result_seeders = _coerce_non_negative_int(
        kwargs.get("min_seeders"),
        default=_DEFAULT_MIN_RESULT_SEEDERS,
    )
    requested_max_size_gib = kwargs.get("max_size_gib", kwargs.get("max_size_gb"))
    max_size_gib = resolve_scraper_max_torrent_size_gib(
        context.bot_data,
        requested_max_size_gib,
    )
    if max_size_gib is None:
        logger.error(
            "[SEARCH] Invalid max_size_gib override %r; value must be greater than 0.",
            requested_max_size_gib,
        )
        return []

    requested_size_limit_gib = _coerce_positive_float(requested_max_size_gib)
    if requested_size_limit_gib is not None and max_size_gib < requested_size_limit_gib:
        logger.info(
            "[SEARCH] Capping max size at configured limit %.2f GiB (requested %.2f GiB).",
            max_size_gib,
            requested_size_limit_gib,
        )

    discovery_results = await _orchestrate_discovery_searches(
        query,
        media_type,
        context,
        search_config=search_config,
        min_result_score=min_result_score,
        min_result_seeders=min_result_seeders,
        max_size_gib=max_size_gib,
        kwargs=kwargs,
    )
    return discovery_results or []
