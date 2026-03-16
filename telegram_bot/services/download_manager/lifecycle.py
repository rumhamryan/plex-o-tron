# telegram_bot/services/download_manager/lifecycle.py

import asyncio
from typing import Any

import libtorrent as lt
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError
from telegram.ext import Application
from telegram.helpers import escape_markdown

from telegram_bot.config import PERSISTENCE_FILE, logger
from telegram_bot.domain.types import BatchMeta, DownloadData, SourceDict
from telegram_bot.utils import sanitize_collection_name

from .adapters import join_path, list_dir, path_exists, remove_file
from .bot_data_access import (
    get_active_downloads,
    get_download_queues,
    get_or_create_download_batches,
    get_plex_config,
    get_save_paths,
)
from .collection_reporting import (
    build_collection_reconciliation_lines,
    get_collection_movies_for_plex,
)
from .progress import ProgressReporter


async def download_task_wrapper(download_data: DownloadData, application: Application) -> None:
    """
    Wraps the entire download lifecycle for a single torrent.
    Handles success, failure, cancellation, and requeueing.
    """
    from . import (
        _finalize_download,
        _requeue_download,
        download_with_progress,
        handle_successful_download,
        process_queue_for_user,
    )

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
                batches = get_or_create_download_batches(application.bot_data)
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
                save_paths=get_save_paths(application.bot_data),
                plex_config=get_plex_config(application.bot_data),
                defer_scan=defer_scan,
            )
            # Now that the media file has been moved, we can safely delete the originals.
            logger.info(f"Removing torrent and deleting original files for: {clean_name}")
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
                message_text = (
                    "❌ *Download Failed*\nAn unknown error occurred in the download manager\\."
                )

    except TimeoutError as e:
        if str(e) == "metadata_timeout":
            logger.warning(f"Metadata timeout for '{clean_name}'. Requeueing.")
            download_data["requeued"] = True
            download_data["metadata_timeout_occurred"] = True
            message_text = (
                f"⚠️ *Metadata Timeout*\nRetrying download for:\n`{escape_markdown(clean_name)}`"
            )

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
            message_text = (
                f"❌ *Error*\nAn unexpected timeout occurred:\n`{escape_markdown(str(e))}`"
            )

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

            message_text = (
                f"⏹️ *Cancelled*\nDownload has been stopped for:\n`{escape_markdown(clean_name)}`"
            )

    except Exception as e:
        logger.error(
            f"Unexpected exception in download task for '{clean_name}': {e}",
            exc_info=True,
        )
        message_text = f"❌ *Error*\nAn unexpected error occurred:\n`{escape_markdown(str(e))}`"

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
    source_dict: SourceDict,
    message_text: str,
    parsed_info: dict[str, Any],
) -> str:
    """Updates season-batch counters and triggers a single Plex scan on completion.

    Returns the (possibly) augmented message_text with batch-complete info lines.
    """
    from . import (
        _trigger_plex_scan,
        ensure_collection_contains_movies,
        finalize_movie_collection,
        wait_for_movies_to_be_available,
    )

    try:
        batch_id = source_dict.get("batch_id")
        if not batch_id:
            return message_text

        batches: dict[str, BatchMeta] = get_or_create_download_batches(application.bot_data)
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
        plex_config = get_plex_config(application.bot_data)

        collection_meta: dict[str, Any] = dict(batch.get("collection") or {})

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
            collection_name = collection_meta.get("name") or "this collection"
            collection_md = escape_markdown(str(collection_name), version=2)
            info_line = (
                "\n\n*Collection Complete*\n"
                f"Queued {total}/{total} movies for *{collection_md}*\\.\n"
                "Starting Plex scan…"
            )
            combined_message = "\n\n".join(summaries) if summaries else message_text

        finalization: dict[str, Any] = {}
        if media_type == "movie":
            finalization = await finalize_movie_collection(application, collection_meta)
            reconciliation_lines = build_collection_reconciliation_lines(finalization)
            if reconciliation_lines:
                info_line += "\n" + "\n".join(reconciliation_lines)

        scan_msg = await _trigger_plex_scan(media_type, plex_config)

        if media_type == "movie":
            raw_name = str(collection_meta.get("name") or "").strip()
            collection_name = sanitize_collection_name(raw_name)
            organized_movies = get_collection_movies_for_plex(finalization)
            if scan_msg:
                logger.info("Waiting for Plex to index new movies before tagging the collection...")
                await wait_for_movies_to_be_available(plex_config, organized_movies)
            added = await ensure_collection_contains_movies(
                plex_config,
                collection_name,
                organized_movies,
            )
            if added:
                info_line += f"\nAdded {len(added)} film{'s' if len(added) != 1 else ''} to the Plex collection\\."

        batch.pop("summaries", None)
        return f"{combined_message}{info_line}{scan_msg}"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Batch tracking error: {e}")
        return message_text


async def _requeue_download(download_data: DownloadData, application: Application) -> None:
    """Moves a paused or interrupted download to the back of the queue."""
    from . import process_queue_for_user, save_state

    chat_id = download_data["chat_id"]
    chat_id_str = str(chat_id)
    logger.info(f"Requeueing download for user {chat_id_str}.")

    active_downloads = get_active_downloads(application.bot_data)
    download_queues = get_download_queues(application.bot_data)

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
        logger.info("Metadata timeout on the only queued item. Waiting 60s before retry.")
        await asyncio.sleep(60)

    save_state(PERSISTENCE_FILE, active_downloads, download_queues)
    await process_queue_for_user(chat_id, application)  # Start next in queue


async def _finalize_download(
    application: Application,
    chat_id: int,
    message_id: int,
    message_text: str,
    source_dict: SourceDict,
    save_path: str,
) -> None:
    """Handles final message sending and resource cleanup."""
    from . import safe_edit_message

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
    from . import save_state

    logger.info(f"Cleaning up resources for task for chat_id {chat_id}.")

    active_downloads = get_active_downloads(application.bot_data)
    download_queues = get_download_queues(application.bot_data)
    if str(chat_id) in active_downloads:
        del active_downloads[str(chat_id)]

    save_state(PERSISTENCE_FILE, active_downloads, download_queues)

    if source_type == "file" and source_value and path_exists(source_value):
        logger.info(f"Deleting temporary .torrent file: {source_value}")
        remove_file(source_value)

    # Clean up leftover .parts files from libtorrent
    try:
        for filename in list_dir(base_save_path):
            if filename.endswith(".parts"):
                parts_file_path = join_path(base_save_path, filename)
                logger.info(f"Found and deleting leftover parts file: {parts_file_path}")
                remove_file(parts_file_path)
    except Exception as e:
        logger.error(f"Could not perform .parts file cleanup in {base_save_path}: {e}")
