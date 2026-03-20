# telegram_bot/services/search_logic/local_search.py

import asyncio
import re

from thefuzz import fuzz, process

from .adapters import is_dir, join_path, list_dir, path_exists, walk_dir
from .filesystem_filters import is_ignored_search_directory, is_ignored_search_file


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

    if not search_path or not path_exists(search_path):
        return None

    matches = []
    match_threshold = 85

    def search_filesystem() -> None:
        for root, dirs, files in walk_dir(search_path):
            if is_ignored_search_directory(root):
                dirs[:] = []
                continue
            dirs[:] = [
                directory_name
                for directory_name in dirs
                if not is_ignored_search_directory(join_path(root, directory_name))
            ]
            filtered_files = [
                file_name for file_name in files if not is_ignored_search_file(file_name)
            ]
            items_to_search = dirs if search_mode == "directory" else filtered_files
            for name in items_to_search:
                ratio = process.extractOne(query, [name], scorer=fuzz.partial_ratio)
                if ratio and ratio[1] > match_threshold:
                    matches.append(join_path(root, name))

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
    if not is_dir(show_path) or is_ignored_search_directory(show_path):
        return None

    pattern = re.compile(rf"season\s+0*{season_num}\b", re.IGNORECASE)

    for dir_name in list_dir(show_path):
        full_path = join_path(show_path, dir_name)
        if is_ignored_search_directory(full_path):
            continue
        if is_dir(full_path) and pattern.search(dir_name):
            return full_path

    return None


async def find_episode_file(season_path: str, season_num: int, episode_num: int) -> str | None:
    """
    Finds a specific episode file within a season directory.
    """
    if not is_dir(season_path) or is_ignored_search_directory(season_path):
        return None

    pattern = re.compile(
        rf"(s0*{season_num}e0*{episode_num}|0*{season_num}x0*{episode_num})\b",
        re.IGNORECASE,
    )

    for file_name in list_dir(season_path):
        full_path = join_path(season_path, file_name)
        if is_dir(full_path) or is_ignored_search_file(full_path):
            continue
        if pattern.search(file_name):
            return full_path

    return None
