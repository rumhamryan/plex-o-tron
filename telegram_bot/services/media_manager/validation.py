# telegram_bot/services/media_manager/validation.py

import os
from typing import Any

import libtorrent as lt
from telegram import Message
from telegram.helpers import escape_markdown

from telegram_bot.config import (
    ALLOWED_EXTENSIONS,
    MAX_TORRENT_SIZE_BYTES,
    MAX_TORRENT_SIZE_GIB,
    logger,
)
from telegram_bot.services.scraping_service import fetch_episode_title_from_wikipedia
from telegram_bot.utils import format_bytes, parse_torrent_name, safe_edit_message


def _looks_like_sample(path: str) -> bool:
    """Returns True when the path strongly suggests the file is a sample clip."""
    normalized_parts = [part for part in path.replace("\\", "/").lower().split("/") if part]
    if not normalized_parts:
        return False

    filename = normalized_parts[-1]
    stem = os.path.splitext(filename)[0]

    return "sample" in stem or any(part == "sample" for part in normalized_parts[:-1])


def select_primary_media_file(files: lt.file_storage) -> tuple[str, str, int] | None:  # type: ignore
    """
    Selects the best candidate media file from a torrent.

    For single-item downloads we prefer the largest valid media file, while
    deprioritizing obvious sample clips when other media files are present.
    """
    candidates: list[tuple[bool, int, str, str]] = []

    for i in range(files.num_files()):
        path_in_torrent = files.file_path(i)
        _, ext = os.path.splitext(path_in_torrent)
        if ext.lower() not in ALLOWED_EXTENSIONS:
            continue

        candidates.append(
            (
                _looks_like_sample(path_in_torrent),
                files.file_size(i),
                path_in_torrent,
                ext,
            )
        )

    if not candidates:
        return None

    non_sample_candidates = [candidate for candidate in candidates if not candidate[0]]
    selection_pool = non_sample_candidates or candidates
    _, size_bytes, path_in_torrent, ext = max(selection_pool, key=lambda candidate: candidate[1])
    return path_in_torrent, ext, size_bytes


def get_dominant_file_type(files: lt.file_storage) -> str:  # type: ignore
    """Determines the file extension of the largest file in a torrent."""
    if files.num_files() == 0:
        return "N/A"
    largest_file_index = max(range(files.num_files()), key=files.file_size)
    largest_filename = files.file_path(largest_file_index)
    _, extension = os.path.splitext(largest_filename)
    return extension[1:].upper() if extension else "N/A"


def validate_torrent_files(ti: lt.torrent_info) -> str | None:  # type: ignore
    """
    Checks if a torrent contains at least one large, valid media file.
    Returns an error message string if invalid, otherwise None.
    """
    files = ti.files()
    if files.num_files() == 0:
        return "the torrent contains no files."

    # Find all files larger than a 10MB threshold
    large_files = [
        files.file_path(i)
        for i in range(files.num_files())
        if files.file_size(i) > 10 * 1024 * 1024
    ]

    if large_files:
        # Check if at least one large file has a valid extension
        if any(os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS for f in large_files):
            return None  # Valid torrent
        # No valid large files found, report the largest file's extension
        largest_idx = max(range(files.num_files()), key=files.file_size)
        largest_ext = os.path.splitext(files.file_path(largest_idx))[1]
        return (
            "contains an unsupported file type "
            f"('{largest_ext}'). I can only download .mkv and .mp4 files."
        )

    # No large files, check the single largest file in the torrent
    largest_idx = max(range(files.num_files()), key=files.file_size)
    largest_file_path = files.file_path(largest_idx)
    ext = os.path.splitext(largest_file_path)[1]
    if ext.lower() not in ALLOWED_EXTENSIONS:
        return (
            f"contains an unsupported file type ('{ext}'). I can only download .mkv and .mp4 files."
        )

    return None


async def validate_and_enrich_torrent(
    ti: lt.torrent_info,  # type: ignore
    progress_message: Message,  # type: ignore
) -> tuple[str | None, dict[str, Any] | None]:
    """
    Validates a torrent_info object against size and file type rules,
    and enriches its metadata (e.g., fetching TV episode titles).

    Returns:
        A tuple of (error_message, parsed_info). If validation fails,
        error_message will be a string. Otherwise, it will be None and
        parsed_info will be populated.
    """
    # 1. Validate size
    if ti.total_size() > MAX_TORRENT_SIZE_BYTES:
        # Escape the dynamic parts of the string that might contain special characters.
        torrent_size_str = escape_markdown(format_bytes(ti.total_size()), version=2)
        size_limit_str = escape_markdown(str(MAX_TORRENT_SIZE_GIB), version=2)

        # Construct the final message using the escaped parts, also escaping the final period.
        error_msg = (
            f"This torrent is *{torrent_size_str}*, which is larger than the "
            f"*{size_limit_str} GiB* limit\\."
        )

        await safe_edit_message(
            progress_message,
            text=f"❌ *Size Limit Exceeded*\n\n{error_msg}",
            parse_mode="MarkdownV2",
        )
        return "Size limit exceeded", None

    # 2. Validate file types
    validation_error = validate_torrent_files(ti)
    if validation_error:
        error_msg = f"This torrent {escape_markdown(validation_error)}"
        await safe_edit_message(
            progress_message,
            text=f"❌ *Unsupported File Type*\n\n{error_msg}",
            parse_mode="MarkdownV2",
        )
        return "Unsupported file type", None

    # 3. Parse name and enrich if it's a TV show
    parsed_info = parse_torrent_name(ti.name())
    if parsed_info.get("type") == "tv":
        # For season packs, skip per-episode Wikipedia lookups
        if parsed_info.get("is_season_pack"):
            pass
        else:
            await safe_edit_message(
                progress_message,
                text="📺 TV show detected. Searching Wikipedia for episode title...",
            )

            (
                episode_title,
                corrected_show_title,
            ) = await fetch_episode_title_from_wikipedia(
                show_title=parsed_info["title"],
                season=parsed_info["season"],
                episode=parsed_info["episode"],
            )
            parsed_info["episode_title"] = episode_title
            if corrected_show_title:
                logger.info(
                    "Corrected TV show title from '%s' to '%s'.",
                    parsed_info["title"],
                    corrected_show_title,
                )
                parsed_info["title"] = corrected_show_title

    return None, parsed_info
