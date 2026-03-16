# telegram_bot/services/download_manager/queue.py

import asyncio
import time
from typing import Any, cast

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown

from telegram_bot.config import PERSISTENCE_FILE, logger
from telegram_bot.domain.types import BatchCollectionMeta, BatchMeta, DownloadData, SourceDict
from telegram_bot.ui.messages import (
    BTN_CANCEL_DOWNLOAD,
    BTN_STOP_ALL,
    MSG_ACTION_EXPIRED_RESTART_COLLECTION,
    MSG_ACTION_EXPIRED_SEND_LINK,
    MSG_ACTION_EXPIRED_START_OVER,
    MSG_DOWNLOAD_NEXT_IN_LINE,
    MSG_NO_MOVIES_SELECTED,
    MSG_STARTING_DOWNLOAD,
    format_collection_queue_added,
    format_download_queue_position,
    format_season_queue_added,
)
from telegram_bot.utils import sanitize_collection_name

from .bot_data_access import (
    get_active_downloads,
    get_download_queues,
    get_or_create_download_batches,
    get_plex_config,
    require_active_downloads,
    require_download_queues,
    require_save_paths,
)
from .collection_reporting import (
    build_collection_reconciliation_lines,
    get_collection_movies_for_plex,
)


async def process_queue_for_user(chat_id: int, application) -> None:
    """
    Checks and processes the download queue for a user.
    This is the single authority for starting a download from the queue.
    """
    from . import _start_download_task

    chat_id_str = str(chat_id)
    active_downloads = get_active_downloads(application.bot_data)
    download_queues = get_download_queues(application.bot_data)

    if chat_id_str in active_downloads:
        return  # A download is already active, do nothing.

    if chat_id_str in download_queues and download_queues[chat_id_str]:
        logger.info(f"No active download for {chat_id_str}. Starting next from queue.")

        next_download_data = download_queues[chat_id_str].pop(0)
        if not download_queues[chat_id_str]:
            del download_queues[chat_id_str]

        await _start_download_task(next_download_data, application)


async def _start_download_task(download_data: DownloadData, application) -> None:
    """Creates, registers, and persists a new download task."""
    from . import download_task_wrapper, safe_edit_message, save_state

    active_downloads = get_active_downloads(application.bot_data)
    download_queues = get_download_queues(application.bot_data)
    chat_id_str = str(download_data["chat_id"])

    download_data["lock"] = asyncio.Lock()
    task = asyncio.create_task(download_task_wrapper(download_data, application))
    download_data["task"] = task
    active_downloads[chat_id_str] = download_data

    save_state(PERSISTENCE_FILE, active_downloads, download_queues)

    # Build initial controls and include "Stop" if queue exists for this user
    controls_row = [InlineKeyboardButton(BTN_CANCEL_DOWNLOAD, callback_data="cancel_download")]
    dq = get_download_queues(application.bot_data)
    if dq.get(chat_id_str):
        controls_row.append(InlineKeyboardButton(BTN_STOP_ALL, callback_data="cancel_all"))
    reply_markup = InlineKeyboardMarkup([controls_row])
    await safe_edit_message(
        application.bot,
        chat_id=download_data["chat_id"],
        message_id=download_data["message_id"],
        text=MSG_STARTING_DOWNLOAD,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=reply_markup,
    )


async def add_download_to_queue(update, context) -> bool:
    """Adds a confirmed download to the user's queue."""
    from . import process_queue_for_user, safe_edit_message, save_state

    query = update.callback_query
    chat_id = query.message.chat_id

    pending_torrent = context.user_data.pop("pending_torrent", None)
    if not pending_torrent:
        await safe_edit_message(
            query.message,
            text=MSG_ACTION_EXPIRED_SEND_LINK,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return False

    active_downloads = require_active_downloads(context.bot_data)
    download_queues = require_download_queues(context.bot_data)
    chat_id_str = str(chat_id)

    # If a currently active download is paused, requeue it
    if chat_id_str in active_downloads and active_downloads[chat_id_str].get("is_paused"):
        logger.info("New download added while another is paused. Requeueing the paused one.")
        active_data = active_downloads[chat_id_str]
        active_data["requeued"] = True
        if "task" in active_data and not active_data["task"].done():
            active_data["task"].cancel()

    save_paths = require_save_paths(context.bot_data)
    download_data: DownloadData = {
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

    logger.info(f"User {chat_id_str} confirmed download. Queued at position {position}.")

    is_truly_active = chat_id_str in active_downloads and not active_downloads[chat_id_str].get(
        "requeued"
    )
    started_download = not is_truly_active
    if is_truly_active:
        message_text = format_download_queue_position(position)
    else:
        message_text = MSG_DOWNLOAD_NEXT_IN_LINE

    await safe_edit_message(
        query.message,
        text=message_text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=None,
    )
    save_state(PERSISTENCE_FILE, active_downloads, download_queues)
    await process_queue_for_user(chat_id, context.application)
    return started_download


async def add_season_to_queue(update, context) -> bool:
    """Adds an entire season's torrents to the queue."""
    from . import process_queue_for_user, safe_edit_message, save_state

    query = update.callback_query
    chat_id = query.message.chat_id

    pending_list = context.user_data.pop("pending_season_download", [])
    if not pending_list:
        await safe_edit_message(
            query.message,
            text=MSG_ACTION_EXPIRED_START_OVER,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return False

    active_downloads = require_active_downloads(context.bot_data)
    download_queues = require_download_queues(context.bot_data)
    chat_id_str = str(chat_id)
    had_active_download = chat_id_str in active_downloads and not active_downloads[chat_id_str].get(
        "requeued"
    )

    if chat_id_str in active_downloads and active_downloads[chat_id_str].get("is_paused"):
        active_data = active_downloads[chat_id_str]
        active_data["requeued"] = True
        if "task" in active_data and not active_data["task"].done():
            active_data["task"].cancel()

    save_paths = require_save_paths(context.bot_data)
    if chat_id_str not in download_queues:
        download_queues[chat_id_str] = []

    # Create a batch id to defer Plex scan until all episodes are moved
    batch_id = f"season-{int(time.time())}-{chat_id}"
    batches: dict[str, BatchMeta] = get_or_create_download_batches(context.bot_data)
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

        source_dict: SourceDict = {
            "value": link,
            "type": "magnet" if link.startswith("magnet:") else "url",
            "parsed_info": parsed_info,
            "info_url": torrent_data.get("info_url"),
            "clean_name": clean_name,
            "batch_id": batch_id,
            "original_message_id": query.message.message_id,
        }
        download_data: DownloadData = {
            "source_dict": source_dict,
            "chat_id": chat_id,
            "message_id": query.message.message_id,
            "save_path": save_paths["default"],
        }
        download_queues[chat_id_str].append(download_data)

    added = len(pending_list)
    await safe_edit_message(
        query.message,
        text=format_season_queue_added(added),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=None,
    )
    save_state(PERSISTENCE_FILE, active_downloads, download_queues)
    await process_queue_for_user(chat_id, context.application)
    return (not had_active_download) and added > 0


async def add_collection_to_queue(update, context) -> bool:
    """Queues all pending collection downloads."""
    from . import process_queue_for_user, safe_edit_message, save_state

    query = update.callback_query
    chat_id = query.message.chat_id

    pending_payload = context.user_data.pop("pending_collection_download", None)
    if not isinstance(pending_payload, dict):
        await safe_edit_message(
            query.message,
            text=MSG_ACTION_EXPIRED_RESTART_COLLECTION,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return False

    items = pending_payload.get("items") or []
    franchise_raw = pending_payload.get("franchise")
    franchise_meta = (
        cast(BatchCollectionMeta, franchise_raw) if isinstance(franchise_raw, dict) else None
    )
    owned_summaries = list(pending_payload.get("owned_summaries") or [])
    if not items and not owned_summaries:
        await safe_edit_message(
            query.message,
            text=MSG_NO_MOVIES_SELECTED,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return False

    active_downloads = require_active_downloads(context.bot_data)
    download_queues = require_download_queues(context.bot_data)
    chat_id_str = str(chat_id)
    had_active_download = chat_id_str in active_downloads and not active_downloads[chat_id_str].get(
        "requeued"
    )

    if chat_id_str in active_downloads and active_downloads[chat_id_str].get("is_paused"):
        active_data = active_downloads[chat_id_str]
        active_data["requeued"] = True
        if "task" in active_data and not active_data["task"].done():
            active_data["task"].cancel()

    save_paths = require_save_paths(context.bot_data)
    if chat_id_str not in download_queues:
        download_queues[chat_id_str] = []

    batch_id = f"collection-{int(time.time())}-{chat_id}"
    batches: dict[str, BatchMeta] = get_or_create_download_batches(context.bot_data)
    initial_summaries = list(owned_summaries)
    batch_meta: BatchMeta = {
        "total": len(items),
        "done": 0,
        "media_type": "movie",
        "scanned": False,
        "summaries": initial_summaries,
    }
    if franchise_meta is not None:
        batch_meta["collection"] = franchise_meta
    batches[batch_id] = batch_meta

    if not items:
        await _finalize_owned_collection_batch(
            query, context, batch_id, franchise_meta, initial_summaries
        )
        return False

    for entry in items:
        link = entry.get("link")
        if not link:
            continue
        parsed_info = entry.get("parsed_info", {})
        movie_meta = entry.get("movie") or {}
        movie_title = str(movie_meta.get("title") or parsed_info.get("title") or "Movie")
        movie_year = movie_meta.get("year") or parsed_info.get("year")
        if movie_year:
            clean_name = f"{movie_title} ({movie_year})"
        else:
            clean_name = movie_title

        source_dict: SourceDict = {
            "value": link,
            "type": "magnet" if link.startswith("magnet:") else "url",
            "parsed_info": parsed_info,
            "info_url": entry.get("info_url"),
            "clean_name": clean_name,
            "batch_id": batch_id,
            "original_message_id": query.message.message_id,
        }
        download_data: DownloadData = {
            "source_dict": source_dict,
            "chat_id": chat_id,
            "message_id": query.message.message_id,
            "save_path": save_paths["default"],
        }
        download_queues[chat_id_str].append(download_data)

    await safe_edit_message(
        query.message,
        text=format_collection_queue_added(len(items)),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=None,
    )
    save_state(PERSISTENCE_FILE, active_downloads, download_queues)
    await process_queue_for_user(chat_id, context.application)
    return (not had_active_download) and len(items) > 0


async def _finalize_owned_collection_batch(
    query,
    context,
    batch_id: str,
    franchise_meta: BatchCollectionMeta | None,
    summaries: list[str],
) -> None:
    """Completes a collection run that only reorganized owned titles."""
    from . import (
        _trigger_plex_scan,
        ensure_collection_contains_movies,
        finalize_movie_collection,
        safe_edit_message,
        wait_for_movies_to_be_available,
    )

    batches: dict[str, BatchMeta] = get_or_create_download_batches(context.bot_data)
    batches.pop(batch_id, None)

    collection_meta: dict[str, Any] = dict(franchise_meta or {})
    raw_name = str(collection_meta.get("name") or "this collection")
    collection_name = sanitize_collection_name(raw_name)
    collection_md = escape_markdown(raw_name, version=2)
    combined = "\n\n".join(summaries) if summaries else "✅ *Already Organized*"

    finalization = await finalize_movie_collection(context, collection_meta)
    reconciliation_lines = build_collection_reconciliation_lines(finalization)
    organized_movies = get_collection_movies_for_plex(finalization)

    info_line = (
        "\n\n*Collection Complete*\n"
        f"All titles for *{collection_md}* were already available\\.\n"
        "Reconciled your library and starting Plex scan…"
    )
    if reconciliation_lines:
        info_line += "\n" + "\n".join(reconciliation_lines)

    plex_config = get_plex_config(context.bot_data)
    scan_msg = await _trigger_plex_scan("movie", plex_config)
    initial_text = f"{combined}{info_line}{scan_msg}"
    await safe_edit_message(
        query.message,
        text=initial_text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=None,
    )

    if scan_msg:
        logger.info("Waiting for Plex to index existing movies before tagging the collection...")
        await wait_for_movies_to_be_available(plex_config, organized_movies)

    added = await ensure_collection_contains_movies(plex_config, collection_name, organized_movies)
    if added:
        final_text = (
            f"{initial_text}\nAdded {len(added)} film{'s' if len(added) != 1 else ''} "
            "to the Plex collection\\."
        )
        await safe_edit_message(
            query.message,
            text=final_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=None,
        )
