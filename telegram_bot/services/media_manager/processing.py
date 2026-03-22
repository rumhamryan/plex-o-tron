# telegram_bot/services/media_manager/processing.py

import asyncio
import os
from typing import Any

import libtorrent as lt
from telegram.helpers import escape_markdown

from telegram_bot.config import ALLOWED_EXTENSIONS, logger
from telegram_bot.domain.types import PostProcessingResult
from telegram_bot.services.scraping_service import fetch_episode_title_from_wikipedia
from telegram_bot.ui.messages import format_media_summary
from telegram_bot.utils import format_bytes, parse_torrent_name

from . import adapters
from .naming import _build_media_display_name, generate_plex_filename
from .paths import _get_disk_usage_percent, _get_final_destination_path, _get_path_size_bytes
from .plex_scan import _trigger_plex_scan
from .validation import select_primary_media_file

LOW_FREE_SPACE_USAGE_THRESHOLD_PERCENT = 85


def _coerce_year(raw_year: Any) -> int | None:
    if isinstance(raw_year, int):
        return raw_year
    if isinstance(raw_year, str) and raw_year.isdigit():
        return int(raw_year)
    return None


async def handle_successful_download(
    ti: lt.torrent_info,  # type: ignore
    parsed_info: dict[str, Any],
    initial_download_path: str,
    save_paths: dict[str, str],
    plex_config: dict[str, str] | None,
    *,
    defer_scan: bool = False,
) -> PostProcessingResult:
    """
    Moves completed downloads to the correct media directory, renames them
    for Plex, and triggers a library scan.
    """
    summary_destination: str | None = None
    summary_size_bytes: int | None = None
    season_pack_processed = 0
    scan_status_message = ""
    summary_title = _build_media_display_name(parsed_info)
    year_value = _coerce_year(parsed_info.get("year"))
    is_season_pack = bool(parsed_info.get("is_season_pack"))

    try:
        files = ti.files()

        if is_season_pack:
            processed = 0
            total_size_bytes = 0
            season_destination: str | None = None

            for i in range(files.num_files()):
                path_in_torrent = files.file_path(i)
                _, ext = os.path.splitext(path_in_torrent)
                if ext.lower() not in ALLOWED_EXTENSIONS:
                    continue

                parsed_info_for_file = parse_torrent_name(os.path.basename(path_in_torrent))
                parsed_info_for_file["title"] = parsed_info.get("title")
                parsed_info_for_file["season"] = parsed_info.get("season")
                parsed_info_for_file["type"] = "tv"

                show_title = parsed_info_for_file.get("title")
                season_num = parsed_info_for_file.get("season")
                episode_num = parsed_info_for_file.get("episode")

                if (
                    not isinstance(show_title, str)
                    or not isinstance(season_num, int)
                    or not isinstance(episode_num, int)
                ):
                    continue

                (
                    episode_title,
                    corrected_show_title,
                ) = await fetch_episode_title_from_wikipedia(
                    show_title=show_title,
                    season=season_num,
                    episode=episode_num,
                )

                parsed_info_for_file["episode_title"] = episode_title
                if corrected_show_title:
                    parsed_info_for_file["title"] = corrected_show_title

                destination_directory = _get_final_destination_path(
                    parsed_info_for_file, save_paths
                )
                adapters.ensure_dir(destination_directory)

                final_filename = generate_plex_filename(parsed_info_for_file, ext)
                current_path = os.path.join(initial_download_path, path_in_torrent)
                new_path = os.path.join(destination_directory, final_filename)
                logger.info("Moving file from '%s' to '%s'", current_path, new_path)
                await asyncio.to_thread(adapters.move_file, current_path, new_path)

                processed += 1
                if season_destination is None:
                    season_destination = destination_directory

                moved_size = _get_path_size_bytes(new_path)
                if moved_size is not None:
                    total_size_bytes += moved_size

            scan_status_message = await _trigger_plex_scan("tv", plex_config)
            summary_destination = season_destination
            summary_size_bytes = total_size_bytes if total_size_bytes > 0 else None
            season_pack_processed = processed

        else:
            selected_media = select_primary_media_file(files)
            if not selected_media:
                raise FileNotFoundError(
                    "No valid media file (.mkv, .mp4) found in the completed torrent."
                )

            target_file_in_torrent, original_extension, selected_size_bytes = selected_media
            current_path = os.path.join(initial_download_path, target_file_in_torrent)
            final_filename = generate_plex_filename(parsed_info, original_extension)
            destination_directory = _get_final_destination_path(parsed_info, save_paths)
            adapters.ensure_dir(destination_directory)
            new_path = os.path.join(destination_directory, final_filename)

            logger.info(
                "Selected primary media file '%s' (%s) from torrent '%s'.",
                target_file_in_torrent,
                format_bytes(selected_size_bytes),
                ti.name(),
            )
            logger.info("Moving file from '%s' to '%s'", current_path, new_path)
            await asyncio.to_thread(adapters.move_file, current_path, new_path)

            summary_destination = new_path
            summary_size_bytes = _get_path_size_bytes(new_path)

            if defer_scan:
                scan_status_message = ""
            else:
                scan_status_message = await _trigger_plex_scan(parsed_info.get("type"), plex_config)

    except Exception as e:
        logger.error("Post-processing failed: %s", e, exc_info=True)
        return {
            "succeeded": False,
            "final_message": (
                "❌ *Post-Processing Error*\n"
                "Download completed but failed during file handling\\.\n\n"
                f"`{escape_markdown(str(e))}`"
            ),
            "destination_path": summary_destination,
            "media_type": parsed_info.get("type"),
            "title": summary_title,
            "year": year_value,
        }

    size_label = format_bytes(summary_size_bytes) if summary_size_bytes is not None else None
    destination_label = summary_destination.replace("\\", "/") if summary_destination else None
    disk_usage_percent = (
        _get_disk_usage_percent(summary_destination) if summary_destination else None
    )
    highlight_disk_usage = (
        disk_usage_percent is not None
        and disk_usage_percent >= LOW_FREE_SPACE_USAGE_THRESHOLD_PERCENT
    )

    media_type = parsed_info.get("type")
    title_icon = "🎬" if media_type == "movie" else "📺" if media_type == "tv" else None
    summary_text = format_media_summary(
        prefix="✅ *Successfully Added to Plex*",
        title=summary_title,
        size_label=size_label,
        destination_label=destination_label,
        disk_usage_percent=disk_usage_percent,
        highlight_disk_usage=highlight_disk_usage,
        title_icon=title_icon,
        size_icon="📦",
        destination_icon="📁",
        disk_usage_icon="⚠️" if highlight_disk_usage else "💽",
    )

    season_note = ""
    if is_season_pack:
        processed_label = season_pack_processed or 0
        season_note = f"\nProcessed and moved {processed_label} episodes from the season pack\\."

    return {
        "succeeded": True,
        "final_message": f"{summary_text}{season_note}{scan_status_message}",
        "destination_path": summary_destination,
        "media_type": parsed_info.get("type"),
        "title": summary_title,
        "year": year_value,
    }
