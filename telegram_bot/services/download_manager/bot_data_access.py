from __future__ import annotations

from typing import Any, cast

from telegram_bot.domain.types import BatchMeta, DownloadData


def get_active_downloads(bot_data: dict[str, Any]) -> dict[str, DownloadData]:
    """Return active downloads map from bot data."""
    return cast(dict[str, DownloadData], bot_data.get("active_downloads", {}))


def get_download_queues(bot_data: dict[str, Any]) -> dict[str, list[DownloadData]]:
    """Return queued downloads map from bot data."""
    return cast(dict[str, list[DownloadData]], bot_data.get("download_queues", {}))


def get_save_paths(bot_data: dict[str, Any]) -> dict[str, str]:
    """Return configured save paths, defaulting to an empty mapping."""
    return cast(dict[str, str], bot_data.get("SAVE_PATHS", {}))


def get_plex_config(bot_data: dict[str, Any]) -> Any:
    """Return Plex config when available."""
    return bot_data.get("PLEX_CONFIG")


def get_or_create_download_batches(bot_data: dict[str, Any]) -> dict[str, BatchMeta]:
    """Return batch metadata map, creating it when missing."""
    return cast(dict[str, BatchMeta], bot_data.setdefault("DOWNLOAD_BATCHES", {}))


def require_active_downloads(bot_data: dict[str, Any]) -> dict[str, DownloadData]:
    """Return active downloads map and raise if missing."""
    return cast(dict[str, DownloadData], bot_data["active_downloads"])


def require_download_queues(bot_data: dict[str, Any]) -> dict[str, list[DownloadData]]:
    """Return queued downloads map and raise if missing."""
    return cast(dict[str, list[DownloadData]], bot_data["download_queues"])


def require_save_paths(bot_data: dict[str, Any]) -> dict[str, str]:
    """Return configured save paths and raise if missing."""
    return cast(dict[str, str], bot_data["SAVE_PATHS"])
