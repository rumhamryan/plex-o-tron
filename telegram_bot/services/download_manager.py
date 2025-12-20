# telegram_bot/services/download_manager.py

import asyncio
import os
import time
from typing import Any
from collections.abc import Callable, Coroutine

import httpx
import libtorrent as lt
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.helpers import escape_markdown

from ..config import logger, PERSISTENCE_FILE
from ..state import save_state
from ..utils import safe_edit_message, sanitize_collection_name
from .media_manager import handle_successful_download, _trigger_plex_scan
from .plex_service import ensure_collection_contains_movies
from ..workflows import finalize_movie_collection


class ProgressReporter:
    """A class to handle reporting download progress to the user via Telegram."""

    def __init__(
        self,
        application: Application,
        chat_id: int,
        message_id: int,
        parsed_info: dict[str, Any],
        clean_name: str,
        download_data: dict[str, Any],
    ):
        self.application = application
        self.chat_id = chat_id
        self.message_id = message_id
        self.parsed_info = parsed_info
        self.clean_name = clean_name
        self.download_data = download_data
        self.last_update_time: float = 0

    async def report(self, status: lt.torrent_status) -> None:  # type: ignore
        """Formats and sends a progress update message."""
        async with self.download_data["lock"]:
            if self.download_data.get("cancellation_pending"):
                return

            current_time = time.monotonic()
            if (
                current_time - self.last_update_time < 2
            ):  # Reduced from 5 for more frequent updates
                return
            self.last_update_time = current_time

            progress_percent = status.progress * 100
            is_paused = self.download_data.get("is_paused", False)
            # When a download is paused libtorrent may still report the last
            # observed download rate. We override the speed to reflect the
            # effective transfer rate during a pause.
            speed_mbps = 0.0 if is_paused else status.download_rate / 1024 / 1024

            # --- CORRECTED STRING PREPARATION WITH version=2 ---
            progress_str = escape_markdown(f"{progress_percent:.2f}", version=2)
            speed_str = escape_markdown(f"{speed_mbps:.2f}", version=2)

            # --- Build the message content ---
            if self.parsed_info.get("type") == "tv":
                s = int(self.parsed_info.get("season", 0) or 0)
                title_str = escape_markdown(
                    self.parsed_info.get("title", ""), version=2
                )

                # Season pack handling: show single-line "<Title> Season 01"
                if self.parsed_info.get("is_season_pack"):
                    name_str = f"`{title_str} Season {s:02d}`"
                else:
                    e = int(self.parsed_info.get("episode", 0) or 0)
                    ep_title = self.parsed_info.get("episode_title", "")
                    episode_details_str = escape_markdown(
                        f"S{s:02d}E{e:02d} - {ep_title}", version=2
                    )
                    name_str = f"`{title_str}`\n`{episode_details_str}`"
            else:
                name_str = f"`{escape_markdown(self.clean_name, version=2)}`"

            header_str = "⏸️ *Paused:*" if is_paused else "⬇️ *Downloading:*"
            state_str = (
                "*paused*"
                if is_paused
                else escape_markdown(status.state.name, version=2)
            )

            message_text = (
                f"{header_str}\n{name_str}\n"
                f"*Progress:* {progress_str}%\n"
                f"*State:* {state_str}\n"
                f"*Peers:* {status.num_peers}\n"
                f"*Speed:* {speed_str} MB/s"
            )

            # Use a single toggle button for both pause and resume actions.
            # Build control row and conditionally add "Stop" if there is a queue
            controls_row = []
            if is_paused:
                controls_row.append(
                    InlineKeyboardButton("▶️ Resume", callback_data="pause_resume")
                )
            else:
                controls_row.append(
                    InlineKeyboardButton("⏸️ Pause", callback_data="pause_resume")
                )
            controls_row.append(
                InlineKeyboardButton("⏹️ Cancel", callback_data="cancel_download")
            )

            # If user has at least 1 queued download, expose "Cancel All"
            try:
                dq = self.application.bot_data.get("download_queues", {})
                if dq.get(str(self.chat_id)):
                    controls_row.append(
                        InlineKeyboardButton("🛑 Stop", callback_data="cancel_all")
                    )
            except Exception:
                pass

            reply_markup = InlineKeyboardMarkup([controls_row])

            # --- FIX: Add a try/except block to prevent UI errors from crashing the download ---
            try:
                await safe_edit_message(
                    self.application.bot,
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=message_text,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=reply_markup,
                )
            except (TimedOut, NetworkError) as e:
                logger.warning(
                    f"Failed to send progress update due to a network error: {e}. "
                    "The download will continue in the background."
                )


async def download_with_progress(
    source: str,
    save_path: str,
    status_callback: Callable[[lt.torrent_status], Coroutine[Any, Any, None]],  # type: ignore
    bot_data: dict,
    download_data: dict,
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
            async with httpx.AsyncClient() as client:
                response = await client.get(source, follow_redirects=True, timeout=30)
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
        if (not getattr(status, "has_metadata", False)) and (
            time.monotonic() - start_time > 60
        ):
            logger.warning(f"Metadata download timed out for {handle.name()}")
            raise TimeoutError("metadata_timeout")

        await asyncio.sleep(1)

    # Final "100%" update
    await status_callback(handle.status())
    logger.info(f"Download completed for: {handle.name()}")
    return True, handle.torrent_file()


async def download_task_wrapper(download_data: dict, application: Application):
    """
    Wraps the entire download lifecycle for a single torrent.
    Handles success, failure, cancellation, and requeueing.
    """
    source_dict = download_data["source_dict"]
    chat_id = download_data["chat_id"]
    message_id = download_data["message_id"]
    initial_save_path = download_data["save_path"]
    clean_name = source_dict.get("clean_name", "Download")
    message_text = "No message"

    reporter = ProgressReporter(
        application,
        chat_id,
        message_id,
        source_dict.get("parsed_info", {}),
        clean_name,
        download_data,
    )

    try:
        success, ti = await download_with_progress(
            source=source_dict["value"],
            save_path=initial_save_path,
            status_callback=reporter.report,
            bot_data=application.bot_data,
            download_data=download_data,
            info_url=source_dict.get("info_url"),
        )

        if success and ti:
            # Inject sanitized collection name if part of a collection batch
            batch_id = source_dict.get("batch_id")
            if batch_id:
                batches = application.bot_data.get("DOWNLOAD_BATCHES", {})
                batch = batches.get(batch_id)
                if batch and batch.get("collection"):
                    raw_name = batch["collection"].get("name")
                    if raw_name:
                        source_dict.setdefault("parsed_info", {})["collection_name"] = (
                            sanitize_collection_name(raw_name)
                        )

            # Defer Plex scan if this download is part of a batch of episodes
            defer_scan = bool(source_dict.get("batch_id"))
            message_text = await handle_successful_download(
                ti=ti,
                parsed_info=source_dict.get("parsed_info", {}),
                initial_download_path=initial_save_path,
                save_paths=application.bot_data.get("SAVE_PATHS", {}),
                plex_config=application.bot_data.get("PLEX_CONFIG"),
                defer_scan=defer_scan,
            )
            # Now that the media file has been moved, we can safely delete the originals.
            logger.info(
                f"Removing torrent and deleting original files for: {clean_name}"
            )
            ses = application.bot_data["TORRENT_SESSION"]
            handle = download_data.get("handle")
            if handle and handle.is_valid():
                # This flag tells libtorrent to remove the torrent and delete all its files.
                ses.remove_torrent(handle, lt.session.delete_files)  # type: ignore
            # If part of a season batch, update counters and maybe trigger a single scan.
            message_text = await _update_batch_and_maybe_scan(
                application,
                source_dict,
                message_text,
                source_dict.get("parsed_info", {}),
            )

        else:
            if not download_data.get("requeued"):
                message_text = "❌ *Download Failed*\nAn unknown error occurred in the download manager\\."

    except TimeoutError as e:
        if str(e) == "metadata_timeout":
            logger.warning(f"Metadata timeout for '{clean_name}'. Requeueing.")
            download_data["requeued"] = True
            download_data["metadata_timeout_occurred"] = True
            message_text = f"⚠️ *Metadata Timeout*\nRetrying download for:\n`{escape_markdown(clean_name)}`"

            # Clean up the stuck torrent
            ses = application.bot_data["TORRENT_SESSION"]
            handle = download_data.get("handle")
            if handle and handle.is_valid():
                ses.remove_torrent(handle, lt.session.delete_files)
        else:
            logger.error(
                f"Unexpected TimeoutError in download task for '{clean_name}': {e}",
                exc_info=True,
            )
            message_text = f"❌ *Error*\nAn unexpected timeout occurred:\n`{escape_markdown(str(e))}`"

    except asyncio.CancelledError:
        if download_data.get("requeued"):
            logger.info(f"Task for '{clean_name}' cancelled for requeue.")
        elif application.bot_data.get("is_shutting_down"):
            logger.info(f"Task for '{clean_name}' paused for shutdown.")
            raise  # Re-raise to be handled by post_shutdown

        else:
            # 1. Log what you are about to do.
            logger.info(
                f"Cancellation cleanup: Removing torrent and deleting files for '{clean_name}'."
            )

            # 2. Get the session and the handle.
            ses = application.bot_data["TORRENT_SESSION"]
            handle = download_data.get("handle")

            # 3. Check if the handle is valid before using it.
            if handle and handle.is_valid():
                # 4. Use the key libtorrent feature to remove the torrent and its data.
                ses.remove_torrent(handle, lt.session.delete_files)  # type: ignore

            message_text = f"⏹️ *Cancelled*\nDownload has been stopped for:\n`{escape_markdown(clean_name)}`"

    except Exception as e:
        logger.error(
            f"Unexpected exception in download task for '{clean_name}': {e}",
            exc_info=True,
        )
        message_text = (
            f"❌ *Error*\nAn unexpected error occurred:\n`{escape_markdown(str(e))}`"
        )

    finally:
        # This block handles cleanup and queue processing
        if download_data.get("requeued"):
            await _requeue_download(download_data, application)
        elif not application.bot_data.get("is_shutting_down"):
            await _finalize_download(
                application,
                chat_id,
                message_id,
                message_text,
                source_dict,
                initial_save_path,
            )
            await process_queue_for_user(chat_id, application)


async def _update_batch_and_maybe_scan(
    application: Application,
    source_dict: dict[str, Any],
    message_text: str,
    parsed_info: dict[str, Any],
) -> str:
    """Updates season-batch counters and triggers a single Plex scan on completion.

    Returns the (possibly) augmented message_text with batch-complete info lines.
    """
    try:
        batch_id = source_dict.get("batch_id")
        if not batch_id:
            return message_text

        batches: dict[str, Any] = application.bot_data.setdefault(
            "DOWNLOAD_BATCHES", {}
        )
        batch = batches.get(batch_id)
        if not isinstance(batch, dict):
            return message_text

        summaries = batch.setdefault("summaries", [])
        summaries.append(message_text)

        batch["done"] = int(batch.get("done", 0)) + 1
        total = int(batch.get("total", 0))
        if batch["done"] < total or batch.get("scanned"):
            return message_text

        # Mark scanned before awaiting network, to avoid double-scans if re-entered
        batch["scanned"] = True

        media_type = batch.get("media_type", "tv")
        plex_config = application.bot_data.get("PLEX_CONFIG")

        if media_type == "tv":
            title = str(parsed_info.get("title", "This Show"))
            season = int(parsed_info.get("season", 0) or 0)
            title_md = escape_markdown(title, version=2)
            info_line = (
                "\n\n*Batch Complete*\n"
                f"Season {season:02d} of *{title_md}* finalized: {total}/{total} episodes\\.\n"
                "Starting Plex scan…"
            )
            combined_message = message_text
        else:
            collection_meta = batch.get("collection") or {}
            collection_name = collection_meta.get("name") or "this collection"
            collection_md = escape_markdown(str(collection_name), version=2)
            info_line = (
                "\n\n*Collection Complete*\n"
                f"Queued {total}/{total} movies for *{collection_md}*\\.\n"
                "Starting Plex scan…"
            )
            combined_message = "\n\n".join(summaries) if summaries else message_text

        if media_type == "movie":
            collection_meta = batch.get("collection") or {}
            await finalize_movie_collection(application, collection_meta)

        scan_msg = await _trigger_plex_scan(media_type, plex_config)

        if media_type == "movie":
            # Wait 120s for Plex scan to likely complete so items are indexable
            if scan_msg:
                logger.info("Waiting 120 seconds for Plex scan to index new movies...")
                await asyncio.sleep(120)

            collection_meta = batch.get("collection") or {}
            raw_name = str(collection_meta.get("name") or "").strip()
            collection_name = sanitize_collection_name(raw_name)
            added = await ensure_collection_contains_movies(
                plex_config,
                collection_name,
                collection_meta.get("movies") or [],
            )
            if added:
                info_line += f"\nAdded {len(added)} film{'s' if len(added) != 1 else ''} to the Plex collection\\."

        batch.pop("summaries", None)
        return f"{combined_message}{info_line}{scan_msg}"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Batch tracking error: {e}")
        return message_text


async def _requeue_download(download_data: dict, application: Application):
    """Moves a paused or interrupted download to the back of the queue."""
    chat_id = download_data["chat_id"]
    chat_id_str = str(chat_id)
    logger.info(f"Requeueing download for user {chat_id_str}.")

    active_downloads = application.bot_data.get("active_downloads", {})
    download_queues = application.bot_data.get("download_queues", {})

    # Clean up data for requeueing but keep pause state
    download_data.pop("task", None)
    download_data.pop("handle", None)
    download_data.pop("requeued", None)

    # Check if this was a metadata timeout
    is_metadata_timeout = download_data.pop("metadata_timeout_occurred", False)

    # If it was a timeout, we want to auto-retry (unpaused).
    # Otherwise (e.g., interrupted by higher priority), we default to paused state
    # so it doesn't auto-resume unexpectedly unless logic elsewhere dictates.
    if is_metadata_timeout:
        download_data["is_paused"] = False
    else:
        download_data["is_paused"] = True  # Ensure it's marked as paused

    if chat_id_str not in download_queues:
        download_queues[chat_id_str] = []
    download_queues[chat_id_str].append(download_data)

    if chat_id_str in active_downloads:
        del active_downloads[chat_id_str]

    # If this was a metadata timeout and it's the ONLY item in the queue,
    # wait 60 seconds before letting process_queue_for_user pick it up again.
    if is_metadata_timeout and len(download_queues[chat_id_str]) == 1:
        logger.info(
            "Metadata timeout on the only queued item. Waiting 60s before retry."
        )
        await asyncio.sleep(60)

    save_state(PERSISTENCE_FILE, active_downloads, download_queues)
    await process_queue_for_user(chat_id, application)  # Start next in queue


async def _finalize_download(
    application, chat_id, message_id, message_text, source_dict, save_path
):
    """Handles final message sending and resource cleanup."""
    try:
        await safe_edit_message(
            application.bot,
            chat_id=chat_id,
            message_id=message_id,
            text=message_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=None,
        )
    except (BadRequest, NetworkError) as e:
        logger.warning(f"Could not send final status message: {e}")

    cleanup_download_resources(
        application, chat_id, source_dict["type"], source_dict["value"], save_path
    )


def cleanup_download_resources(
    application: Application,
    chat_id: int,
    source_type: str,
    source_value: str,
    base_save_path: str,
):
    """Handles all post-task cleanup of state and files."""
    logger.info(f"Cleaning up resources for task for chat_id {chat_id}.")

    active_downloads = application.bot_data.get("active_downloads", {})
    download_queues = application.bot_data.get("download_queues", {})
    if str(chat_id) in active_downloads:
        del active_downloads[str(chat_id)]

    save_state(PERSISTENCE_FILE, active_downloads, download_queues)

    if source_type == "file" and source_value and os.path.exists(source_value):
        logger.info(f"Deleting temporary .torrent file: {source_value}")
        os.remove(source_value)

    # Clean up leftover .parts files from libtorrent
    try:
        for filename in os.listdir(base_save_path):
            if filename.endswith(".parts"):
                parts_file_path = os.path.join(base_save_path, filename)
                logger.info(
                    f"Found and deleting leftover parts file: {parts_file_path}"
                )
                os.remove(parts_file_path)
    except Exception as e:
        logger.error(f"Could not perform .parts file cleanup in {base_save_path}: {e}")


async def process_queue_for_user(chat_id: int, application: Application):
    """
    Checks and processes the download queue for a user.
    This is the single authority for starting a download from the queue.
    """
    chat_id_str = str(chat_id)
    active_downloads = application.bot_data.get("active_downloads", {})
    download_queues = application.bot_data.get("download_queues", {})

    if chat_id_str in active_downloads:
        return  # A download is already active, do nothing.

    if chat_id_str in download_queues and download_queues[chat_id_str]:
        logger.info(f"No active download for {chat_id_str}. Starting next from queue.")

        next_download_data = download_queues[chat_id_str].pop(0)
        if not download_queues[chat_id_str]:
            del download_queues[chat_id_str]

        await _start_download_task(next_download_data, application)


async def _start_download_task(download_data: dict, application: Application):
    """Creates, registers, and persists a new download task."""
    active_downloads = application.bot_data.get("active_downloads", {})
    download_queues = application.bot_data.get("download_queues", {})
    chat_id_str = str(download_data["chat_id"])

    download_data["lock"] = asyncio.Lock()
    task = asyncio.create_task(download_task_wrapper(download_data, application))
    download_data["task"] = task
    active_downloads[chat_id_str] = download_data

    save_state(PERSISTENCE_FILE, active_downloads, download_queues)

    # Build initial controls and include "Stop" if queue exists for this user
    controls_row = [
        InlineKeyboardButton("⏹️ Cancel Download", callback_data="cancel_download")
    ]
    dq = application.bot_data.get("download_queues", {})
    if dq.get(chat_id_str):
        controls_row.append(InlineKeyboardButton("🛑 Stop", callback_data="cancel_all"))
    reply_markup = InlineKeyboardMarkup([controls_row])
    await safe_edit_message(
        application.bot,
        chat_id=download_data["chat_id"],
        message_id=download_data["message_id"],
        text="▶️ Your download is now starting\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=reply_markup,
    )


async def add_download_to_queue(update, context):
    """Adds a confirmed download to the user's queue."""
    query = update.callback_query
    chat_id = query.message.chat_id

    pending_torrent = context.user_data.pop("pending_torrent", None)
    if not pending_torrent:
        await safe_edit_message(
            query.message,
            text="This action has expired\\. Please send the link again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    active_downloads = context.bot_data["active_downloads"]
    download_queues = context.bot_data["download_queues"]
    chat_id_str = str(chat_id)

    # If a currently active download is paused, requeue it
    if chat_id_str in active_downloads and active_downloads[chat_id_str].get(
        "is_paused"
    ):
        logger.info(
            "New download added while another is paused. Requeueing the paused one."
        )
        active_data = active_downloads[chat_id_str]
        active_data["requeued"] = True
        if "task" in active_data and not active_data["task"].done():
            active_data["task"].cancel()

    save_paths = context.bot_data["SAVE_PATHS"]
    download_data = {
        "source_dict": pending_torrent,
        "chat_id": chat_id,
        "message_id": pending_torrent.get("message_id")
        or pending_torrent.get("original_message_id"),
        "save_path": save_paths["default"],
    }

    if chat_id_str not in download_queues:
        download_queues[chat_id_str] = []
    download_queues[chat_id_str].append(download_data)
    position = len(download_queues[chat_id_str])

    logger.info(
        f"User {chat_id_str} confirmed download. Queued at position {position}."
    )

    is_truly_active = chat_id_str in active_downloads and not active_downloads[
        chat_id_str
    ].get("requeued")
    if is_truly_active:
        message_text = (
            f"✅ Download queued\\. You are position \\#{position} in line\\."
        )
    else:
        message_text = "✅ Your download is next in line and will begin shortly\\."

    await safe_edit_message(
        query.message,
        text=message_text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=None,
    )
    save_state(PERSISTENCE_FILE, active_downloads, download_queues)
    await process_queue_for_user(chat_id, context.application)


async def add_season_to_queue(update, context):
    """Adds an entire season's torrents to the queue."""
    query = update.callback_query
    chat_id = query.message.chat_id

    pending_list = context.user_data.pop("pending_season_download", [])
    if not pending_list:
        await safe_edit_message(
            query.message,
            text="This action has expired\\. Please start over\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    active_downloads = context.bot_data["active_downloads"]
    download_queues = context.bot_data["download_queues"]
    chat_id_str = str(chat_id)

    if chat_id_str in active_downloads and active_downloads[chat_id_str].get(
        "is_paused"
    ):
        active_data = active_downloads[chat_id_str]
        active_data["requeued"] = True
        if "task" in active_data and not active_data["task"].done():
            active_data["task"].cancel()

    save_paths = context.bot_data["SAVE_PATHS"]
    if chat_id_str not in download_queues:
        download_queues[chat_id_str] = []

    # Create a batch id to defer Plex scan until all episodes are moved
    batch_id = f"season-{int(time.time())}-{chat_id}"
    batches: dict[str, Any] = context.bot_data.setdefault("DOWNLOAD_BATCHES", {})
    batches[batch_id] = {
        "total": len(pending_list),
        "done": 0,
        "media_type": "tv",
        "scanned": False,
        "summaries": [],
    }

    for torrent_data in pending_list:
        link = torrent_data.get("link")
        if not link:
            continue
        parsed_info = torrent_data.get("parsed_info", {})
        title = parsed_info.get("title") or "Download"
        season_value = parsed_info.get("season")
        clean_name = title
        if season_value not in (None, ""):
            try:
                season_number = int(season_value)
            except (TypeError, ValueError):
                logger.debug(
                    "Could not parse season value '%s' for title '%s'.",
                    season_value,
                    title,
                )
            else:
                clean_name = f"{title} S{season_number:02d}"

        source_dict = {
            "value": link,
            "type": "magnet" if link.startswith("magnet:") else "url",
            "parsed_info": parsed_info,
            "info_url": torrent_data.get("info_url"),
            "clean_name": clean_name,
            "batch_id": batch_id,
            "original_message_id": query.message.message_id,
        }
        download_data = {
            "source_dict": source_dict,
            "chat_id": chat_id,
            "message_id": query.message.message_id,
            "save_path": save_paths["default"],
        }
        download_queues[chat_id_str].append(download_data)

    added = len(pending_list)
    await safe_edit_message(
        query.message,
        text=f"✅ Success\\! Added {added} episodes to your download queue\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=None,
    )
    save_state(PERSISTENCE_FILE, active_downloads, download_queues)
    await process_queue_for_user(chat_id, context.application)


async def add_collection_to_queue(update, context):
    """Queues all pending collection downloads."""
    query = update.callback_query
    chat_id = query.message.chat_id

    pending_payload = context.user_data.pop("pending_collection_download", None)
    if not isinstance(pending_payload, dict):
        await safe_edit_message(
            query.message,
            text="This action has expired\\. Please restart the collection workflow\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    items = pending_payload.get("items") or []
    franchise_meta = pending_payload.get("franchise") or {}
    owned_summaries = list(pending_payload.get("owned_summaries") or [])
    if not items and not owned_summaries:
        await safe_edit_message(
            query.message,
            text="No movies were selected for download\\. Please try again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    active_downloads = context.bot_data["active_downloads"]
    download_queues = context.bot_data["download_queues"]
    chat_id_str = str(chat_id)

    if chat_id_str in active_downloads and active_downloads[chat_id_str].get(
        "is_paused"
    ):
        active_data = active_downloads[chat_id_str]
        active_data["requeued"] = True
        if "task" in active_data and not active_data["task"].done():
            active_data["task"].cancel()

    save_paths = context.bot_data["SAVE_PATHS"]
    if chat_id_str not in download_queues:
        download_queues[chat_id_str] = []

    batch_id = f"collection-{int(time.time())}-{chat_id}"
    batches: dict[str, Any] = context.bot_data.setdefault("DOWNLOAD_BATCHES", {})
    initial_summaries = list(owned_summaries)
    batches[batch_id] = {
        "total": len(items),
        "done": 0,
        "media_type": "movie",
        "scanned": False,
        "collection": franchise_meta,
        "summaries": initial_summaries,
    }

    if not items:
        await _finalize_owned_collection_batch(
            query, context, batch_id, franchise_meta, initial_summaries
        )
        return

    for entry in items:
        link = entry.get("link")
        if not link:
            continue
        parsed_info = entry.get("parsed_info", {})
        movie_meta = entry.get("movie") or {}
        movie_title = str(
            movie_meta.get("title") or parsed_info.get("title") or "Movie"
        )
        movie_year = movie_meta.get("year") or parsed_info.get("year")
        if movie_year:
            clean_name = f"{movie_title} ({movie_year})"
        else:
            clean_name = movie_title

        source_dict = {
            "value": link,
            "type": "magnet" if link.startswith("magnet:") else "url",
            "parsed_info": parsed_info,
            "info_url": entry.get("info_url"),
            "clean_name": clean_name,
            "batch_id": batch_id,
            "original_message_id": query.message.message_id,
        }
        download_data = {
            "source_dict": source_dict,
            "chat_id": chat_id,
            "message_id": query.message.message_id,
            "save_path": save_paths["default"],
        }
        download_queues[chat_id_str].append(download_data)

    await safe_edit_message(
        query.message,
        text=(
            f"✅ Added {len(items)} movie{'s' if len(items) != 1 else ''} from this collection to your queue\\."
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=None,
    )
    save_state(PERSISTENCE_FILE, active_downloads, download_queues)
    await process_queue_for_user(chat_id, context.application)


async def _finalize_owned_collection_batch(
    query,
    context,
    batch_id: str,
    franchise_meta: dict[str, Any] | None,
    summaries: list[str],
) -> None:
    """Completes a collection run that only reorganized owned titles."""
    batches: dict[str, Any] = context.bot_data.setdefault("DOWNLOAD_BATCHES", {})
    batches.pop(batch_id, None)

    collection_meta = franchise_meta or {}
    raw_name = str(collection_meta.get("name") or "this collection")
    collection_name = sanitize_collection_name(raw_name)
    collection_md = escape_markdown(raw_name, version=2)
    combined = "\n\n".join(summaries) if summaries else "✅ *Already Organized*"

    await finalize_movie_collection(context, collection_meta)

    info_line = (
        "\n\n*Collection Complete*\n"
        f"All titles for *{collection_md}* were already available\\.\n"
        "Organized your library and starting Plex scan…"
    )

    plex_config = context.bot_data.get("PLEX_CONFIG")
    scan_msg = await _trigger_plex_scan("movie", plex_config)

    # Wait 120s for Plex scan to likely complete
    if scan_msg:
        logger.info("Waiting 120 seconds for Plex scan to index existing movies...")
        await asyncio.sleep(120)

    added = await ensure_collection_contains_movies(
        plex_config,
        collection_name,
        collection_meta.get("movies") or [],
    )
    if added:
        info_line += f"\nAdded {len(added)} film{'s' if len(added) != 1 else ''} to the Plex collection\\."

    final_text = f"{combined}{info_line}{scan_msg}"
    await safe_edit_message(
        query.message,
        text=final_text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=None,
    )


async def handle_pause_resume(update, context):
    """Toggle pause or resume for the current download."""
    query = update.callback_query
    chat_id_str = str(query.message.chat_id)
    active_downloads = context.bot_data.get("active_downloads", {})

    if chat_id_str not in active_downloads:
        await safe_edit_message(
            query.message,
            text="ℹ️ Could not find an active download to pause or resume\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    download_data = active_downloads[chat_id_str]
    async with download_data["lock"]:
        handle = download_data.get("handle")
        if handle is None:
            logger.warning(
                "Pause/resume requested but no torrent handle found for user %s.",
                chat_id_str,
            )
            return

        # Use our tracked flag instead of libtorrent flags for compatibility
        is_paused = bool(download_data.get("is_paused"))
        if is_paused:
            handle.resume()
            download_data["is_paused"] = False
            logger.info(f"Resume request processed for user {chat_id_str}.")
        else:
            handle.pause()
            download_data["is_paused"] = True
            logger.info(f"Pause request processed for user {chat_id_str}.")


async def handle_cancel_request(update, context):
    """Handles a user's request to cancel a download."""
    query = update.callback_query
    chat_id_str = str(query.message.chat_id)
    active_downloads = context.bot_data.get("active_downloads", {})

    if chat_id_str not in active_downloads:
        await safe_edit_message(
            query.message,
            text="ℹ️ Could not find an active download to cancel\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    download_data = active_downloads[chat_id_str]
    async with download_data["lock"]:
        if query.data == "cancel_download":
            # Mark this download so progress updates pause during confirmation.
            download_data["cancellation_pending"] = True
            message_text = "Are you sure you want to cancel this download\\?"
            reply_markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Yes, Cancel", callback_data="cancel_confirm"
                        ),
                        InlineKeyboardButton(
                            "❌ No, Continue", callback_data="cancel_deny"
                        ),
                    ]
                ]
            )
            await safe_edit_message(
                query.message,
                text=message_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=reply_markup,
            )

        elif query.data == "cancel_confirm":
            # User confirmed cancellation; clear flag and stop the task.
            download_data.pop("cancellation_pending", None)
            logger.info(f"Cancellation confirmed for user {chat_id_str}.")
            if "task" in download_data and not download_data["task"].done():
                download_data["task"].cancel()

        elif query.data == "cancel_deny":
            # User opted not to cancel; remove the flag so updates resume.
            download_data.pop("cancellation_pending", None)


async def handle_cancel_all(update, context):
    """Two-step cancel-all: confirm, then clear queue and cancel active."""
    query = update.callback_query
    chat_id = query.message.chat_id
    chat_id_str = str(chat_id)

    action = query.data or ""
    active_downloads = context.bot_data.get("active_downloads", {})
    download_queues = context.bot_data.get("download_queues", {})

    # When initiating, set pending flag and ask for confirmation
    if action == "cancel_all":
        if chat_id_str in active_downloads:
            dd = active_downloads[chat_id_str]
            async with dd["lock"]:
                dd["cancellation_pending"] = True

        message_text = (
            "Are you sure you want to cancel the current download "
            "and clear all queued downloads?"
        )
        reply_markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Yes, Cancel All", callback_data="cancel_all_confirm"
                    ),
                    InlineKeyboardButton(
                        "❌ No, Continue", callback_data="cancel_all_deny"
                    ),
                ]
            ]
        )
        await safe_edit_message(
            query.message,
            text=message_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup,
        )
        return

    # Confirm: clear queue and cancel active
    if action == "cancel_all_confirm":
        # Clear all queued downloads for this user
        if chat_id_str in download_queues:
            removed = len(download_queues.get(chat_id_str, []))
            del download_queues[chat_id_str]
            logger.info(f"Cleared {removed} queued downloads for user {chat_id_str}.")

        # Cancel the active download task if present
        if chat_id_str in active_downloads:
            dd = active_downloads[chat_id_str]
            async with dd["lock"]:
                dd.pop("cancellation_pending", None)
                task = dd.get("task")
                if task and not task.done():
                    logger.info(f"Cancelling active download for user {chat_id_str}.")
                    task.cancel()

        # Persist state after clearing the queue
        save_state(PERSISTENCE_FILE, active_downloads, download_queues)

        # Acknowledge; the active task will finalize its own message text
        try:
            await safe_edit_message(
                query.message,
                text="⏹️ Cancelled all downloads for this chat\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=None,
            )
        except Exception:
            pass
        return

    # Deny: remove pending flag and resume updates
    if action == "cancel_all_deny":
        if chat_id_str in active_downloads:
            dd = active_downloads[chat_id_str]
            async with dd["lock"]:
                dd.pop("cancellation_pending", None)
        # No immediate re-render; progress updates will resume naturally
        return
