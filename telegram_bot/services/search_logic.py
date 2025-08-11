# telegram_bot/services/search_logic.py

import asyncio
import os
import re
from typing import Any, Callable, Coroutine, Dict, List, Optional, Union

from telegram.ext import ContextTypes
from thefuzz import fuzz, process

from ..config import logger
from . import scraping_service

# --- Type Aliases for Readability ---
ScraperCoroutine = Coroutine[Any, Any, List[Dict[str, Any]]]
ScraperFunction = Callable[..., ScraperCoroutine]


# --- Search Orchestration ---

async def orchestrate_searches(
    query: str, media_type: str, context: ContextTypes.DEFAULT_TYPE, **kwargs
) -> List[Dict[str, Any]]:
    """
    Coordinates searches across all enabled torrent sites concurrently.

    It reads the search configuration, creates an asyncio task for each
    enabled scraper, runs them in parallel, and returns a single list of
    results sorted by score.
    """
    search_config = context.bot_data.get("SEARCH_CONFIG", {})
    websites_config = search_config.get("websites", {})
    
    config_key = 'movies' if media_type == 'movie' else 'tv'
    sites_to_scrape = websites_config.get(config_key, [])

    if not isinstance(sites_to_scrape, list) or not sites_to_scrape:
        logger.warning(f"[SEARCH] No websites configured for media type '{config_key}' in config.ini.")
        return []

    # --- FIX: Correct the scraper map. The YTS scraper is only for YTS. ---
    # A dedicated scraper for EZTV would need to be created in the future.
    scraper_map: Dict[str, ScraperFunction] = {
        "1337x": scraping_service.scrape_1337x,
        "YTS.mx": scraping_service.scrape_yts,
    }

    tasks = []
    for site_info in sites_to_scrape:
        if not isinstance(site_info, dict):
            logger.warning(f"[SEARCH] Skipping invalid item in '{config_key}' config: {site_info}")
            continue

        if site_info.get("enabled", True):
            site_name = site_info.get("name")
            site_url = site_info.get("search_url")

            if not isinstance(site_name, str) or not site_name:
                logger.warning(f"[SEARCH] Skipping site due to missing or invalid 'name' key: {site_info}")
                continue

            if not site_url:
                logger.warning(f"[SEARCH] Skipping site '{site_name}' due to missing 'search_url' key.")
                continue

            scraper_func = scraper_map.get(site_name)
            
            if scraper_func:
                logger.info(f"[SEARCH] Creating search task for '{site_name}' with query: '{query}'")
                task = asyncio.create_task(
                    scraper_func(query, media_type, site_url, context, **kwargs)
                )
                tasks.append(task)
            else:
                logger.warning(f"[SEARCH] Configured site '{site_name}' has no corresponding scraper function. It will be ignored.")

    if not tasks:
        logger.warning("[SEARCH] No enabled search sites found to orchestrate.")
        return []

    results_from_all_sites = await asyncio.gather(*tasks)
    all_results = [result for sublist in results_from_all_sites for result in sublist]
    all_results.sort(key=lambda x: x.get('score', 0), reverse=True)

    logger.info(f"[SEARCH] Orchestration complete. Returning {len(all_results)} sorted results.")
    return all_results


# --- Result Scoring and Parsing ---

def score_torrent_result(title: str, uploader: str, preferences: Dict[str, Any], seeders: int = 0) -> int: # <--- CHANGE THIS
    """
    Scores a torrent result based on user preferences (codecs, uploaders, etc.).
    This version correctly handles a dictionary of preferences with weighted scores.
    """
    score = 0
    title_lower = title.lower()
    
    # Score based on codecs (e.g., "x265": 2)
    for codec, value in preferences.get('codecs', {}).items():
        if codec.lower() in title_lower:
            score += value

    # Score based on resolutions/quality (e.g., "1080p": 5)
    for quality, value in preferences.get('resolutions', {}).items():
        if quality.lower() in title_lower:
            score += value
            
    # Score based on trusted uploaders (e.g., "MeGusta": 5)
    for trusted_uploader, value in preferences.get('uploaders', {}).items():
        if trusted_uploader.lower() == uploader.lower():
            score += value

    # Add the raw seeder count directly to the score
    score += seeders

    return score


def _parse_codec(title: str) -> Optional[str]:
    """Extracts codec information like 'x265' or 'x264' from a torrent title."""
    title_lower = title.lower()
    if 'x265' in title_lower or 'hevc' in title_lower:
        return 'x265'
    if 'x264' in title_lower:
        return 'x264'
    return None


def _parse_size_to_gb(size_str: str) -> float:
    """Converts size strings like '1.5 GB' or '500 MB' to a float in GB."""
    size_str = size_str.lower().replace(',', '')
    try:
        size_match = re.search(r'([\d.]+)', size_str)
        if not size_match: return 0.0
        
        size_val = float(size_match.group(1))
        if 'gb' in size_str:
            return size_val
        if 'mb' in size_str:
            return size_val / 1024
        if 'kb' in size_str:
            return size_val / (1024 * 1024)
            
    except (ValueError, TypeError):
        return 0.0
    return 0.0


# --- Local Filesystem Searching (for Delete workflow) ---

async def find_media_by_name(
    media_type: str, query: str, save_paths: Dict[str, str], search_mode: str = 'directory'
) -> Union[str, List[str], None]:
    """
    Finds a movie or TV show in the local library using fuzzy string matching.
    """
    path_key = 'movies' if media_type == 'movie' else 'tv_shows'
    search_path = save_paths.get(path_key)

    if not search_path or not os.path.exists(search_path):
        return None

    matches = []
    match_threshold = 85

    def search_filesystem():
        for root, dirs, files in os.walk(search_path):
            items_to_search = dirs if search_mode == 'directory' else files
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


async def find_season_directory(show_path: str, season_num: int) -> Optional[str]:
    """
    Finds the directory for a specific season within a TV show's folder.
    """
    if not os.path.isdir(show_path):
        return None

    pattern = re.compile(rf'season\s+0*{season_num}\b', re.IGNORECASE)

    for dir_name in os.listdir(show_path):
        full_path = os.path.join(show_path, dir_name)
        if os.path.isdir(full_path) and pattern.search(dir_name):
            return full_path
            
    return None


async def find_episode_file(season_path: str, season_num: int, episode_num: int) -> Optional[str]:
    """
    Finds a specific episode file within a season directory.
    """
    if not os.path.isdir(season_path):
        return None

    pattern = re.compile(rf'(s0*{season_num}e0*{episode_num}|0*{season_num}x0*{episode_num})\b', re.IGNORECASE)

    for file_name in os.listdir(season_path):
        if pattern.search(file_name):
            return os.path.join(season_path, file_name)
            
    return None