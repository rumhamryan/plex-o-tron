# telegram_bot/services/search_logic/orchestrator.py

import asyncio
import re
from collections.abc import Coroutine
from typing import Any

from telegram.ext import ContextTypes

from ...config import logger, resolve_scraper_max_torrent_size_gib
from .. import scraping_service
from ..discovery import CircuitBreaker, DiscoveryOrchestrator, DiscoveryRequest
from ..discovery.orchestrator import PROVIDER_FACTORY
from ..interfaces import ScraperFunction

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


def _create_scraper_task(
    coro: Coroutine[Any, Any, list[dict[str, Any]]],
) -> asyncio.Task[list[dict[str, Any]]]:
    return asyncio.create_task(coro)


def _is_screener_movie_release(result: dict[str, Any]) -> bool:
    """Returns True when a movie title contains known screener release tags."""
    raw_title = result.get("title")
    if not isinstance(raw_title, str) or not raw_title.strip():
        return False
    return bool(_MOVIE_SCREENER_PATTERN.search(raw_title))


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

    websites_config = search_config.get("websites", {})
    if isinstance(websites_config, dict):
        provider_groups.extend(
            [
                websites_config.get("providers", []),
                websites_config.get(config_key, []),
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
        return None

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
    Coordinates searches across all enabled torrent sites concurrently.

    It reads the search configuration, creates an asyncio task for each
    enabled scraper, runs them in parallel, and returns a single list of
    results sorted by score.
    """
    search_config = context.bot_data.get("SEARCH_CONFIG", {})
    websites_config = search_config.get("websites", {})
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
    if discovery_results is not None:
        return discovery_results

    config_key = "movies" if media_type == "movie" else "tv"
    sites_to_scrape = (
        websites_config.get(config_key, []) if isinstance(websites_config, dict) else []
    )

    if not isinstance(sites_to_scrape, list) or not sites_to_scrape:
        logger.warning(
            f"[SEARCH] No websites configured for media type '{config_key}' in config.ini."
        )
        return []

    scraper_map: dict[str, ScraperFunction] = {
        "1337x": scraping_service.scrape_1337x,
        "1337x.to": scraping_service.scrape_1337x,
        "1337x torrents": scraping_service.scrape_1337x,
        "yts": scraping_service.scrape_yts,
        "yts.lt": scraping_service.scrape_yts,
        "yts.mx": scraping_service.scrape_yts,
        "tpb": scraping_service.scrape_tpb,
        "the pirate bay": scraping_service.scrape_tpb,
        "thepiratebay": scraping_service.scrape_tpb,
        "thepiratebay.org": scraping_service.scrape_tpb,
        "pirate bay": scraping_service.scrape_tpb,
        "piratebay": scraping_service.scrape_tpb,
        "eztv": (
            lambda search_query, media_type, site_url, context, **extra_kwargs: (
                scraping_service.scrape_yaml_site(
                    search_query,
                    media_type,
                    site_url,
                    context,
                    site_name=extra_kwargs.pop("site_name", "eztv"),
                    **extra_kwargs,
                )
            )
        ),
    }

    task_entries: list[tuple[str, asyncio.Task[list[dict[str, Any]]]]] = []
    for site_info in sites_to_scrape:
        if not isinstance(site_info, dict):
            logger.warning(f"[SEARCH] Skipping invalid item in '{config_key}' config: {site_info}")
            continue

        if site_info.get("enabled", True):
            site_name = site_info.get("name")
            normalized_name = site_name.strip().lower() if isinstance(site_name, str) else None
            site_url = site_info.get("search_url")

            if not isinstance(site_name, str) or not site_name or not normalized_name:
                logger.warning(
                    f"[SEARCH] Skipping site due to missing or invalid 'name' key: {site_info}"
                )
                continue

            if not site_url:
                logger.warning(
                    f"[SEARCH] Skipping site '{site_name}' due to missing 'search_url' key."
                )
                continue

            canonical_site_name: str = site_name
            if normalized_name and "eztv" in normalized_name:
                canonical_site_name = "eztv"

            search_query = query
            year = kwargs.get("year")

            # Only append the year for the 1337x scraper.
            if normalized_name and normalized_name.startswith("1337x") and year:
                search_query += f" {year}"

            scraper_func = scraper_map.get(normalized_name) if normalized_name else None
            if scraper_func is None and normalized_name and "eztv" in normalized_name:
                scraper_func = scraper_map.get("eztv")

            # Allow callers to override the string used for fuzzy filtering.
            base_filter = kwargs.get("base_query_for_filter", query)
            extra_kwargs = {k: v for k, v in kwargs.items() if k != "base_query_for_filter"}
            extra_kwargs.pop("max_size_gb", None)
            extra_kwargs["max_size_gib"] = max_size_gib
            extra_kwargs["site_name"] = canonical_site_name

            if normalized_name and normalized_name.startswith("1337x") and scraper_func is not None:
                logger.info(
                    f"[SEARCH] Creating search task for '{site_name}' with query: '{query}'"
                )
                scrape_coro = scraper_func(
                    search_query,
                    media_type,
                    site_url,
                    context,
                    base_query_for_filter=base_filter,
                    **extra_kwargs,
                )
                task = _create_scraper_task(scrape_coro)
                task_entries.append((site_name, task))
            elif scraper_func is not None:
                logger.info(
                    f"[SEARCH] Creating search task for '{site_name}' with query: '{query}'"
                )
                scrape_coro = scraper_func(
                    search_query,
                    media_type,
                    site_url,
                    context,
                    base_query_for_filter=base_filter,
                    **extra_kwargs,
                )
                task = _create_scraper_task(scrape_coro)
                task_entries.append((site_name, task))
            else:
                # Fallback: try YAML-backed generic scraper by site name
                logger.info(
                    f"[SEARCH] Creating search task for '{site_name}' (YAML) with query: '{query}'"
                )
                task = _create_scraper_task(
                    scraping_service.scrape_yaml_site(
                        search_query,
                        media_type,
                        site_url,
                        context,
                        site_name=canonical_site_name,
                        base_query_for_filter=base_filter,
                    )
                )
                task_entries.append((site_name, task))

    if not task_entries:
        logger.warning("[SEARCH] No enabled search sites found to orchestrate.")
        return []

    results_from_all_sites = await asyncio.gather(*(entry_task for _, entry_task in task_entries))

    # Flatten, coerce, and sort results
    all_results: list[dict[str, Any]] = []
    for (site_label, _), site_results in zip(task_entries, results_from_all_sites):
        # Enforce schema constraints (e.g. valid integers for swarm counts)
        coerced_results = [scraping_service._coerce_swarm_counts(r) for r in site_results]

        filtered_site_results = coerced_results
        if media_type == "movie":
            blocked_count = 0
            filtered_site_results = []
            for result in coerced_results:
                if _is_screener_movie_release(result):
                    blocked_count += 1
                    continue
                filtered_site_results.append(result)
            if blocked_count:
                logger.info(
                    "[SEARCH] %s: Dropped %d screener-tagged movie result(s).",
                    site_label,
                    blocked_count,
                )

        # Filter by score and viability: remove results with score < 6 or seeders < 20
        viable_results = [
            r
            for r in filtered_site_results
            if r.get("score", 0) >= min_result_score and r.get("seeders", 0) >= min_result_seeders
        ]

        _log_scraper_results(site_label, viable_results)
        all_results.extend(viable_results)

    all_results.sort(key=lambda x: x.get("score", 0), reverse=True)

    logger.info(f"[SEARCH] Orchestration complete. Returning {len(all_results)} sorted results.")
    return all_results


def _log_scraper_results(site_label: str, results: list[dict[str, Any]]) -> None:
    """
    Emits a structured log entry enumerating each scraper result so operators
    can observe exactly what was returned before UI filtering occurs.
    """
    header = f"--- {site_label} Scraper Results ---"
    lines = [header]
    if not results:
        lines.append("No results returned.")
        lines.append("--------------------")
        logger.info("\n".join(lines))
        return

    ordered_fields = [
        "title",
        "info_url",
        # "page_url",
        "score",
        "source",
        "uploader",
        "size_gib",
        "codec",
        "seeders",
        "leechers",
        "year",
        "matched_video_formats",
        "matched_audio_formats",
        "matched_audio_channels",
        "has_video_match",
        "has_audio_match",
        "is_gold_av",
        "is_silver_av",
        "is_bronze_av",
    ]

    for idx, result in enumerate(results, start=1):
        # Guard rail: warn if leechers is somehow missing even after coercion
        if "leechers" not in result:
            logger.warning(f"[SEARCH] Result {idx} from {site_label} is missing 'leechers'")

        lines.append(f"Result {idx}:")
        for field in ordered_fields:
            val = result.get(field)
            # Sanitize swarm counts for display
            if field in ("seeders", "leechers") and val is not None:
                try:
                    val = int(val)
                except (ValueError, TypeError):
                    val = 0
            lines.append(f"  {field}: {val}")
        lines.append("--------------------")
    logger.info("\n".join(lines))
