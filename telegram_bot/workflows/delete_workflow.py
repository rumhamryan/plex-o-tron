# telegram_bot/workflows/delete_workflow.py

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

from plexapi.server import PlexServer
from plexapi.exceptions import NotFound, Unauthorized

from telegram import (
    Update,
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest, TimedOut
from telegram.helpers import escape_markdown

from ..config import DELETION_ENABLED, logger
from ..services.search_logic import (
    find_media_by_name,
    find_season_directory,
    find_episode_file,
)
from ..utils import format_bytes, safe_edit_message, safe_send_message

if TYPE_CHECKING:
    from plexapi.video import Movie, Episode, Show, Season


BUTTON_LABEL_MAX_LEN = 48
UNKNOWN_SIZE_LABEL = "Unknown size"


class PlexDeleteResult(TypedDict, total=False):
    status: Literal["success", "skip", "not_found", "error"]
    detail: str
    plex_deleted: bool
    plex_items_deleted: int


def _get_display_name(path: str) -> str:
    """Return a stable display name for a file/folder path."""
    normalized = path.rstrip(os.sep)
    base_name = os.path.basename(normalized)
    return base_name or normalized


def _format_size_label(path: str) -> str:
    """Return a short size label (in GB) for display purposes."""
    try:
        size_bytes = _calculate_path_size(path)
    except OSError as exc:
        logger.info("Unable to determine file size for '%s': %s", path, exc)
        return UNKNOWN_SIZE_LABEL

    if size_bytes <= 0:
        return "0 GB"

    size_in_gb = round(size_bytes / (1024**3), 2)
    if size_in_gb == 0:
        return format_bytes(size_bytes)
    return f"{size_in_gb} GB"


def _calculate_path_size(path: str) -> int:
    """Return the total size for files or directories."""
    target = Path(path)
    if not target.exists():
        raise OSError(f"Path does not exist: {path}")

    if target.is_file():
        return target.stat().st_size

    if target.is_dir():
        total = 0
        stack = [target]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as iterator:
                    for entry in iterator:
                        try:
                            if entry.is_file(follow_symlinks=False):
                                total += entry.stat(follow_symlinks=False).st_size
                            elif entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                        except OSError as entry_exc:
                            logger.info(
                                "Skipping unreadable entry '%s': %s",
                                entry.path,
                                entry_exc,
                            )
            except OSError as dir_exc:
                logger.info(
                    "Unable to scan directory '%s': %s",
                    current,
                    dir_exc,
                    exc_info=False,
                )
        return total

    return 0


def _compose_button_label(path: str) -> str:
    base_name = _get_display_name(path)
    size_label = _format_size_label(path)
    candidate = f"{base_name} | {size_label}"
    if len(candidate) <= BUTTON_LABEL_MAX_LEN:
        return candidate

    fallback = f"{base_name} | {UNKNOWN_SIZE_LABEL}"
    if len(fallback) <= BUTTON_LABEL_MAX_LEN:
        return fallback

    suffix = f" | {UNKNOWN_SIZE_LABEL}"
    available = BUTTON_LABEL_MAX_LEN - len(suffix)
    if available <= 1:
        truncated_name = base_name[: max(0, available)]
        return f"{truncated_name}{suffix}"

    truncated_name = base_name[: available - 1] + "…"
    return f"{truncated_name}{suffix}"


def _has_name_twin(path: str) -> bool:
    """Detect whether another file in the same directory shares the same stem."""
    target = Path(path)
    if not target.exists() or not target.is_file():
        return False

    normalized = target.stem.casefold()
    try:
        for sibling in target.parent.iterdir():
            if sibling == target or not sibling.is_file():
                continue
            if sibling.stem.casefold() == normalized:
                logger.info(
                    "Detected name twin for '%s' inside '%s'.",
                    target.name,
                    target.parent,
                )
                return True
    except FileNotFoundError:
        # Directory disappeared mid-check; treat as no twin.
        return False
    except OSError as exc:
        logger.error(
            "Unable to inspect sibling files for '%s': %s",
            target.name,
            exc,
            exc_info=True,
        )
        raise RuntimeError(
            f"Unable to inspect sibling files for '{target.name}'. Please try again."
        ) from exc

    return False


async def _delete_from_filesystem(path: str) -> tuple[bool, str]:
    """Remove a file or directory from disk."""

    def _remove() -> str:
        if os.path.isfile(path):
            os.remove(path)
            return "file"
        if os.path.isdir(path):
            shutil.rmtree(path)
            return "directory"
        raise FileNotFoundError(path)

    try:
        removed_kind = await asyncio.to_thread(_remove)
        logger.info("Removed %s from filesystem: %s", removed_kind, path)
        return True, removed_kind
    except FileNotFoundError:
        logger.warning(
            "Filesystem deletion skipped because the path no longer exists: %s", path
        )
        return False, "missing"
    except Exception as exc:
        logger.error(
            "Filesystem deletion failed for '%s': %s", path, exc, exc_info=True
        )
        return False, str(exc)


async def _delete_plex_collection(plex: PlexServer, collection_name: str) -> bool:
    """Delete a Plex collection by name, if it exists."""
    if not collection_name:
        return False

    normalized = collection_name.strip()
    if not normalized:
        return False

    try:
        for section in plex.library.sections():
            if getattr(section, "type", None) != "movie":
                continue
            try:
                collection = section.collection(normalized)
            except NotFound:
                continue

            await asyncio.to_thread(collection.delete)
            logger.info("Deleted Plex collection '%s'.", normalized)
            return True
    except Exception as exc:
        logger.error(
            "Failed to delete Plex collection '%s': %s", normalized, exc, exc_info=True
        )
        raise

    logger.info(
        "No Plex collection named '%s' was found across the configured sections.",
        normalized,
    )
    return False


def _find_media_in_plex_by_path(
    plex: PlexServer, path_to_delete: str
) -> Movie | Episode | Show | Season | None:
    """
    Scans all Plex libraries to find the media item that corresponds to a given file or directory path.

    This is a blocking function and must be run in a separate thread.
    """
    # Normalize paths to be safe
    path_to_delete = os.path.abspath(path_to_delete)

    # Iterate through all video libraries
    for section in plex.library.sections():
        if section.type in ["movie", "show"]:
            logger.info(
                f"Searching for path '{path_to_delete}' in Plex library '{section.title}'..."
            )

            # Use the .search() method for efficiency where possible, but we need to check file paths,
            # which often requires iterating.
            for item in section.all():
                # For Shows, Seasons, or Movies (items with locations)
                if hasattr(item, "locations"):
                    for location in item.locations:
                        if os.path.abspath(location) == path_to_delete:
                            logger.info(f"Found match for directory: {item.title}")
                            return item  # Found a match for a whole Show or Movie directory

                # For individual episodes
                if hasattr(item, "media"):
                    for media in item.media:
                        for part in media.parts:
                            if os.path.abspath(part.file) == path_to_delete:
                                logger.info(f"Found match for file: {item.title}")
                                return item  # Found a match for an Episode file

    logger.warning(f"Could not find any item in Plex matching path: {path_to_delete}")
    return None


async def _delete_item_from_plex(
    path_to_delete: str, plex_config: dict
) -> tuple[PlexDeleteResult, PlexServer | None]:
    """
    Connects to Plex, finds a media item by its path, and deletes it via the API.
    Returns a structured result plus the Plex connection for downstream use.
    """
    plex: PlexServer | None = None
    try:
        plex = await asyncio.to_thread(
            PlexServer, plex_config["url"], plex_config["token"]
        )
    except Unauthorized:
        return (
            {"status": "error", "detail": "Plex authentication failed."},
            None,
        )
    except Exception as exc:
        logger.error(
            "Plex connection failed during deletion request: %s", exc, exc_info=True
        )
        return (
            {
                "status": "error",
                "detail": f"An error occurred while communicating with Plex: {exc}",
            },
            None,
        )

    if plex is None:
        logger.error("Plex connection returned None during deletion.")
        return (
            {
                "status": "error",
                "detail": "Plex connection failed unexpectedly.",
            },
            None,
        )

    plex_server: PlexServer = plex

    abs_path = os.path.abspath(path_to_delete)
    if os.path.isfile(abs_path):
        try:
            if _has_name_twin(abs_path):
                detail = "Skipped Plex deletion because another encode with the same name exists in this directory."
                logger.info("%s", detail)
                return (
                    {"status": "skip", "detail": detail, "plex_deleted": False},
                    plex_server,
                )
        except RuntimeError as exc:
            return ({"status": "error", "detail": str(exc)}, plex_server)

    try:
        if os.path.isdir(abs_path):
            logger.info(
                "Directory delete requested. Searching Plex movies under: %s",
                abs_path,
            )
            items_to_delete = []
            seen_keys: set[int] = set()

            for section in plex_server.library.sections():
                if getattr(section, "type", None) != "movie":
                    continue
                for item in section.all():
                    try:
                        for media in getattr(item, "media", []) or []:
                            for part in getattr(media, "parts", []) or []:
                                part_path = os.path.abspath(getattr(part, "file", ""))
                                if part_path.startswith(abs_path + os.sep):
                                    rk = getattr(item, "ratingKey", None)
                                    if rk is not None and rk not in seen_keys:
                                        seen_keys.add(rk)
                                        items_to_delete.append(item)
                                    break
                    except Exception:
                        continue

            if items_to_delete:
                base_name = os.path.basename(abs_path)
                logger.info(
                    "Found %d Plex movie(s) under collection folder '%s'. Deleting...",
                    len(items_to_delete),
                    base_name,
                )
                for it in items_to_delete:
                    await asyncio.to_thread(it.delete)

                return (
                    {
                        "status": "success",
                        "detail": f"Deleted {len(items_to_delete)} Plex movie(s) linked to '{base_name}'.",
                        "plex_deleted": True,
                        "plex_items_deleted": len(items_to_delete),
                    },
                    plex_server,
                )

        plex_item = await asyncio.to_thread(
            _find_media_in_plex_by_path, plex_server, abs_path
        )

        if not plex_item:
            return (
                {
                    "status": "not_found",
                    "detail": "Could not find the item in Plex. It may have already been removed.",
                },
                plex_server,
            )

        display_name = plex_item.title
        logger.info("Found Plex item '%s'. Attempting API deletion...", display_name)
        await asyncio.to_thread(plex_item.delete)
        logger.info("Successfully deleted '%s' via Plex API.", display_name)
        return (
            {
                "status": "success",
                "detail": f"Deleted Plex item '{display_name}'.",
                "plex_deleted": True,
                "plex_items_deleted": 1,
            },
            plex_server,
        )

    except Exception as exc:
        logger.error(
            "An unexpected error occurred during Plex deletion: %s", exc, exc_info=True
        )
        return (
            {
                "status": "error",
                "detail": f"An error occurred while communicating with Plex: {exc}",
            },
            plex_server,
        )


async def handle_delete_workflow(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Manages the text-based replies in the multi-step conversation for deleting media.
    """
    if not isinstance(update.message, Message) or not update.message.text:
        return
    if context.user_data is None:
        context.user_data = {}

    chat_id = update.message.chat_id
    text = update.message.text.strip()
    next_action = context.user_data.get("next_action", "")
    status_message: Message | None = None

    # Clean up the user's message and the bot's last prompt
    prompt_message_id = context.user_data.pop("prompt_message_id", None)
    try:
        await update.message.delete()
        if prompt_message_id:
            await context.bot.delete_message(
                chat_id=chat_id, message_id=prompt_message_id
            )
    except (BadRequest, TimedOut):
        pass

    save_paths = context.bot_data.get("SAVE_PATHS", {})
    escaped_text = escape_markdown(text, version=2)

    # Route based on the expected next action
    if next_action == "delete_movie_collection_search":
        # FIX: Escape the trailing periods
        status_message = await safe_send_message(
            context.bot,
            chat_id,
            f"🔎 Searching for movie collection: `{escaped_text}`\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        found = await find_media_by_name("movie", text, save_paths, "directory")
        await _present_delete_results(
            found,
            status_message,
            "movie collection",
            text,
            context,
            "movie_collection",
        )
        context.user_data.pop("next_action", None)

    elif next_action == "delete_movie_single_search":
        # FIX: Escape the trailing periods
        status_message = await safe_send_message(
            context.bot,
            chat_id,
            f"🔎 Searching for single movie: `{escaped_text}`\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        found = await find_media_by_name("movie", text, save_paths, "file")
        await _present_delete_results(
            found,
            status_message,
            "single movie",
            text,
            context,
            "movie_file",
        )
        context.user_data.pop("next_action", None)

    elif next_action == "delete_tv_show_search":
        # FIX: Escape the trailing periods
        status_message = await safe_send_message(
            context.bot,
            chat_id,
            f"🔎 Searching for TV show: `{escaped_text}`\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        found_path = await find_media_by_name("tv_shows", text, save_paths)
        if isinstance(found_path, str):
            context.user_data["show_path_to_delete"] = found_path
            base_name = os.path.basename(found_path)
            keyboard = [
                [InlineKeyboardButton("🗑️ All", callback_data="delete_tv_all")],
                [InlineKeyboardButton("💿 Season", callback_data="delete_tv_season")],
                [InlineKeyboardButton("▶️ Episode", callback_data="delete_tv_episode")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
            ]
            await safe_edit_message(
                status_message,
                text=f"Found show: `{escape_markdown(base_name)}`\\.\n\nWhat would you like to delete\\?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await safe_edit_message(
                status_message,
                text=f"❌ No single TV show directory found matching: `{escaped_text}`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        context.user_data.pop("next_action", None)

    elif next_action == "delete_tv_season_search":
        # FIX: Escape the trailing periods
        status_message = await safe_send_message(
            context.bot,
            chat_id,
            f"🔎 Searching for season `{escaped_text}`\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        show_path = context.user_data.get("show_path_to_delete")
        if show_path and text.isdigit():
            found_path = await find_season_directory(show_path, int(text))
            await _present_delete_results(
                found_path,
                status_message,
                f"Season {text}",
                text,
                context,
                "tv_season",
            )
        else:
            await safe_edit_message(
                status_message,
                text="❌ Invalid input or show context lost. Please start over\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        context.user_data.pop("next_action", None)

    elif next_action == "delete_tv_episode_season_prompt":
        if text.isdigit():
            context.user_data["season_to_delete_num"] = int(text)
            context.user_data["next_action"] = "delete_tv_episode_episode_prompt"
            new_prompt = await safe_send_message(
                context.bot,
                chat_id,
                f"📺 Season {escaped_text} selected\\. Now, please send the episode number to delete\\.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "❌ Cancel", callback_data="cancel_operation"
                            )
                        ]
                    ]
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            context.user_data["prompt_message_id"] = new_prompt.message_id
        else:
            await safe_send_message(
                context.bot, chat_id, "❌ Invalid season number. Please start over."
            )

    elif next_action == "delete_tv_episode_episode_prompt":
        # FIX: Escape the trailing periods
        status_message = await safe_send_message(
            context.bot,
            chat_id,
            f"🔎 Searching for episode `{escaped_text}`\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        show_path = context.user_data.get("show_path_to_delete")
        season_num = context.user_data.get("season_to_delete_num")
        if show_path and season_num and text.isdigit():
            season_path = await find_season_directory(show_path, season_num)
            if season_path:
                found_path = await find_episode_file(season_path, season_num, int(text))
                await _present_delete_results(
                    found_path,
                    status_message,
                    f"S{season_num}E{text}",
                    text,
                    context,
                    "tv_episode",
                )
            else:
                await safe_edit_message(
                    status_message,
                    text=f"❌ Could not find Season {season_num} to look for the episode in\\.",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
        else:
            await safe_edit_message(
                status_message,
                text="❌ Invalid input or context lost\\. Please start over\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        context.user_data.pop("next_action", None)


async def handle_delete_buttons(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handles all button presses related to the delete workflow."""
    query = update.callback_query
    if (
        not query
        or not query.data
        or not isinstance(query.message, Message)
        or context.user_data is None
    ):
        return

    action = query.data

    if action.startswith("delete_start_"):
        await _handle_start_buttons(query, context)
    elif action.startswith("delete_movie_"):
        await _handle_movie_type_buttons(query, context)
    elif action.startswith("delete_tv_"):
        await _handle_tv_scope_buttons(query, context)
    elif action.startswith("delete_select_"):
        await _handle_selection_button(query, context)
    elif action == "confirm_delete":
        await _handle_confirm_delete_button(query, context)
    else:
        logger.warning(f"Received unhandled delete callback: {action}")


async def _present_delete_results(
    results: str | list[str] | None,
    status_message: Message,
    media_name: str,
    query_text: str,
    context: ContextTypes.DEFAULT_TYPE,
    delete_target_kind: str,
):
    """Presents single, multiple, or no search results to the user for deletion."""
    if context.user_data is None:
        context.user_data = {}

    if isinstance(results, str):
        context.user_data["path_to_delete"] = results
        context.user_data["delete_target_kind"] = delete_target_kind
        base_name = os.path.basename(results)

        # FIX: Normalize path separators for display
        display_path = results.replace(os.sep, "/")

        message_text = (
            f"Found:\n`{escape_markdown(base_name, version=2)}`\n\n"
            f"*Path:*\n`{escape_markdown(display_path, version=2)}`\n\n"
            f"Are you sure you want to permanently delete this item\\?"
        )
        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Yes, Delete It", callback_data="confirm_delete"
                ),
                InlineKeyboardButton("❌ No, Cancel", callback_data="cancel_operation"),
            ]
        ]
        await safe_edit_message(
            status_message,
            text=message_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    elif isinstance(results, list):
        context.user_data["selection_choices"] = results
        context.user_data["selection_target_kind"] = delete_target_kind
        keyboard = []
        for i, path in enumerate(results):
            button_text = _compose_button_label(path)
            keyboard.append(
                [
                    InlineKeyboardButton(
                        button_text,
                        callback_data=f"delete_select_{i}",
                    )
                ]
            )
        keyboard.append(
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]
        )
        await safe_edit_message(
            status_message,
            text="Multiple matches found\\. Which one do you want to delete\\?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    else:
        await safe_edit_message(
            status_message,
            text=f"❌ No {media_name} found matching: `{escape_markdown(query_text, version=2)}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def _handle_start_buttons(query, context):
    """Handles 'Movie' or 'TV Show' selection."""
    if query.data == "delete_start_movie":
        message_text = (
            "Delete a full movie collection \\(folder\\) or a single movie file\\?"
        )
        keyboard = [
            [
                InlineKeyboardButton(
                    "🗂️ Collection", callback_data="delete_movie_collection"
                ),
                InlineKeyboardButton(
                    "📄 Single File", callback_data="delete_movie_single"
                ),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation"),
            ]
        ]
    else:  # delete_start_tv
        context.user_data["next_action"] = "delete_tv_show_search"
        message_text = "📺 Please send me the title of the TV show to delete\\."
        keyboard = [
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]
        ]

    await safe_edit_message(
        query.message,
        text=message_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    context.user_data["prompt_message_id"] = query.message.message_id


async def _handle_movie_type_buttons(query, context):
    """Handles 'Collection' or 'Single' movie selection."""
    if query.data == "delete_movie_collection":
        context.user_data["next_action"] = "delete_movie_collection_search"
        message_text = (
            "🎬 Please send the title of the movie collection \\(folder\\) to delete\\."
        )
    else:  # delete_movie_single
        context.user_data["next_action"] = "delete_movie_single_search"
        message_text = (
            "🎬 Please send the title of the single movie \\(file\\) to delete\\."
        )

    await safe_edit_message(
        query.message,
        text=message_text,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    context.user_data["prompt_message_id"] = query.message.message_id


async def _handle_tv_scope_buttons(query, context):
    """Handles 'All', 'Season', or 'Episode' selection for a TV show."""
    show_path = context.user_data.get("show_path_to_delete")
    if not show_path:
        await safe_edit_message(
            query.message,
            "❌ Error: Show context lost\\. Please start over\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if query.data == "delete_tv_all":
        context.user_data["path_to_delete"] = show_path
        context.user_data["delete_target_kind"] = "tv_show"
        base_name = os.path.basename(show_path)

        display_path = show_path.replace(os.sep, "/")

        message_text = (
            f"Are you sure you want to delete the ENTIRE show `{escape_markdown(base_name, version=2)}` and all its contents\\?\n\n"
            f"*Path:*\n`{escape_markdown(display_path, version=2)}`"
        )
        reply_markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Yes, Delete All", callback_data="confirm_delete"
                    ),
                    InlineKeyboardButton(
                        "❌ No, Cancel", callback_data="cancel_operation"
                    ),
                ]
            ]
        )
        await safe_edit_message(
            query.message,
            text=message_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    elif query.data == "delete_tv_season":
        context.user_data["next_action"] = "delete_tv_season_search"
        message_text = "💿 Please send me the season number to delete\\."
        reply_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
        )
        await safe_edit_message(
            query.message,
            text=message_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        context.user_data["prompt_message_id"] = query.message.message_id

    elif query.data == "delete_tv_episode":
        context.user_data["next_action"] = "delete_tv_episode_season_prompt"
        message_text = "📺 First, please send the season number\\."
        reply_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
        )
        await safe_edit_message(
            query.message,
            text=message_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        context.user_data["prompt_message_id"] = query.message.message_id


async def _handle_selection_button(query, context):
    """Handles the user choosing one item from a list of multiple matches."""
    choices = context.user_data.pop("selection_choices", [])
    try:
        index = int(query.data.split("_")[2])
        path_to_delete = choices[index]
        context.user_data["path_to_delete"] = path_to_delete
        target_kind = context.user_data.pop("selection_target_kind", None)
        if target_kind:
            context.user_data["delete_target_kind"] = target_kind
        base_name = os.path.basename(path_to_delete)
        message_text = (
            f"You selected:\n`{escape_markdown(base_name)}`\n\n"
            f"Are you sure you want to permanently delete this item\\?"
        )
        reply_markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Yes, Delete It", callback_data="confirm_delete"
                    ),
                    InlineKeyboardButton(
                        "❌ No, Cancel", callback_data="cancel_operation"
                    ),
                ]
            ]
        )
        await safe_edit_message(
            query.message,
            text=message_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except (ValueError, IndexError):
        await safe_edit_message(
            query.message,
            text="❌ Error: Could not process selection\\. Please start over\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def _handle_confirm_delete_button(query, context):
    """Handles the final 'Yes, Delete' confirmation using the Plex API first."""
    path_to_delete = context.user_data.pop("path_to_delete", None)
    delete_target_kind = context.user_data.get("delete_target_kind")
    plex_config = context.bot_data.get("PLEX_CONFIG")
    message_text = ""
    plex: PlexServer | None = None

    if not path_to_delete:
        message_text = (
            "❌ Error: Path to delete not found\\. The action may have expired\\."
        )
    elif not DELETION_ENABLED:
        logger.warning(
            f"Deletion attempted for '{path_to_delete}' but DELETION_ENABLED is False."
        )
        message_text = "ℹ️ *Deletion Confirmed*\n\n(Note: Actual file deletion is disabled by the administrator)"
    elif plex_config:
        display_name = _get_display_name(path_to_delete)
        escaped_name = escape_markdown(display_name, version=2)
        size_label = _format_size_label(path_to_delete)
        escaped_size = escape_markdown(size_label, version=2)

        def _format_item_line(prefix: str) -> str:
            return f"{prefix}\n{escaped_name}\nSize: {escaped_size}"

        await safe_edit_message(
            query.message,
            text="Connecting to Plex and attempting to delete the item...",
            reply_markup=None,
        )

        result, plex = await _delete_item_from_plex(path_to_delete, plex_config)
        status = result.get("status", "error")
        detail = result.get("detail", "")

        if status == "success":
            message_text = _format_item_line("🗑️ *Successfully Deleted from Plex*")
        elif status == "skip":
            logger.info(
                "Plex deletion skipped for '%s'; falling back to filesystem removal.",
                path_to_delete,
            )
            manual_success, manual_detail = await _delete_from_filesystem(
                path_to_delete
            )
            if manual_success:
                message_text = (
                    _format_item_line("⚠️ *Plex Skipped, Removed From Disk*")
                    + "\n"
                    + "Plex library left untouched because other encodes still exist\\."
                )
            else:
                if manual_detail == "missing":
                    message_text = "❌ *Deletion Failed*\nThe path no longer exists on the server\\."
                else:
                    message_text = "❌ *Manual Deletion Failed*\n`{}`".format(
                        escape_markdown(manual_detail, version=2)
                    )
        elif status == "not_found":
            logger.warning(
                "Item not found in Plex, falling back to manual filesystem deletion."
            )
            manual_success, manual_detail = await _delete_from_filesystem(
                path_to_delete
            )
            if manual_success:
                message_text = _format_item_line(
                    "🗑️ *Item Not in Plex, Deleted Manually*"
                )
            else:
                if manual_detail == "missing":
                    message_text = "❌ *Deletion Failed*\nThe path no longer exists on the server\\."
                else:
                    message_text = "❌ *Manual Deletion Failed*\n`{}`".format(
                        escape_markdown(manual_detail, version=2)
                    )
        else:
            message_text = "❌ *Deletion Failed*\n`{}`".format(
                escape_markdown(detail or "Unknown error", version=2)
            )

        filesystem_deleted = not os.path.exists(path_to_delete)
        if (
            filesystem_deleted
            and delete_target_kind == "movie_collection"
            and plex is not None
        ):
            base_collection_name = _get_display_name(path_to_delete)
            try:
                await _delete_plex_collection(plex, base_collection_name)
            except Exception as exc:
                message_text += "\n" + "⚠️ Failed to delete Plex collection\\."
                logger.error(
                    "Failed to delete Plex collection '%s': %s",
                    base_collection_name,
                    exc,
                    exc_info=True,
                )
    else:
        message_text = "❌ *Plex Not Configured*\nCannot perform a library-aware delete. Please configure Plex in your `config.ini` file\\."

    # Clear the user's conversational context
    keys_to_clear = [
        "show_path_to_delete",
        "next_action",
        "prompt_message_id",
        "season_to_delete_num",
        "selection_choices",
        "selection_target_kind",
        "active_workflow",
        "path_to_delete",
        "delete_target_kind",
    ]
    for key in keys_to_clear:
        context.user_data.pop(key, None)

    await safe_edit_message(
        query.message,
        text=message_text,
        reply_markup=None,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
