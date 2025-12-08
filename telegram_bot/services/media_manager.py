# telegram_bot/services/media_manager.py

import asyncio
import os
import shutil
from typing import Any

import libtorrent as lt
from plexapi.exceptions import NotFound, Unauthorized
from plexapi.server import PlexServer
from telegram import Message
from telegram.helpers import escape_markdown

from ..config import (
    ALLOWED_EXTENSIONS,
    MAX_TORRENT_SIZE_BYTES,
    MAX_TORRENT_SIZE_GB,
    logger,
)
from ..utils import format_bytes, safe_edit_message, parse_torrent_name
from .scrapers import fetch_episode_title_from_wikipedia


def generate_plex_filename(parsed_info: dict[str, Any], original_extension: str) -> str:
    """Generates a clean, Plex-friendly filename from the parsed info."""
    title = parsed_info.get("title", "Unknown Title")
    invalid_chars = r'<>:"/\|?*'
    safe_title = "".join(c for c in title if c not in invalid_chars)

    if parsed_info.get("type") == "movie":
        year = parsed_info.get("year", "Unknown Year")
        return f"{safe_title} ({year}){original_extension}"

    elif parsed_info.get("type") == "tv":
        season = parsed_info.get("season", 0)
        episode = parsed_info.get("episode", 0)
        episode_title = parsed_info.get("episode_title")
        safe_episode_title = ""
        if episode_title:
            safe_episode_title = " - " + "".join(
                c for c in episode_title if c not in invalid_chars
            )
        return f"s{season:02d}e{episode:02d}{safe_episode_title}{original_extension}"

    else:  # Fallback for 'unknown' type
        return f"{safe_title}{original_extension}"


def get_dominant_file_type(files: lt.file_storage) -> str:  # type: ignore
    """Determines the file extension of the largest file in a torrent."""
    if files.num_files() == 0:
        return "N/A"
    largest_file_index = max(range(files.num_files()), key=files.file_size)
    largest_filename = files.file_path(largest_file_index)
    _, extension = os.path.splitext(largest_filename)
    return extension[1:].upper() if extension else "N/A"


def parse_resolution_from_name(name: str) -> str:
    """Parses a torrent name to find the video resolution."""
    name_lower = name.lower()
    if any(res in name_lower for res in ["2160p", "4k", "uhd"]):
        return "4K"
    if "1080p" in name_lower:
        return "1080p"
    if "720p" in name_lower:
        return "720p"
    if any(res in name_lower for res in ["480p", "sd", "dvdrip"]):
        return "SD"
    return "N/A"


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
        if any(
            os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS for f in large_files
        ):
            return None  # Valid torrent
        else:
            # No valid large files found, report the largest file's extension
            largest_idx = max(range(files.num_files()), key=files.file_size)
            largest_ext = os.path.splitext(files.file_path(largest_idx))[1]
            return f"contains an unsupported file type ('{largest_ext}'). I can only download .mkv and .mp4 files."
    else:
        # No large files, check the single largest file in the torrent
        largest_idx = max(range(files.num_files()), key=files.file_size)
        largest_file_path = files.file_path(largest_idx)
        ext = os.path.splitext(largest_file_path)[1]
        if ext.lower() not in ALLOWED_EXTENSIONS:
            return f"contains an unsupported file type ('{ext}'). I can only download .mkv and .mp4 files."

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
        size_limit_str = escape_markdown(str(MAX_TORRENT_SIZE_GB), version=2)

        # Construct the final message using the escaped parts, also escaping the final period.
        error_msg = (
            f"This torrent is *{torrent_size_str}*, which is larger than the "
            f"*{size_limit_str} GB* limit\\."
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
                    f"Corrected TV show title from '{parsed_info['title']}' to '{corrected_show_title}'."
                )
                parsed_info["title"] = corrected_show_title

    return None, parsed_info


async def handle_successful_download(
    ti: lt.torrent_info,  # type: ignore
    parsed_info: dict[str, Any],
    initial_download_path: str,
    save_paths: dict[str, str],
    plex_config: dict[str, str] | None,
    *,
    defer_scan: bool = False,
) -> str:
    """
    Moves completed downloads to the correct media directory, renames them
    for Plex, and triggers a library scan.
    """
    try:
        files = ti.files()

        if parsed_info.get("is_season_pack"):
            processed = 0
            for i in range(files.num_files()):
                path_in_torrent = files.file_path(i)
                _, ext = os.path.splitext(path_in_torrent)
                if ext.lower() not in ALLOWED_EXTENSIONS:
                    continue

                parsed_info_for_file = parse_torrent_name(
                    os.path.basename(path_in_torrent)
                )
                parsed_info_for_file["title"] = parsed_info.get("title")
                parsed_info_for_file["season"] = parsed_info.get("season")
                parsed_info_for_file["type"] = "tv"

                # 1. Extract the values into local variables for type narrowing.
                show_title = parsed_info_for_file.get("title")
                season_num = parsed_info_for_file.get("season")
                episode_num = parsed_info_for_file.get("episode")

                # 2. Use isinstance to validate the types. This is what the IDE needs.
                if (
                    not isinstance(show_title, str)
                    or not isinstance(season_num, int)
                    or not isinstance(episode_num, int)
                ):
                    # If any crucial info is missing, skip this file entirely.
                    continue

                # 3. Call the function with the validated local variables.
                # The IDE now knows these variables cannot be None.
                (
                    episode_title,
                    corrected_show_title,
                ) = await fetch_episode_title_from_wikipedia(
                    show_title=show_title,
                    season=season_num,
                    episode=episode_num,
                )

                # 4. Safely update the dictionary with the results.
                parsed_info_for_file["episode_title"] = episode_title
                if corrected_show_title:
                    parsed_info_for_file["title"] = corrected_show_title

                destination_directory = _get_final_destination_path(
                    parsed_info_for_file, save_paths
                )
                os.makedirs(destination_directory, exist_ok=True)

                final_filename = generate_plex_filename(parsed_info_for_file, ext)
                current_path = os.path.join(initial_download_path, path_in_torrent)
                new_path = os.path.join(destination_directory, final_filename)
                logger.info(f"Moving file from '{current_path}' to '{new_path}'")
                await asyncio.to_thread(shutil.move, current_path, new_path)

                processed += 1

            # Trigger a single Plex scan after all files are moved
            scan_status_message = await _trigger_plex_scan("tv", plex_config)
            # Telegram MarkdownV2 requires escaping '.' characters
            return (
                "✅ *Success\\!*\n"
                f"Processed and moved {processed} episodes from the season pack\\."
                f"{scan_status_message}"
            )

        # --- Single file torrent processing ---
        target_file_in_torrent = None
        original_extension = ".mkv"  # Default

        for i in range(files.num_files()):
            path_in_torrent = files.file_path(i)
            _, ext = os.path.splitext(path_in_torrent)
            if ext.lower() in ALLOWED_EXTENSIONS:
                target_file_in_torrent = path_in_torrent
                original_extension = ext
                break

        if not target_file_in_torrent:
            raise FileNotFoundError(
                "No valid media file (.mkv, .mp4) found in the completed torrent."
            )

        current_path = os.path.join(initial_download_path, target_file_in_torrent)
        final_filename = generate_plex_filename(parsed_info, original_extension)
        destination_directory = _get_final_destination_path(parsed_info, save_paths)
        os.makedirs(destination_directory, exist_ok=True)
        new_path = os.path.join(destination_directory, final_filename)

        logger.info(f"Moving file from '{current_path}' to '{new_path}'")
        await asyncio.to_thread(shutil.move, current_path, new_path)

        # Optionally defer Plex scan when part of a multi-episode batch
        if defer_scan:
            scan_status_message = ""
        else:
            scan_status_message = await _trigger_plex_scan(
                parsed_info.get("type"), plex_config
            )

    except Exception as e:
        logger.error(f"Post-processing failed: {e}", exc_info=True)
        return f"❌ *Post-Processing Error*\nDownload completed but failed during file handling.\n\n`{escape_markdown(str(e))}`"

    clean_name = parsed_info.get("title", "Download")
    return (
        f"✅ *Success\\!*\n"
        f"Renamed and moved to Plex Server:\n"
        f"`{escape_markdown(clean_name)}`"
        f"{scan_status_message}"
    )


def _get_final_destination_path(
    parsed_info: dict[str, Any], save_paths: dict[str, str]
) -> str:
    """Determines the final directory for the downloaded media."""
    media_type = parsed_info.get("type")

    if media_type == "movie":
        return save_paths.get("movies", save_paths["default"])

    if media_type == "tv":
        root_path = save_paths.get("tv_shows", save_paths["default"])
        show_title = parsed_info.get("title", "Unknown Show")
        season_num = parsed_info.get("season", 0)

        # Sanitize show title for directory name
        invalid_chars = r'<>:"/\|?*'
        safe_show_title = "".join(c for c in show_title if c not in invalid_chars)

        return os.path.join(root_path, safe_show_title, f"Season {season_num:02d}")

    return save_paths["default"]


async def _trigger_plex_scan(
    media_type: str | None, plex_config: dict[str, str] | None
) -> str:
    """Triggers a Plex library scan for the relevant library."""
    if not plex_config:
        return ""

    # --- Refactored Guard Clause: Ensure media_type is a valid string before use ---
    if not media_type:
        return ""

    library_map = {"movie": "Movies", "tv": "TV Shows"}
    # `media_type` is now guaranteed to be a string, resolving the type error.
    library_name = library_map.get(media_type)

    if not library_name:
        # This handles cases where media_type is 'unknown' or another unexpected value.
        return ""

    logger.info(f"Attempting to scan '{library_name}' library in Plex...")
    try:
        # Run blocking PlexAPI calls in a separate thread
        plex = await asyncio.to_thread(
            PlexServer, plex_config["url"], plex_config["token"]
        )
        target_library = await asyncio.to_thread(plex.library.section, library_name)
        await asyncio.to_thread(target_library.update)

        logger.info(f"Successfully triggered Plex scan for '{library_name}'.")
        return f"\n\nPlex scan for the `{escape_markdown(library_name)}` library has been initiated\\."

    except (Unauthorized, NotFound, Exception) as e:
        error_map = {
            Unauthorized: "Plex token is invalid.",
            NotFound: f"Plex library '{library_name}' not found.",
        }
        reason = error_map.get(type(e), f"An unexpected error occurred: {e}")
        logger.error(f"Plex scan failed: {reason}")
        return "\n\n*Plex Error:* Could not trigger scan\\."
