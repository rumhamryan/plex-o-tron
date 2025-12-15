import asyncio
import os
import shutil
import re
from typing import Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown
from telegram.constants import ParseMode

from ..config import logger
from ..services import scraping_service, search_logic, plex_service
from ..services.download_manager import add_download_to_queue, process_queue_for_user
from ..services.media_manager import parse_resolution_from_name
from ..utils import safe_edit_message, parse_torrent_name, safe_send_message


def _sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()


async def handle_franchise_confirmation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Handles the confirmation of a seed movie in collection mode.
    """
    if not context.user_data or "pending_torrent" not in context.user_data:
        return

    seed_torrent = context.user_data["pending_torrent"]
    parsed_info = seed_torrent.get("parsed_info", {})
    is_collection_mode = parsed_info.get("collection_mode", False)

    # Queue the seed download
    await add_download_to_queue(update, context)

    if is_collection_mode:
        asyncio.create_task(run_franchise_logic(update, context, seed_torrent))


async def run_franchise_logic(
    update: Update, context: ContextTypes.DEFAULT_TYPE, seed_torrent: dict[str, Any]
) -> None:
    parsed_info = seed_torrent.get("parsed_info", {})
    movie_title = parsed_info.get("title")
    if not movie_title:
        return

    chat_id = update.effective_chat.id if update.effective_chat else 0
    status_msg = await safe_send_message(
        context.bot,
        chat_id,
        f"🎬 *Collection Mode*\nDetecting franchise details for *{escape_markdown(movie_title, version=2)}*\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    # 1. Fetch Details
    franchise_info = await scraping_service.fetch_franchise_details(movie_title)
    if not franchise_info:
        await safe_edit_message(
            status_msg,
            f"⚠️ Could not detect a franchise for *{escape_markdown(movie_title, version=2)}*\\. Proceeding as single movie\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # Initialize selection state (default all selected)
    for m in franchise_info["movies"]:
        m["selected"] = True

    context.user_data["franchise_pending"] = {
        "franchise_info": franchise_info,
        "seed_torrent": seed_torrent,
        "status_message_id": status_msg.message_id,
        "chat_id": chat_id,
    }

    await _present_selection_ui(context, chat_id, status_msg.message_id)


async def _present_selection_ui(context, chat_id, message_id):
    data = context.user_data.get("franchise_pending")
    if not data:
        return

    movies = data["franchise_info"]["movies"]
    name = data["franchise_info"]["name"]

    keyboard = []
    # Limit buttons if too many? Telegram limit is high for vertical lists but let's be safe.
    # Pagination might be needed if franchise > 100 movies (rare).
    for i, m in enumerate(movies):
        status = "✅" if m["selected"] else "❌"
        title = m["title"]
        year = m.get("year") or "?"
        text = f"{status} {title} ({year})"
        # Max 64 bytes for callback_data
        keyboard.append(
            [InlineKeyboardButton(text, callback_data=f"franchise_toggle_{i}")]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "✅ Confirm Selection", callback_data="franchise_confirm"
            ),
        ]
    )
    keyboard.append(
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]
    )

    text = (
        f"🎞️ *{escape_markdown(name, version=2)}* Collection\n\n"
        "Select the movies you want to *INCLUDE* in the download queue:"
    )

    await safe_edit_message(
        context.bot,
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def handle_franchise_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query.data:
        return

    try:
        idx = int(query.data.split("_")[-1])
    except ValueError:
        return

    data = context.user_data.get("franchise_pending")
    if not data:
        await query.answer("Session expired.")
        return

    movies = data["franchise_info"]["movies"]
    if 0 <= idx < len(movies):
        movies[idx]["selected"] = not movies[idx]["selected"]
        await _present_selection_ui(context, data["chat_id"], data["status_message_id"])

    await query.answer()


async def handle_franchise_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    data = context.user_data.get("franchise_pending")

    if not data:
        await safe_edit_message(
            query.message, "❌ Session expired.", parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    await safe_edit_message(
        query.message,
        "✅ Selection confirmed. Processing queue...",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    # Proceed in background
    asyncio.create_task(_process_franchise_queueing(context, data, query.message))

    del context.user_data["franchise_pending"]
    await query.answer()


async def _process_franchise_queueing(context, data, status_msg):
    franchise_info = data["franchise_info"]
    seed_torrent = data["seed_torrent"]
    chat_id = data["chat_id"]

    franchise_name = franchise_info["name"]
    movies = franchise_info["movies"]

    # 2. Setup Directory
    save_paths = context.bot_data.get("SAVE_PATHS", {})
    movies_root = save_paths.get("movies", save_paths.get("default"))
    franchise_dir = os.path.join(movies_root, _sanitize_filename(franchise_name))

    if not os.path.exists(franchise_dir):
        os.makedirs(franchise_dir, exist_ok=True)

    # 3. Update Seed Download Destination
    _update_seed_destination(context, seed_torrent, franchise_dir)

    # 4. Move Existing Movies
    moved_count = await _move_existing_movies(movies_root, franchise_dir, movies)

    # 5. Queue Missing
    queued_count = 0
    parsed_info = seed_torrent.get("parsed_info", {})
    movie_title = parsed_info.get("title")
    seed_year = parsed_info.get("year")
    seed_res = parse_resolution_from_name(seed_torrent.get("clean_name", "")) or "1080p"

    for movie in movies:
        if not movie.get("selected"):
            continue

        title = movie["title"]
        year = movie.get("year")

        # Skip seed
        if _is_same_movie(title, year, movie_title, seed_year):
            continue

        # Check if present
        if _is_movie_present(franchise_dir, title, year):
            continue

        # Search and Queue
        await safe_edit_message(
            status_msg,
            f"Searching for: *{escape_markdown(title, version=2)}*\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

        results = await search_logic.orchestrate_searches(
            title,
            "movie",
            context,
            year=str(year) if year else None,
            resolution=seed_res,
        )

        if not results:
            # Fallback
            results = await search_logic.orchestrate_searches(
                title, "movie", context, year=str(year) if year else None
            )

        candidate = _select_best_candidate(results, seed_res)

        if candidate:
            await _queue_franchise_item(
                context, candidate, franchise_dir, chat_id, title, year
            )
            queued_count += 1
        else:
            logger.warning(f"Could not find torrent for franchise movie: {title}")

    # 6. Plex Collection
    await plex_service.create_plex_collection(context, franchise_name, movies)

    await safe_edit_message(
        status_msg,
        f"✅ Franchise *{escape_markdown(franchise_name, version=2)}* processed\\.\nMoved: {moved_count}, Queued: {queued_count}\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


def _update_seed_destination(context, seed_torrent, dest_path):
    original_msg_id = seed_torrent.get("original_message_id")

    def _update(data_list):
        for data in data_list:
            if data.get("message_id") == original_msg_id:
                src = data.get("source_dict", {})
                if "parsed_info" in src:
                    src["parsed_info"]["destination_folder"] = dest_path
                    return True
        return False

    active = context.bot_data.get("active_downloads", {})
    queues = context.bot_data.get("download_queues", {})

    if _update(active.values()):
        return
    for q in queues.values():
        if _update(q):
            return


async def _move_existing_movies(root, dest, movies_list):
    count = 0
    if not os.path.exists(root):
        return 0

    targets = []
    for m in movies_list:
        targets.append((m["title"].lower().strip(), m.get("year")))

    for item in os.listdir(root):
        full_path = os.path.join(root, item)
        if not os.path.isfile(full_path):
            continue

        if os.path.dirname(full_path) == dest:
            continue

        parsed = parse_torrent_name(item)
        if parsed.get("type") != "movie":
            continue

        parsed_title = parsed.get("title", "").lower().strip()
        parsed_year = parsed.get("year")

        matched = False
        for t_title, t_year in targets:
            if _normalize_title(parsed_title) == _normalize_title(t_title):
                if t_year and parsed_year:
                    if int(t_year) != int(parsed_year):
                        continue
                matched = True
                break

        if matched:
            try:
                shutil.move(full_path, os.path.join(dest, item))
                count += 1
            except Exception as e:
                logger.error(f"Failed to move {item}: {e}")

    return count


def _normalize_title(title):
    return re.sub(r"[^a-z0-9]", "", title.lower())


def _is_movie_present(folder, title, year):
    if not os.path.exists(folder):
        return False
    target_norm = _normalize_title(title)

    for item in os.listdir(folder):
        parsed = parse_torrent_name(item)
        if parsed.get("type") != "movie":
            continue

        parsed_title = _normalize_title(parsed.get("title", ""))
        if parsed_title == target_norm:
            if year and parsed.get("year"):
                if int(year) != int(parsed.get("year")):
                    continue
            return True
    return False


def _is_same_movie(t1, y1, t2, y2):
    if not t1 or not t2:
        return False
    return _normalize_title(t1) == _normalize_title(t2) and (
        not y1 or not y2 or int(y1) == int(y2)
    )


def _select_best_candidate(results, target_res):
    if not results:
        return None
    same_res = [r for r in results if target_res.lower() in r.get("title", "").lower()]
    if same_res:
        return same_res[0]
    return results[0]


async def _queue_franchise_item(context, result, dest_folder, chat_id, title, year):
    link = result.get("page_url") or result.get("magnet")
    if not link:
        return

    parsed = parse_torrent_name(result.get("title", ""))
    parsed["title"] = title
    if year:
        parsed["year"] = year
    parsed["destination_folder"] = dest_folder
    parsed["type"] = "movie"
    parsed["collection_mode"] = True

    msg = await safe_send_message(
        context.bot,
        chat_id,
        f"⬇️ Queuing *{escape_markdown(title, version=2)}*\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    source_dict = {
        "value": link,
        "type": "magnet" if link.startswith("magnet:") else "url",
        "parsed_info": parsed,
        "clean_name": f"{title} ({year})" if year else title,
        "original_message_id": msg.message_id,
    }

    download_data = {
        "source_dict": source_dict,
        "chat_id": chat_id,
        "message_id": msg.message_id,
        "save_path": context.bot_data.get("SAVE_PATHS", {}).get("default"),
    }

    queues = context.bot_data.get("download_queues", {})
    chat_str = str(chat_id)
    if chat_str not in queues:
        queues[chat_str] = []
    queues[chat_str].append(download_data)

    await process_queue_for_user(chat_id, context.application)
