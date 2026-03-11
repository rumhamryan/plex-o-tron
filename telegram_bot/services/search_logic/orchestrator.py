# telegram_bot/services/search_logic/orchestrator.py

import asyncio
from collections.abc import Coroutine
from typing import Any

from telegram.ext import ContextTypes

from ...config import logger
from .. import scraping_service
from ..interfaces import ScraperFunction


def _create_scraper_task(
    coro: Coroutine[Any, Any, list[dict[str, Any]]],
) -> asyncio.Task[list[dict[str, Any]]]:
    return asyncio.create_task(coro)


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

    config_key = "movies" if media_type == "movie" else "tv"
    sites_to_scrape = websites_config.get(config_key, [])

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

        # Filter by score and viability: remove results with score < 6 or seeders < 20
        viable_results = [
            r for r in coerced_results if r.get("score", 0) >= 6 and r.get("seeders", 0) >= 20
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
        "size_gb",
        "codec",
        "seeders",
        "leechers",
        "year",
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
    # logger.info("\n".join(lines))
