# telegram_bot/workflows/delete_workflow.py

from __future__ import annotations

import asyncio
import os
import shutil
from typing import List, Optional, Union, Tuple, TYPE_CHECKING

from plexapi.server import PlexServer
from plexapi.exceptions import Unauthorized

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
from ..utils import safe_edit_message

if TYPE_CHECKING:
    from plexapi.video import Movie, Episode, Show, Season


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
    path_to_delete: str, plex_config: dict, context: ContextTypes.DEFAULT_TYPE
) -> tuple[bool, str]:
    """
    Connects to Plex, finds a media item by its path, and deletes it via the API.
    Returns (success_boolean, status_message).
    """
    try:
        # Connect to Plex in a non-blocking way
        plex = await asyncio.to_thread(
            PlexServer, plex_config["url"], plex_config["token"]
        )

        # Find the corresponding item in Plex
        plex_item = await asyncio.to_thread(
            _find_media_in_plex_by_path, plex, path_to_delete
        )

        if not plex_item:
            return (
                False,
                "Could not find the item in your Plex library. It may have been already deleted or never scanned.",
            )

        display_name = plex_item.title
        logger.info(f"Found Plex item '{display_name}'. Attempting API deletion...")

        # Perform the deletion
        await asyncio.to_thread(plex_item.delete)

        # Success!
        message = f"🗑️ *Successfully Deleted from Plex*\n`{escape_markdown(display_name, version=2)}`"
        logger.info(f"Successfully deleted '{display_name}' via Plex API.")
        return True, message

    except Unauthorized:
        return False, "Plex authentication failed. Please check your token."
    except Exception as e:
        logger.error(
            f"An unexpected error occurred during Plex deletion: {e}", exc_info=True
        )
        return (
            False,
            f"An error occurred while communicating with Plex:\n`{escape_markdown(str(e), version=2)}`",
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
        status_message = await context.bot.send_message(
            chat_id,
            f"🔎 Searching for movie collection: `{escaped_text}`\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        found = await find_media_by_name("movie", text, save_paths, "directory")
        await _present_delete_results(
            found, status_message, "movie collection", text, context
        )
        context.user_data.pop("next_action", None)

    elif next_action == "delete_movie_single_search":
        # FIX: Escape the trailing periods
        status_message = await context.bot.send_message(
            chat_id,
            f"🔎 Searching for single movie: `{escaped_text}`\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        found = await find_media_by_name("movie", text, save_paths, "file")
        await _present_delete_results(
            found, status_message, "single movie", text, context
        )
        context.user_data.pop("next_action", None)

    elif next_action == "delete_tv_show_search":
        # FIX: Escape the trailing periods
        status_message = await context.bot.send_message(
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
        status_message = await context.bot.send_message(
            chat_id,
            f"🔎 Searching for season `{escaped_text}`\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        show_path = context.user_data.get("show_path_to_delete")
        if show_path and text.isdigit():
            found_path = await find_season_directory(show_path, int(text))
            await _present_delete_results(
                found_path, status_message, f"Season {text}", text, context
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
            new_prompt = await context.bot.send_message(
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
            await context.bot.send_message(
                chat_id, "❌ Invalid season number. Please start over."
            )

    elif next_action == "delete_tv_episode_episode_prompt":
        # FIX: Escape the trailing periods
        status_message = await context.bot.send_message(
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
                    found_path, status_message, f"S{season_num}E{text}", text, context
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
):
    """Presents single, multiple, or no search results to the user for deletion."""
    if context.user_data is None:
        context.user_data = {}

    if isinstance(results, str):
        context.user_data["path_to_delete"] = results
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
        keyboard = []
        for i, path in enumerate(results):
            keyboard.append(
                [
                    InlineKeyboardButton(
                        os.path.basename(path), callback_data=f"delete_select_{i}"
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
    plex_config = context.bot_data.get("PLEX_CONFIG")
    message_text = ""

    if not path_to_delete:
        message_text = "❌ Error: Path to delete not found. The action may have expired."
    elif not DELETION_ENABLED:
        logger.warning(
            f"Deletion attempted for '{path_to_delete}' but DELETION_ENABLED is False."
        )
        message_text = "ℹ️ *Deletion Confirmed*\n\n(Note: Actual file deletion is disabled by the administrator)"
    elif plex_config:
        await safe_edit_message(
            query.message,
            text="Connecting to Plex and attempting to delete the item...",
            reply_markup=None,
        )

        success, message_text = await _delete_item_from_plex(
            path_to_delete, plex_config, context
        )

        if not success and "Could not find" in message_text:
            # Fallback to manual deletion if not found in Plex
            logger.warning(
                "Item not found in Plex, falling back to manual filesystem deletion."
            )
            try:
                if os.path.exists(path_to_delete):
                    display_name = os.path.basename(path_to_delete)
                    if os.path.isfile(path_to_delete):
                        await asyncio.to_thread(os.remove, path_to_delete)
                    elif os.path.isdir(path_to_delete):
                        await asyncio.to_thread(shutil.rmtree, path_to_delete)
                    message_text = f"🗑️ *Item Not in Plex, Deleted Manually*\n`{escape_markdown(display_name, version=2)}`"
                else:
                    message_text = (
                        "❌ *Deletion Failed*\nThe path no longer exists on the server."
                    )
            except Exception as e:
                message_text = f"❌ *Manual Deletion Failed*\n`{escape_markdown(str(e), version=2)}`"
    else:
        message_text = "❌ *Plex Not Configured*\nCannot perform a library-aware delete. Please configure Plex in your `config.ini` file."

    # Clear the user's conversational context
    keys_to_clear = [
        "show_path_to_delete",
        "next_action",
        "prompt_message_id",
        "season_to_delete_num",
        "selection_choices",
        "active_workflow",
        "path_to_delete",
    ]
    for key in keys_to_clear:
        context.user_data.pop(key, None)

    await safe_edit_message(
        query.message,
        text=message_text,
        reply_markup=None,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
