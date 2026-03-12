# telegram_bot/services/media_manager/paths.py

import os
from typing import Any

from telegram_bot.config import logger

from . import adapters


def _get_path_size_bytes(path: str) -> int | None:
    """Safely return file size in bytes."""
    try:
        return adapters.get_path_size_bytes(path)
    except OSError as exc:
        logger.info("Unable to determine size for '%s': %s", path, exc)
        return None


def _get_disk_usage_percent(path: str) -> int | None:
    """Safely return disk usage percentage for the filesystem backing the path."""
    try:
        total_bytes, used_bytes, _ = adapters.get_disk_usage(path)
    except OSError as exc:
        logger.info("Unable to determine disk usage for '%s': %s", path, exc)
        return None

    if total_bytes <= 0:
        return None

    return round((used_bytes / total_bytes) * 100)


def _get_final_destination_path(parsed_info: dict[str, Any], save_paths: dict[str, str]) -> str:
    """Determines the final directory for the downloaded media."""
    media_type = parsed_info.get("type")

    if media_type == "movie":
        root = save_paths.get("movies", save_paths["default"])
        collection_name = parsed_info.get("collection_name")
        if collection_name:
            safe_col_name = _sanitize_directory_component(collection_name, "Collection")
            return os.path.join(root, safe_col_name)
        return root

    if media_type == "tv":
        root_path = save_paths.get("tv_shows", save_paths["default"])
        show_title = parsed_info.get("title", "Unknown Show")
        season_num = parsed_info.get("season", 0)

        # Sanitize show title for directory name
        invalid_chars = r'<>:"/\\|?*'
        safe_show_title = "".join(c for c in show_title if c not in invalid_chars)

        return os.path.join(root_path, safe_show_title, f"Season {season_num:02d}")

    return save_paths["default"]


def _sanitize_directory_component(value: Any, fallback: str) -> str:
    invalid_chars = '<>:"/\\|?*'
    base = str(value or "").strip()
    safe = "".join(c for c in base if c not in invalid_chars).strip()
    return safe or str(fallback)
