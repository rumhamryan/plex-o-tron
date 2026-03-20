# telegram_bot/workflows/delete_workflow/handlers.py

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from plexapi.server import PlexServer
from telegram import (
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TimedOut
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from ...config import DELETION_ENABLED, logger
from ...services.search_logic import (
    find_episode_file,
    find_media_by_name,
    find_season_directory,
)
from ...ui.keyboards import (
    cancel_only_keyboard,
    confirm_cancel_keyboard,
    stacked_choice_keyboard,
)
from ...ui.messages import format_media_summary
from ...utils import safe_edit_message, safe_send_message
from ..navigation import mark_chat_workflow_active, return_to_home, set_active_prompt_message_id

if TYPE_CHECKING:
    pass

from .filesystem import _delete_from_filesystem
from .helpers import (
    _format_manual_delete_failure,
    _format_size_label,
    _get_display_name,
    _has_valid_plex_credentials,
)
from .plex import _delete_item_from_plex, _delete_plex_collection
from .selection import _present_delete_results


def _truncate_log_text(value: str, limit: int = 120) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3]}..."


def _log_delete_event(message: Message | None, event: str, **fields: object) -> None:
    chat_id = getattr(message, "chat_id", None)
    from_user = getattr(message, "from_user", None)
    user_id = getattr(from_user, "id", None)
    details = [f"event={event}", f"chat_id={chat_id}", f"user_id={user_id}"]
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, str):
            rendered_value = _truncate_log_text(value)
        else:
            rendered_value = str(value)
        details.append(f"{key}={rendered_value}")
    logger.info("[DELETE] %s" % " ".join(details))


def _log_delete_search_outcome(
    message: Message,
    *,
    target_kind: str,
    query_text: str,
    results: str | list[str] | None,
) -> None:
    if isinstance(results, str):
        _log_delete_event(
            message,
            "search_completed",
            target_kind=target_kind,
            query=query_text,
            result="single_match",
            matches=1,
            path=results,
        )
        return
    if isinstance(results, list):
        _log_delete_event(
            message,
            "search_completed",
            target_kind=target_kind,
            query=query_text,
            result="multiple_matches",
            matches=len(results),
        )
        return
    _log_delete_event(
        message,
        "search_completed",
        target_kind=target_kind,
        query=query_text,
        result="no_match",
        matches=0,
    )


async def handle_delete_workflow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            await context.bot.delete_message(chat_id=chat_id, message_id=prompt_message_id)
    except (BadRequest, TimedOut):
        pass

    save_paths = context.bot_data.get("SAVE_PATHS", {})
    escaped_text = escape_markdown(text, version=2)

    # Route based on the expected next action
    if next_action == "delete_movie_collection_search":
        _log_delete_event(
            update.message,
            "search_requested",
            target_kind="movie_collection",
            query=text,
        )
        # FIX: Escape the trailing periods
        status_message = await safe_send_message(
            context.bot,
            chat_id,
            f"🔎 Searching for movie collection: `{escaped_text}`\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        found = await find_media_by_name("movie", text, save_paths, "directory")
        _log_delete_search_outcome(
            update.message,
            target_kind="movie_collection",
            query_text=text,
            results=found,
        )
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
        _log_delete_event(
            update.message,
            "search_requested",
            target_kind="movie_file",
            query=text,
        )
        # FIX: Escape the trailing periods
        status_message = await safe_send_message(
            context.bot,
            chat_id,
            f"🔎 Searching for single movie: `{escaped_text}`\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        found = await find_media_by_name("movie", text, save_paths, "file")
        _log_delete_search_outcome(
            update.message,
            target_kind="movie_file",
            query_text=text,
            results=found,
        )
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
        _log_delete_event(
            update.message,
            "search_requested",
            target_kind="tv_show",
            query=text,
        )
        # FIX: Escape the trailing periods
        status_message = await safe_send_message(
            context.bot,
            chat_id,
            f"🔎 Searching for TV show: `{escaped_text}`\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        found_path = await find_media_by_name("tv_shows", text, save_paths)
        _log_delete_search_outcome(
            update.message,
            target_kind="tv_show",
            query_text=text,
            results=found_path,
        )
        if isinstance(found_path, str):
            context.user_data["show_path_to_delete"] = found_path
            base_name = os.path.basename(found_path)
            await safe_edit_message(
                status_message,
                text=f"Found show: `{escape_markdown(base_name)}`\\.\n\nWhat would you like to delete\\?",
                reply_markup=stacked_choice_keyboard(
                    [
                        ("🗑️ All", "delete_tv_all"),
                        ("💿 Season", "delete_tv_season"),
                        ("▶️ Episode", "delete_tv_episode"),
                    ]
                ),
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
        _log_delete_event(
            update.message,
            "search_requested",
            target_kind="tv_season",
            query=text,
        )
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
            _log_delete_search_outcome(
                update.message,
                target_kind="tv_season",
                query_text=text,
                results=found_path,
            )
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
            _log_delete_event(
                update.message,
                "episode_season_selected",
                target_kind="tv_episode",
                season=text,
            )
            context.user_data["season_to_delete_num"] = int(text)
            context.user_data["next_action"] = "delete_tv_episode_episode_prompt"
            new_prompt = await safe_send_message(
                context.bot,
                chat_id,
                f"📺 Season {escaped_text} selected\\. Now, please send the episode number to delete\\.",
                reply_markup=cancel_only_keyboard(),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            context.user_data["prompt_message_id"] = new_prompt.message_id
        else:
            _log_delete_event(
                update.message,
                "episode_season_invalid",
                target_kind="tv_episode",
                season=text,
            )
            await safe_send_message(
                context.bot, chat_id, "❌ Invalid season number. Please start over."
            )

    elif next_action == "delete_tv_episode_episode_prompt":
        _log_delete_event(
            update.message,
            "search_requested",
            target_kind="tv_episode",
            query=text,
        )
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
                _log_delete_search_outcome(
                    update.message,
                    target_kind="tv_episode",
                    query_text=f"S{season_num:02d}E{int(text):02d}",
                    results=found_path,
                )
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


async def handle_delete_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def _handle_start_buttons(query, context):
    """Handles 'Movie' or 'TV Show' selection."""
    mark_chat_workflow_active(context, query.message.chat_id, "delete")
    if query.data == "delete_start_movie":
        _log_delete_event(query.message, "workflow_started", media_type="movie")
        message_text = "Delete a full movie collection \\(folder\\) or a single movie file\\?"
        reply_markup = stacked_choice_keyboard(
            [
                ("🗂️ Collection", "delete_movie_collection"),
                ("📄 Single File", "delete_movie_single"),
            ]
        )
    else:  # delete_start_tv
        _log_delete_event(query.message, "workflow_started", media_type="tv_show")
        context.user_data["next_action"] = "delete_tv_show_search"
        message_text = "📺 Please send me the title of the TV show to delete\\."
        reply_markup = cancel_only_keyboard()

    await safe_edit_message(
        query.message,
        text=message_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    context.user_data["prompt_message_id"] = query.message.message_id
    set_active_prompt_message_id(context, query.message.chat_id, query.message.message_id)


async def _handle_movie_type_buttons(query, context):
    """Handles 'Collection' or 'Single' movie selection."""
    if query.data == "delete_movie_collection":
        _log_delete_event(query.message, "movie_scope_selected", target_kind="movie_collection")
        context.user_data["next_action"] = "delete_movie_collection_search"
        message_text = "🎬 Please send the title of the movie collection \\(folder\\) to delete\\."
    else:  # delete_movie_single
        _log_delete_event(query.message, "movie_scope_selected", target_kind="movie_file")
        context.user_data["next_action"] = "delete_movie_single_search"
        message_text = "🎬 Please send the title of the single movie \\(file\\) to delete\\."

    await safe_edit_message(
        query.message,
        text=message_text,
        reply_markup=cancel_only_keyboard(),
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
        _log_delete_event(query.message, "tv_scope_selected", target_kind="tv_show", path=show_path)
        context.user_data["path_to_delete"] = show_path
        context.user_data["delete_target_kind"] = "tv_show"
        base_name = os.path.basename(show_path)

        display_path = show_path.replace(os.sep, "/")

        message_text = (
            f"Are you sure you want to delete the ENTIRE show `{escape_markdown(base_name, version=2)}` and all its contents\\?\n\n"
            f"*Path:*\n`{escape_markdown(display_path, version=2)}`"
        )
        reply_markup = confirm_cancel_keyboard(
            "✅ Yes, Delete All",
            "confirm_delete",
            cancel_label="❌ No, Cancel",
        )
        await safe_edit_message(
            query.message,
            text=message_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    elif query.data == "delete_tv_season":
        _log_delete_event(
            query.message, "tv_scope_selected", target_kind="tv_season", path=show_path
        )
        context.user_data["next_action"] = "delete_tv_season_search"
        message_text = "💿 Please send me the season number to delete\\."
        reply_markup = cancel_only_keyboard()
        await safe_edit_message(
            query.message,
            text=message_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        context.user_data["prompt_message_id"] = query.message.message_id

    elif query.data == "delete_tv_episode":
        _log_delete_event(
            query.message, "tv_scope_selected", target_kind="tv_episode", path=show_path
        )
        context.user_data["next_action"] = "delete_tv_episode_season_prompt"
        message_text = "📺 First, please send the season number\\."
        reply_markup = cancel_only_keyboard()
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
        _log_delete_event(
            query.message,
            "selection_made",
            target_kind=context.user_data.get("selection_target_kind"),
            selected_index=index,
            path=path_to_delete,
        )
        context.user_data["path_to_delete"] = path_to_delete
        target_kind = context.user_data.pop("selection_target_kind", None)
        if target_kind:
            context.user_data["delete_target_kind"] = target_kind
        base_name = os.path.basename(path_to_delete)
        message_text = (
            f"You selected:\n`{escape_markdown(base_name)}`\n\n"
            f"Are you sure you want to permanently delete this item\\?"
        )
        reply_markup = confirm_cancel_keyboard(
            "✅ Yes, Delete It",
            "confirm_delete",
            cancel_label="❌ No, Cancel",
        )
        await safe_edit_message(
            query.message,
            text=message_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except (ValueError, IndexError):
        _log_delete_event(query.message, "selection_invalid", callback_data=query.data)
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
    has_valid_plex = _has_valid_plex_credentials(plex_config)
    message_text = ""
    plex: PlexServer | None = None
    outcome = "unknown"

    _log_delete_event(
        query.message,
        "confirm_requested",
        target_kind=delete_target_kind,
        path=path_to_delete,
        plex_enabled=has_valid_plex,
    )

    if not path_to_delete:
        outcome = "missing_path"
        message_text = "❌ Error: Path to delete not found\\. The action may have expired\\."
    elif not DELETION_ENABLED:
        outcome = "deletion_disabled"
        logger.warning(f"Deletion attempted for '{path_to_delete}' but DELETION_ENABLED is False.")
        message_text = "ℹ️ *Deletion Confirmed*\n\n(Note: Actual file deletion is disabled by the administrator)"
    else:
        display_name = _get_display_name(path_to_delete)
        size_label = _format_size_label(path_to_delete)

        def _format_item_line(prefix: str) -> str:
            return format_media_summary(
                prefix=prefix,
                title=display_name,
                size_label=size_label,
            )

        if has_valid_plex:
            await safe_edit_message(
                query.message,
                text="Connecting to Plex and attempting to delete the item...",
                reply_markup=None,
            )

            result, plex = await _delete_item_from_plex(path_to_delete, plex_config)  # type: ignore[arg-type]
            status = result.get("status", "error")
            detail = result.get("detail", "")

            if status == "success":
                outcome = "plex_deleted"
                message_text = _format_item_line("🗑️ *Successfully Deleted from Plex*")
            elif status == "manual_delete_required":
                logger.warning(
                    "Plex deletion requires manual filesystem cleanup for '%s': %s",
                    path_to_delete,
                    detail,
                )
                manual_success, manual_detail = await _delete_from_filesystem(path_to_delete)
                if manual_success:
                    outcome = "disk_deleted_plex_cleanup_needed"
                    note = escape_markdown(detail or "Plex still needs cleanup.", version=2)
                    message_text = _format_item_line("⚠️ *Deleted From Disk, Plex Needs Cleanup*")
                    message_text += "\n" + note
                else:
                    outcome = "disk_delete_failed_after_plex_manual_required"
                    message_text = _format_manual_delete_failure(manual_detail)
            elif status == "skip":
                logger.info(
                    "Plex deletion skipped for '%s'; falling back to filesystem removal.",
                    path_to_delete,
                )
                manual_success, manual_detail = await _delete_from_filesystem(path_to_delete)
                if manual_success:
                    outcome = "disk_deleted_plex_skipped"
                    note = escape_markdown(
                        "Plex library left untouched because other encodes still exist.",
                        version=2,
                    )
                    message_text = (
                        _format_item_line("⚠️ *Plex Skipped, Removed From Disk*") + "\n" + note
                    )
                else:
                    outcome = "disk_delete_failed_after_plex_skip"
                    message_text = _format_manual_delete_failure(manual_detail)
            elif status == "not_found":
                logger.warning(
                    "Item not found in Plex, falling back to manual filesystem deletion."
                )
                manual_success, manual_detail = await _delete_from_filesystem(path_to_delete)
                if manual_success:
                    outcome = "disk_deleted_item_missing_in_plex"
                    message_text = _format_item_line("🗑️ *Item Not in Plex, Deleted Manually*")
                else:
                    outcome = "disk_delete_failed_after_plex_not_found"
                    message_text = _format_manual_delete_failure(manual_detail)
            else:
                if plex is None:
                    logger.warning(
                        "Unable to communicate with Plex for '%s': %s",
                        path_to_delete,
                        detail,
                    )
                    manual_success, manual_detail = await _delete_from_filesystem(path_to_delete)
                    if manual_success:
                        outcome = "disk_deleted_plex_unavailable"
                        error_note = escape_markdown(
                            f"Plex deletion failed: {detail or 'Unknown error'}",
                            version=2,
                        )
                        message_text = (
                            _format_item_line("⚠️ *Plex Unavailable, Deleted From Disk*")
                            + "\n"
                            + error_note
                        )
                    else:
                        outcome = "disk_delete_failed_plex_unavailable"
                        message_text = _format_manual_delete_failure(manual_detail)
                else:
                    outcome = "plex_delete_failed"
                    message_text = "❌ *Deletion Failed*\n`{}`".format(
                        escape_markdown(detail or "Unknown error", version=2)
                    )

            filesystem_deleted = not os.path.exists(path_to_delete)
            if filesystem_deleted and delete_target_kind == "movie_collection" and plex is not None:
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
            if plex_config:
                logger.warning("Plex token missing or placeholder; skipping Plex API deletion.")
                reason_note = "Plex token is set to the placeholder; deleted item from disk only."
            else:
                reason_note = "Plex integration is not configured; deleted item from disk."

            manual_success, manual_detail = await _delete_from_filesystem(path_to_delete)
            if manual_success:
                outcome = "disk_deleted_without_plex"
                note = escape_markdown(reason_note, version=2)
                message_text = _format_item_line("✅ *Deleted From Disk*") + "\n" + note
            else:
                outcome = "disk_delete_failed_without_plex"
                message_text = _format_manual_delete_failure(manual_detail)

    _log_delete_event(
        query.message,
        "confirm_completed",
        target_kind=delete_target_kind,
        path=path_to_delete,
        outcome=outcome,
    )
    await safe_edit_message(
        query.message,
        text=message_text,
        reply_markup=None,
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    await return_to_home(
        context,
        query.message.chat_id,
        message_text=None,
        message_parse_mode=ParseMode.MARKDOWN_V2,
    )
