# telegram_bot/services/download_manager/download_core.py

import asyncio
import time
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import libtorrent as lt

from telegram_bot.config import logger
from telegram_bot.domain.types import DownloadData

from .adapters import fetch_url


async def download_with_progress(
    source: str,
    save_path: str,
    status_callback: Callable[[lt.torrent_status], Coroutine[Any, Any, None]],  # type: ignore
    bot_data: dict[str, Any],
    download_data: DownloadData,
    info_url: str | None = None,
) -> tuple[bool, lt.torrent_info | None]:  # type: ignore
    """
    Core libtorrent download logic. Handles magnet links, torrent URLs, and local files.
    Returns (success_status, torrent_info_object).
    """
    if info_url:
        logger.info(f"[DOWNLOAD] Torrent info page: {info_url}")
    else:
        logger.info("[DOWNLOAD] Torrent info page: Not available")

    ses = bot_data["TORRENT_SESSION"]
    params: dict[str, Any] = {}

    try:
        # --- LOGIC TO HANDLE DIFFERENT SOURCE TYPES ---
        if source.startswith("magnet:"):
            logger.info("Source is a magnet link.")
            # libtorrent handles magnet URI parsing directly.
            params = lt.parse_magnet_uri(source)  # type: ignore
            params.save_path = save_path  # type: ignore
            params.storage_mode = lt.storage_mode_t.storage_mode_sparse  # type: ignore

        elif source.startswith(("http://", "https://")):
            logger.info(f"Source is a URL. Downloading .torrent file from: {source}")
            # Source is a URL, so we must download the .torrent content first.
            response = await fetch_url(source, follow_redirects=True, timeout=30)
            response.raise_for_status()

            # Create the torrent_info object from the downloaded content.
            ti = lt.torrent_info(response.content)  # type: ignore
            params = {
                "save_path": save_path,
                "storage_mode": lt.storage_mode_t.storage_mode_sparse,  # type: ignore
                "ti": ti,
            }

        else:
            logger.info(f"Source is a local file path: {source}")
            # Source is assumed to be a local file path.
            params = {
                "save_path": save_path,
                "storage_mode": lt.storage_mode_t.storage_mode_sparse,  # type: ignore
                "ti": lt.torrent_info(source),  # type: ignore
            }

    except httpx.HTTPError as e:
        logger.error(f"Failed to retrieve .torrent file from URL '{source}': {e}")
        return False, None
    except RuntimeError as e:
        # This catches errors from lt.torrent_info(), e.g., "not a valid torrent".
        logger.error(f"Libtorrent failed to parse source '{source}': {e}")
        return False, None
    except Exception as e:
        logger.error(
            f"An unexpected error occurred while preparing download params for '{source}': {e}",
            exc_info=True,
        )
        return False, None

    # --- ADD TORRENT TO SESSION AND START DOWNLOAD LOOP ---
    handle = ses.add_torrent(params)
    download_data["handle"] = handle  # Store handle for pausing/resuming

    start_time = time.monotonic()
    while not handle.status().is_seeding:
        if bot_data.get("is_shutting_down") or download_data.get("requeued"):
            raise asyncio.CancelledError("Shutdown or requeue initiated.")

        # Handle pausing. We still emit progress updates so the user interface
        # can reflect the paused state and show a toggle button to resume.
        if download_data.get("is_paused"):
            handle.pause()
            await status_callback(handle.status())  # Immediate paused update
            while download_data.get("is_paused"):
                if bot_data.get("is_shutting_down"):
                    raise asyncio.CancelledError("Shutdown initiated.")
                await asyncio.sleep(1)
                await status_callback(handle.status())
            handle.resume()

        status = handle.status()
        await status_callback(status)

        # Timeout logic for stalled metadata fetch (avoid libtorrent enum reference)
        if (not getattr(status, "has_metadata", False)) and (time.monotonic() - start_time > 60):
            logger.warning(f"Metadata download timed out for {handle.name()}")
            raise TimeoutError("metadata_timeout")

        await asyncio.sleep(1)

    # Final "100%" update
    await status_callback(handle.status())
    logger.info(f"Download completed for: {handle.name()}")
    return True, handle.torrent_file()
