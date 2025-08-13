# telegram_bot/services/download_manager.py

import asyncio
import os
import time
from typing import Any
from collections.abc import Callable, Coroutine

import libtorrent as lt
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError
from telegram.helpers import escape_markdown

from ..config import logger, PERSISTENCE_FILE
from ..state import save_state
from ..utils import safe_edit_message
from .media_manager import handle_successful_download


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
            if current_time - self.last_update_time < 5:
                return
            self.last_update_time = current_time

            progress_percent = status.progress * 100
            speed_mbps = status.download_rate / 1024 / 1024

            # --- CORRECTED STRING PREPARATION WITH version=2 ---
            progress_str = escape_markdown(f"{progress_percent:.2f}", version=2)
            speed_str = escape_markdown(f"{speed_mbps:.2f}", version=2)

            # --- Build the message content ---
            if self.parsed_info.get("type") == "tv":
                s = self.parsed_info.get("season", 0)
                e = self.parsed_info.get("episode", 0)
                ep_title = self.parsed_info.get("episode_title", "")

                # Also apply version=2 to the title escaping
                title_str = escape_markdown(
                    self.parsed_info.get("title", ""), version=2
                )
                episode_details_str = escape_markdown(
                    f"S{s:02d}E{e:02d} - {ep_title}", version=2
                )

                name_str = f"`{title_str}`\n" f"`{episode_details_str}`"
            else:
                name_str = f"`{escape_markdown(self.clean_name, version=2)}`"

            is_paused = self.download_data.get("is_paused", False)
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

            if is_paused:
                reply_markup = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "▶️ Resume", callback_data="resume_download"
                            ),
                            InlineKeyboardButton(
                                "⏹️ Cancel", callback_data="cancel_download"
                            ),
                        ]
                    ]
                )
            else:
                reply_markup = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "⏸️ Pause", callback_data="pause_download"
                            ),
                            InlineKeyboardButton(
                                "⏹️ Cancel", callback_data="cancel_download"
                            ),
                        ]
                    ]
                )

            await safe_edit_message(
                self.application.bot,
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=message_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=reply_markup,
            )


async def download_with_progress(
    source: str,
    save_path: str,
    status_callback: Callable[[lt.torrent_status], Coroutine[Any, Any, None]],  # type: ignore
    bot_data: dict,
    download_data: dict,
) -> tuple[bool, lt.torrent_info | None]:  # type: ignore
    """
    Core libtorrent download logic.
    Returns (success_status, torrent_info_object).
    """
    ses = bot_data["TORRENT_SESSION"]
    params = {
        "save_path": save_path,
        "storage_mode": lt.storage_mode_t.storage_mode_sparse,  # type: ignore
    }

    if source.startswith("magnet:"):
        params = lt.parse_magnet_uri(source)  # type: ignore
        params.save_path = save_path
        # You can add other params attributes here if needed
    else:
        params = {
            "save_path": save_path,
            "storage_mode": lt.storage_mode_t.storage_mode_sparse,  # type: ignore
            "ti": lt.torrent_info(source),  # type: ignore
        }

    handle = ses.add_torrent(params)
    download_data["handle"] = handle  # Store handle for pausing/resuming

    start_time = time.monotonic()
    while not handle.status().is_seeding:
        if bot_data.get("is_shutting_down") or download_data.get("requeued"):
            raise asyncio.CancelledError("Shutdown or requeue initiated.")

        # Handle pausing
        if download_data.get("is_paused"):
            handle.pause()
            # Loop here until resumed, but still check for shutdown
            while download_data.get("is_paused"):
                if bot_data.get("is_shutting_down"):
                    raise asyncio.CancelledError("Shutdown initiated.")
                await asyncio.sleep(1)
            handle.resume()

        status = handle.status()
        await status_callback(status)

        # Timeout logic for stalled torrents
        if status.state == lt.torrent_status.downloading_metadata and time.monotonic() - start_time > 60:  # type: ignore
            logger.warning(f"Metadata download timed out for {handle.name()}")
            return False, None

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
        )

        if success and ti:
            message_text = await handle_successful_download(
                ti=ti,
                parsed_info=source_dict.get("parsed_info", {}),
                initial_download_path=initial_save_path,
                save_paths=application.bot_data.get("SAVE_PATHS", {}),
                plex_config=application.bot_data.get("PLEX_CONFIG"),
            )
        else:
            if not download_data.get("requeued"):
                message_text = "❌ *Download Failed*\nAn unknown error occurred in the download manager."

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
    download_data["is_paused"] = True  # Ensure it's marked as paused

    if chat_id_str not in download_queues:
        download_queues[chat_id_str] = []
    download_queues[chat_id_str].append(download_data)

    if chat_id_str in active_downloads:
        del active_downloads[chat_id_str]

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

    reply_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⏹️ Cancel Download", callback_data="cancel_download")]]
    )
    await safe_edit_message(
        application.bot,
        chat_id=download_data["chat_id"],
        message_id=download_data["message_id"],
        text="▶️ Your download is now starting...",
        reply_markup=reply_markup,
    )


async def add_download_to_queue(update, context):
    """Adds a confirmed download to the user's queue."""
    query = update.callback_query
    chat_id = query.message.chat_id

    pending_torrent = context.user_data.pop("pending_torrent", None)
    if not pending_torrent:
        await safe_edit_message(
            query.message, text="This action has expired. Please send the link again."
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
        "message_id": pending_torrent["original_message_id"],
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
        message_text = f"✅ Download queued. You are position #{position} in line."
    else:
        message_text = "✅ Your download is next in line and will begin shortly."

    await safe_edit_message(query.message, text=message_text, reply_markup=None)
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
            text="This action has expired. Please start over.",
        )
        return

    active_downloads = context.bot_data["active_downloads"]
    download_queues = context.bot_data["download_queues"]
    chat_id_str = str(chat_id)

    if chat_id_str in active_downloads and active_downloads[chat_id_str].get("is_paused"):
        active_data = active_downloads[chat_id_str]
        active_data["requeued"] = True
        if "task" in active_data and not active_data["task"].done():
            active_data["task"].cancel()

    save_paths = context.bot_data["SAVE_PATHS"]
    if chat_id_str not in download_queues:
        download_queues[chat_id_str] = []

    for link in pending_list:
        source_dict = {
            "value": link,
            "type": "magnet" if link.startswith("magnet:") else "url",
            "parsed_info": {},
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
        text=f"✅ Success! Added {added} episodes to your download queue.",
        reply_markup=None,
    )
    save_state(PERSISTENCE_FILE, active_downloads, download_queues)
    await process_queue_for_user(chat_id, context.application)


async def handle_pause_request(update, context):
    """Handles a user's request to pause a download."""
    query = update.callback_query
    chat_id_str = str(query.message.chat_id)
    active_downloads = context.bot_data.get("active_downloads", {})

    if chat_id_str in active_downloads:
        download_data = active_downloads[chat_id_str]
        async with download_data["lock"]:
            download_data["is_paused"] = True
            logger.info(f"Pause request received for download for user {chat_id_str}.")
    else:
        await safe_edit_message(
            query.message, text="ℹ️ Could not find an active download to pause."
        )


async def handle_resume_request(update, context):
    """Handles a user's request to resume a download."""
    query = update.callback_query
    chat_id_str = str(query.message.chat_id)
    active_downloads = context.bot_data.get("active_downloads", {})

    if chat_id_str in active_downloads:
        download_data = active_downloads[chat_id_str]
        async with download_data["lock"]:
            download_data["is_paused"] = False
            logger.info(f"Resume request received for download for user {chat_id_str}.")
    else:
        await safe_edit_message(
            query.message,
            text="ℹ️ This download is in the queue and will resume automatically.",
        )


async def handle_cancel_request(update, context):
    """Handles a user's request to cancel a download."""
    query = update.callback_query
    chat_id_str = str(query.message.chat_id)
    active_downloads = context.bot_data.get("active_downloads", {})

    if chat_id_str not in active_downloads:
        await safe_edit_message(
            query.message, text="ℹ️ Could not find an active download to cancel."
        )
        return

    download_data = active_downloads[chat_id_str]
    async with download_data["lock"]:
        if query.data == "cancel_download":
            message_text = "Are you sure you want to cancel this download?"
            reply_markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Yes, Cancel", callback_data="cancel_confirm"
                        ),
                        InlineKeyboardButton(
                            "❌ No, Continue", callback_data="resume_download"
                        ),  # Simplification
                    ]
                ]
            )
            await safe_edit_message(
                query.message, text=message_text, reply_markup=reply_markup
            )

        elif query.data == "cancel_confirm":
            logger.info(f"Cancellation confirmed for user {chat_id_str}.")
            if "task" in download_data and not download_data["task"].done():
                download_data["task"].cancel()
