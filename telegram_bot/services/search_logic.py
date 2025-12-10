# telegram_bot/services/search_logic.py

import asyncio
import os
import re
from typing import Any
from collections.abc import Callable, Coroutine

from telegram.ext import ContextTypes
from thefuzz import fuzz, process

from ..config import logger
from . import scraping_service

# --- Type Aliases for Readability ---
ScraperCoroutine = Coroutine[Any, Any, list[dict[str, Any]]]
ScraperFunction = Callable[..., ScraperCoroutine]


# --- Search Orchestration ---


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
            logger.warning(
                f"[SEARCH] Skipping invalid item in '{config_key}' config: {site_info}"
            )
            continue

        if site_info.get("enabled", True):
            site_name = site_info.get("name")
            normalized_name = (
                site_name.strip().lower() if isinstance(site_name, str) else None
            )
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
            extra_kwargs = {
                k: v for k, v in kwargs.items() if k != "base_query_for_filter"
            }
            extra_kwargs["site_name"] = canonical_site_name

            if (
                normalized_name
                and normalized_name.startswith("1337x")
                and scraper_func is not None
            ):
                logger.info(
                    f"[SEARCH] Creating search task for '{site_name}' with query: '{query}'"
                )
                task = asyncio.create_task(
                    scraper_func(
                        search_query,
                        media_type,
                        site_url,
                        context,
                        base_query_for_filter=base_filter,
                        **extra_kwargs,
                    )
                )
                task_entries.append((site_name, task))
            elif scraper_func is not None:
                logger.info(
                    f"[SEARCH] Creating search task for '{site_name}' with query: '{query}'"
                )
                task = asyncio.create_task(
                    scraper_func(
                        search_query,
                        media_type,
                        site_url,
                        context,
                        base_query_for_filter=base_filter,
                        **extra_kwargs,
                    )
                )
                task_entries.append((site_name, task))
            else:
                # Fallback: try YAML-backed generic scraper by site name
                logger.info(
                    f"[SEARCH] Creating search task for '{site_name}' (YAML) with query: '{query}'"
                )
                task = asyncio.create_task(
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

    results_from_all_sites = await asyncio.gather(
        *(entry_task for _, entry_task in task_entries)
    )

    for (site_label, _), site_results in zip(task_entries, results_from_all_sites):
        _log_scraper_results(site_label, site_results)
    all_results = [result for sublist in results_from_all_sites for result in sublist]
    all_results.sort(key=lambda x: x.get("score", 0), reverse=True)

    logger.info(
        f"[SEARCH] Orchestration complete. Returning {len(all_results)} sorted results."
    )
    return all_results


# --- Result Scoring and Parsing ---
# (Moved to telegram_bot/utils.py to avoid circular imports)


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
        lines.append(f"Result {idx}:")
        for field in ordered_fields:
            lines.append(f"  {field}: {result.get(field)}")
        lines.append("--------------------")
    logger.info("\n".join(lines))


def _parse_size_to_gb(size_str: str) -> float:
    """Converts size strings like '1.5 GB' or '500 MB' to a float in GB."""
    size_str = size_str.lower().replace(",", "")
    try:
        size_match = re.search(r"([\d.]+)", size_str)
        if not size_match:
            return 0.0

        size_val = float(size_match.group(1))
        if "gb" in size_str:
            return size_val
        if "mb" in size_str:
            return size_val / 1024
        if "kb" in size_str:
            return size_val / (1024 * 1024)

    except (ValueError, TypeError):
        return 0.0
    return 0.0


# --- Local Filesystem Searching (for Delete workflow) ---


async def find_media_by_name(
    media_type: str,
    query: str,
    save_paths: dict[str, str],
    search_mode: str = "directory",
) -> str | list[str] | None:
    """
    Finds a movie or TV show in the local library using fuzzy string matching.
    """
    path_key = "movies" if media_type == "movie" else "tv_shows"
    search_path = save_paths.get(path_key)

    if not search_path or not os.path.exists(search_path):
        return None

    matches = []
    match_threshold = 85

    def search_filesystem():
        for root, dirs, files in os.walk(search_path):
            items_to_search = dirs if search_mode == "directory" else files
            for name in items_to_search:
                ratio = process.extractOne(query, [name], scorer=fuzz.partial_ratio)
                if ratio and ratio[1] > match_threshold:
                    matches.append(os.path.join(root, name))

    await asyncio.to_thread(search_filesystem)

    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    return matches


async def find_season_directory(show_path: str, season_num: int) -> str | None:
    """
    Finds the directory for a specific season within a TV show's folder.
    """
    if not os.path.isdir(show_path):
        return None

    pattern = re.compile(rf"season\s+0*{season_num}\b", re.IGNORECASE)

    for dir_name in os.listdir(show_path):
        full_path = os.path.join(show_path, dir_name)
        if os.path.isdir(full_path) and pattern.search(dir_name):
            return full_path

    return None


async def find_episode_file(
    season_path: str, season_num: int, episode_num: int
) -> str | None:
    """
    Finds a specific episode file within a season directory.
    """
    if not os.path.isdir(season_path):
        return None

    pattern = re.compile(
        rf"(s0*{season_num}e0*{episode_num}|0*{season_num}x0*{episode_num})\b",
        re.IGNORECASE,
    )

    for file_name in os.listdir(season_path):
        if pattern.search(file_name):
            return os.path.join(season_path, file_name)

    return None
