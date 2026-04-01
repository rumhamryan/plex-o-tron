from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from datetime import date, datetime
from typing import Any, cast

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from telegram_bot.config import logger
from telegram_bot.domain.types import TrackingItem
from telegram_bot.services.tracking import manager as tracking_manager
from telegram_bot.services.tracking.collection_resolution import (
    CollectionTrackingCandidate,
    resolve_collection_tracking_candidates,
)
from telegram_bot.services.tracking.targets import (
    MOVIE_TRACKING_ADAPTER,
    TV_ONGOING_TRACKING_ADAPTER,
)
from telegram_bot.ui.keyboards import (
    cancel_only_keyboard,
    confirm_cancel_keyboard,
    single_column_keyboard,
)
from telegram_bot.utils import safe_edit_message
from telegram_bot.workflows.navigation import (
    get_active_prompt_message_id,
    mark_chat_workflow_active,
    return_to_home,
    set_active_prompt_message_id,
)

from .state import (
    TRACKING_COLLECTION_CANDIDATES_KEY,
    TRACKING_COLLECTION_LIBRARY_MOVIES_KEY,
    TRACKING_COLLECTION_NAME_KEY,
    TRACKING_COLLECTION_SKIPPED_PAST_YEAR_KEY,
    TRACKING_COLLECTION_SKIPPED_STREAMING_KEY,
    TRACKING_CANDIDATES_KEY,
    TRACKING_NEXT_ACTION_KEY,
    TRACKING_SELECTED_INDEX_KEY,
    TRACKING_TARGET_KIND_KEY,
    clear_tracking_workflow_state,
)

TRACKING_AWAIT_MOVIE_TITLE = "await_movie_title"
TRACKING_AWAIT_TV_TITLE = "await_tv_title"
TRACKING_AWAIT_COLLECTION_NAME = "await_collection_name"
TRACKING_CALLBACK_PREFIX = "track_"


def _get_user_data_store(context: ContextTypes.DEFAULT_TYPE) -> MutableMapping[str, Any]:
    if context.user_data is None:
        context.user_data = {}
    return cast(MutableMapping[str, Any], context.user_data)


def _tracking_menu_keyboard() -> InlineKeyboardMarkup:
    return single_column_keyboard(
        [
            ("🎬 Schedule Movie", "track_schedule_movie"),
            ("📺 Schedule TV Show", "track_schedule_tv"),
            ("📋 Review Scheduled Items", "track_review"),
        ]
    )


def _tracking_menu_text() -> str:
    return (
        "*Auto\\-Download Tracking*\n\n"
        "Schedule future movie releases or ongoing TV next\\-episode tracking\\.\n"
        "You can also review and cancel active schedules\\."
    )


def _tracking_movie_scope_keyboard() -> InlineKeyboardMarkup:
    return single_column_keyboard(
        [
            ("🎬 Single Movie", "track_schedule_movie_single"),
            ("🎞️ Collection", "track_schedule_movie_collection"),
        ]
    )


def _collection_candidate_line(candidate: Mapping[str, Any]) -> str:
    title = str(candidate.get("canonical_title") or candidate.get("title") or "Movie")
    escaped_title = escape_markdown(title, version=2)
    year = candidate.get("year")
    suffix = f" \\({int(year)}\\)" if isinstance(year, int) else ""

    availability_date = candidate.get("availability_date")
    if isinstance(availability_date, date):
        availability_text = availability_date.isoformat()
    else:
        availability_text = "TBD"
    escaped_availability = escape_markdown(availability_text, version=2)
    return f"\\- {escaped_title}{suffix}\n  Streaming: {escaped_availability}"


def _collection_confirm_prompt_text(
    collection_name: str,
    candidates: list[CollectionTrackingCandidate],
    *,
    total_titles: int,
    skipped_released_streaming: int,
    skipped_past_year_unknown_streaming: int,
) -> str:
    escaped_collection = escape_markdown(collection_name, version=2)
    lines: list[str] = [
        f"*Confirm Collection Schedule*\n\n*Collection:* {escaped_collection}",
        f"*Total titles resolved:* {total_titles}",
        f"*To schedule:* {len(candidates)}",
    ]
    if skipped_released_streaming:
        lines.append(f"*Skipped \\(already streaming released\\):* {skipped_released_streaming}")
    if skipped_past_year_unknown_streaming:
        lines.append(
            "*Skipped \\(past\\-year with unknown streaming date\\):* "
            f"{skipped_past_year_unknown_streaming}"
        )

    lines.append("")
    preview_limit = 10
    for candidate in candidates[:preview_limit]:
        lines.append(_collection_candidate_line(candidate))
    if len(candidates) > preview_limit:
        lines.append(f"\\- \\+{len(candidates) - preview_limit} more")
    lines.append("\nStart tracking these collection titles?")
    return "\n".join(lines)


def _candidate_summary_line(candidate: Mapping[str, Any]) -> str:
    target_kind = str(candidate.get("target_kind") or "movie").lower()
    title = str(candidate.get("canonical_title") or candidate.get("title") or "Item")
    escaped_title = escape_markdown(title, version=2)

    if target_kind == "tv":
        next_air = candidate.get("next_air_date")
        if isinstance(next_air, date):
            next_air_text = next_air.isoformat()
        else:
            next_air_text = "TBD"
        return f"\\- {escaped_title}\n  Next Air: {escape_markdown(next_air_text, version=2)}"

    year = candidate.get("year")
    year_text = f" \\({int(year)}\\)" if isinstance(year, int) else ""
    date_value = candidate.get("availability_date")
    source = candidate.get("availability_source")

    if isinstance(source, str) and source.strip():
        source_label = source.strip()
    elif isinstance(date_value, date):
        source_label = "unknown"
    else:
        source_label = "metadata-only checks"

    date_label = date_value.isoformat() if isinstance(date_value, date) else "TBD"
    escaped_source = escape_markdown(source_label, version=2)
    escaped_date = escape_markdown(date_label, version=2)
    return f"\\- {escaped_title}{year_text}\n  Source: {escaped_source}\n  Date: {escaped_date}"


def _candidate_selection_summary_line(candidate: Mapping[str, Any]) -> str:
    target_kind = str(candidate.get("target_kind") or "movie").lower()
    if target_kind != "tv":
        return _candidate_summary_line(candidate)

    title = str(candidate.get("canonical_title") or candidate.get("title") or "Item")
    escaped_title = escape_markdown(title, version=2)
    next_air = candidate.get("next_air_date")
    if isinstance(next_air, date):
        next_air_text = next_air.isoformat()
    else:
        next_air_text = "TBD"
    return f"\\- {escaped_title}\n  Next Air: {escape_markdown(next_air_text, version=2)}"


def _candidate_button_label(candidate: Mapping[str, Any]) -> str:
    title = str(candidate.get("canonical_title") or candidate.get("title") or "Item")
    target_kind = str(candidate.get("target_kind") or "movie").lower()
    if target_kind == "movie":
        year = candidate.get("year")
        if isinstance(year, int):
            title = f"{title} ({year})"
    if len(title) > 45:
        title = f"{title[:42]}..."
    return title


def _tracking_review_item_summary_line(item: TrackingItem) -> str:
    target_kind = str(item.get("target_kind") or "movie").lower()
    icon = "📺" if target_kind == "tv" else "🎬"
    title = tracking_manager.get_tracking_display_title(item)
    escaped_title = escape_markdown(title, version=2)

    if target_kind != "tv":
        return f"\\- {icon} {escaped_title}"

    payload = item.get("target_payload")
    payload_dict = payload if isinstance(payload, dict) else {}
    pending_air_date = payload_dict.get("pending_episode_air_date")
    if isinstance(pending_air_date, date):
        next_air_text = pending_air_date.isoformat()
    elif isinstance(pending_air_date, str) and pending_air_date.strip():
        raw_value = pending_air_date.strip()
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            next_air_text = parsed.date().isoformat()
        except ValueError:
            next_air_text = raw_value
    else:
        next_air_text = "TBD"

    return (
        f"\\- {icon} {escaped_title}\n" f"  Next Air: {escape_markdown(next_air_text, version=2)}"
    )


def _tracking_review_keyboard(items: list[TrackingItem]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        item_id = str(item.get("id"))
        target_kind = str(item.get("target_kind") or "movie").lower()
        icon = "🎬" if target_kind == "movie" else "📺"
        title = tracking_manager.get_tracking_display_title(item)
        if len(title) > 22:
            title = f"{title[:19]}..."
        rows.append(
            [
                InlineKeyboardButton(
                    f"🛑 Cancel: {icon} {title}",
                    callback_data=f"track_cancel_{item_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")])
    return InlineKeyboardMarkup(rows)


async def render_tracking_menu(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    *,
    target_message: Message | None = None,
) -> None:
    mark_chat_workflow_active(context, chat_id, "track")
    if isinstance(target_message, Message):
        await safe_edit_message(
            target_message,
            text=_tracking_menu_text(),
            reply_markup=_tracking_menu_keyboard(),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        set_active_prompt_message_id(context, chat_id, target_message.message_id)
        return

    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=_tracking_menu_text(),
        reply_markup=_tracking_menu_keyboard(),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    set_active_prompt_message_id(context, chat_id, sent.message_id)


async def _handle_movie_schedule_scope_prompt(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not isinstance(query.message, Message):
        return
    user_data = _get_user_data_store(context)
    mark_chat_workflow_active(context, query.message.chat_id, "track")
    clear_tracking_workflow_state(user_data)
    user_data[TRACKING_TARGET_KIND_KEY] = "movie"
    await safe_edit_message(
        query.message,
        text=(
            "🎬 Choose the movie scheduling mode\\.\n\n"
            "Single Movie: track one movie title\\.\n"
            "Collection: resolve a franchise and track titles without streaming release\\."
        ),
        reply_markup=_tracking_movie_scope_keyboard(),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    set_active_prompt_message_id(context, query.message.chat_id, query.message.message_id)


async def _handle_collection_name_prompt(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not isinstance(query.message, Message):
        return
    user_data = _get_user_data_store(context)
    mark_chat_workflow_active(context, query.message.chat_id, "track")
    clear_tracking_workflow_state(user_data)
    user_data[TRACKING_TARGET_KIND_KEY] = "movie"
    user_data[TRACKING_NEXT_ACTION_KEY] = TRACKING_AWAIT_COLLECTION_NAME
    await safe_edit_message(
        query.message,
        text=(
            "🎞️ Send the movie collection or franchise name to schedule\\.\n\n"
            "I will resolve collection titles, then keep only titles that do not yet have a "
            "streaming release date\\."
        ),
        reply_markup=cancel_only_keyboard(),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    set_active_prompt_message_id(context, query.message.chat_id, query.message.message_id)


async def _handle_schedule_prompt(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    target_kind: str,
) -> None:
    if not isinstance(query.message, Message):
        return
    user_data = _get_user_data_store(context)
    mark_chat_workflow_active(context, query.message.chat_id, "track")
    clear_tracking_workflow_state(user_data)
    user_data[TRACKING_TARGET_KIND_KEY] = target_kind
    if target_kind == "tv":
        user_data[TRACKING_NEXT_ACTION_KEY] = TRACKING_AWAIT_TV_TITLE
        prompt_text = (
            "📺 Send the TV show title to track for ongoing next\\-episode auto\\-download\\.\n\n"
            "This mode follows the next unretrieved released episode for the show\\."
        )
    else:
        user_data[TRACKING_NEXT_ACTION_KEY] = TRACKING_AWAIT_MOVIE_TITLE
        prompt_text = (
            "🎬 Send the movie title you want to track for automatic future download\\.\n\n"
            "Only unreleased movies can be scheduled\\."
        )
    await safe_edit_message(
        query.message,
        text=prompt_text,
        reply_markup=cancel_only_keyboard(),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    set_active_prompt_message_id(context, query.message.chat_id, query.message.message_id)


async def _render_tracking_review(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not isinstance(query.message, Message):
        return
    chat_id = query.message.chat_id
    items = tracking_manager.list_tracking_items(context.application, chat_id=chat_id)
    if not items:
        await return_to_home(
            context,
            chat_id,
            source_message=query.message,
            message_text="No scheduled items to review\\.",
            message_parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    await safe_edit_message(
        query.message,
        text=(
            "*Active Scheduled Items*\n\n"
            + "\n".join(_tracking_review_item_summary_line(item) for item in items)
            + "\n\nChoose a scheduled item below if you want to cancel it\\."
        ),
        reply_markup=_tracking_review_keyboard(items),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


def _parse_callback_suffix(data: str, prefix: str) -> str | None:
    if not data.startswith(prefix):
        return None
    return data[len(prefix) :]


def _confirm_prompt_text(candidate: Mapping[str, Any]) -> str:
    target_kind = str(candidate.get("target_kind") or "movie").lower()
    if target_kind == "tv":
        footer = "Start tracking this TV show for ongoing next episodes?"
    else:
        footer = "Start tracking this movie?"
    return f"*Confirm Schedule*\n\n{_candidate_summary_line(candidate)}\n\n{footer}"


async def _handle_pick_candidate(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not isinstance(query.message, Message):
        return
    user_data = _get_user_data_store(context)
    candidates = user_data.get(TRACKING_CANDIDATES_KEY)
    if not isinstance(candidates, list):
        await safe_edit_message(
            query.message,
            text="This selection has expired\\. Please start again\\.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    suffix = _parse_callback_suffix(query.data or "", "track_pick_")
    if suffix is None:
        return
    try:
        index = int(suffix)
    except ValueError:
        return
    if not (0 <= index < len(candidates)):
        return

    candidate = cast(dict[str, Any], candidates[index])
    user_data[TRACKING_SELECTED_INDEX_KEY] = index
    await safe_edit_message(
        query.message,
        text=_confirm_prompt_text(candidate),
        reply_markup=confirm_cancel_keyboard("✅ Confirm Schedule", "track_confirm"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_confirm_candidate(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not isinstance(query.message, Message):
        return
    user_data = _get_user_data_store(context)
    selected_index = user_data.get(TRACKING_SELECTED_INDEX_KEY)
    candidates = user_data.get(TRACKING_CANDIDATES_KEY)
    if not isinstance(selected_index, int) or not isinstance(candidates, list):
        await safe_edit_message(
            query.message,
            text="This selection has expired\\. Please schedule again\\.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    if not (0 <= selected_index < len(candidates)):
        return

    selected = cast(dict[str, Any], candidates[selected_index])
    target_kind = str(
        selected.get("target_kind") or user_data.get(TRACKING_TARGET_KIND_KEY) or "movie"
    )
    target_kind = target_kind.lower()

    if target_kind == "movie":
        if selected.get("is_released"):
            await safe_edit_message(
                query.message,
                text=(
                    "❌ This movie appears to be already released for streaming or Blu\\-ray/DVD\\.\n"
                    "Only future releases can be scheduled\\."
                ),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        created = tracking_manager.create_movie_tracking_item(
            context.application,
            chat_id=query.message.chat_id,
            canonical_title=str(selected.get("canonical_title") or selected.get("title") or ""),
            year=selected.get("year"),
            availability_date=selected.get("availability_date"),
            availability_source=selected.get("availability_source"),
            title=str(selected.get("title") or selected.get("canonical_title") or ""),
        )
    else:
        tmdb_series_id = selected.get("tmdb_series_id")
        if not isinstance(tmdb_series_id, int) or tmdb_series_id <= 0:
            await safe_edit_message(
                query.message,
                text=(
                    "❌ This TV selection does not include a valid TMDB series id\\.\n"
                    "Please start again and choose another show\\."
                ),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        created = tracking_manager.create_tv_tracking_item(
            context.application,
            chat_id=query.message.chat_id,
            canonical_title=str(selected.get("canonical_title") or selected.get("title") or ""),
            tmdb_series_id=tmdb_series_id,
            title=str(selected.get("title") or selected.get("canonical_title") or ""),
            next_air_date=selected.get("next_air_date"),
        )

    created_type = "TV Show" if created.get("target_kind") == "tv" else "Movie"
    created_title = tracking_manager.get_tracking_display_title(created)
    clear_tracking_workflow_state(user_data)
    await return_to_home(
        context,
        query.message.chat_id,
        source_message=query.message,
        message_text=(
            "✅ Schedule created\\.\n\n"
            f"Type: *{escape_markdown(created_type, version=2)}*\n"
            f"Name: *{escape_markdown(created_title, version=2)}*\n\n"
            "This item will be monitored until fulfillment or manual cancellation\\."
        ),
        message_parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_confirm_collection(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not isinstance(query.message, Message):
        return
    user_data = _get_user_data_store(context)
    raw_collection_name = user_data.get(TRACKING_COLLECTION_NAME_KEY)
    raw_candidates = user_data.get(TRACKING_COLLECTION_CANDIDATES_KEY)
    raw_library_movies = user_data.get(TRACKING_COLLECTION_LIBRARY_MOVIES_KEY)
    if not isinstance(raw_collection_name, str) or not isinstance(raw_candidates, list):
        await safe_edit_message(
            query.message,
            text="This collection selection has expired\\. Please start again\\.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    tracked_items = tracking_manager.get_tracking_items(context.application.bot_data)
    known_item_ids = set(tracked_items.keys())
    collection_movies = (
        cast(list[dict[str, Any]], raw_library_movies)
        if isinstance(raw_library_movies, list)
        else []
    )

    scheduled_titles: list[str] = []
    new_count = 0
    reused_count = 0
    for raw_candidate in raw_candidates:
        if not isinstance(raw_candidate, dict):
            continue
        canonical_title = str(
            raw_candidate.get("canonical_title") or raw_candidate.get("title") or ""
        ).strip()
        if not canonical_title:
            continue
        raw_availability_date = raw_candidate.get("availability_date")
        availability_date = (
            raw_availability_date if isinstance(raw_availability_date, date) else None
        )
        created = tracking_manager.create_movie_tracking_item(
            context.application,
            chat_id=query.message.chat_id,
            canonical_title=canonical_title,
            year=raw_candidate.get("year"),
            availability_date=availability_date,
            availability_source=raw_candidate.get("availability_source"),
            collection_name=raw_collection_name,
            collection_fs_name=raw_collection_name,
            collection_movies=collection_movies,
            title=canonical_title,
        )
        created_id = str(created.get("id") or "")
        if created_id and created_id in known_item_ids:
            reused_count += 1
        else:
            new_count += 1
            if created_id:
                known_item_ids.add(created_id)
        scheduled_titles.append(tracking_manager.get_tracking_display_title(created))

    if not scheduled_titles:
        clear_tracking_workflow_state(user_data)
        await return_to_home(
            context,
            query.message.chat_id,
            source_message=query.message,
            message_text=(
                "❌ No schedulable titles remained for this collection\\.\n\n"
                "Try another collection name, or schedule a single movie instead\\."
            ),
            message_parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    escaped_collection_name = escape_markdown(raw_collection_name, version=2)
    preview_limit = 8
    preview_lines = [
        f"\\- {escape_markdown(title, version=2)}" for title in scheduled_titles[:preview_limit]
    ]
    if len(scheduled_titles) > preview_limit:
        preview_lines.append(f"\\- \\+{len(scheduled_titles) - preview_limit} more")
    clear_tracking_workflow_state(user_data)
    await return_to_home(
        context,
        query.message.chat_id,
        source_message=query.message,
        message_text=(
            "✅ Collection schedule created\\.\n\n"
            f"Collection: *{escaped_collection_name}*\n"
            f"Titles selected: *{len(scheduled_titles)}*\n"
            f"New schedules: *{new_count}*\n"
            f"Already scheduled: *{reused_count}*\n\n" + "\n".join(preview_lines)
        ),
        message_parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_cancel_item(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not isinstance(query.message, Message):
        return
    item_id = _parse_callback_suffix(query.data or "", "track_cancel_")
    if not item_id:
        return
    item = tracking_manager.get_tracking_item(context.application, item_id)
    if not item or int(item.get("chat_id", 0) or 0) != query.message.chat_id:
        await safe_edit_message(
            query.message,
            text="That scheduled item is no longer available\\.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    title = tracking_manager.get_tracking_display_title(item)
    await safe_edit_message(
        query.message,
        text=f"Cancel this scheduled item\\?\n\n*{escape_markdown(title, version=2)}*",
        reply_markup=confirm_cancel_keyboard("✅ Yes, Cancel", f"track_cancel_confirm_{item_id}"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_cancel_item_confirm(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not isinstance(query.message, Message):
        return
    item_id = _parse_callback_suffix(query.data or "", "track_cancel_confirm_")
    if not item_id:
        return
    item = tracking_manager.get_tracking_item(context.application, item_id)
    cancelled_type = "TV Show" if item and item.get("target_kind") == "tv" else "Movie"
    cancelled_title = tracking_manager.get_tracking_display_title(item) if item else "Unknown"
    cancelled = tracking_manager.cancel_tracking_item(
        context.application,
        item_id=item_id,
        chat_id=query.message.chat_id,
    )
    if cancelled:
        await return_to_home(
            context,
            query.message.chat_id,
            source_message=query.message,
            message_text=(
                "✅ Scheduled item cancelled\\.\n\n"
                f"Type: *{escape_markdown(cancelled_type, version=2)}*\n"
                f"Name: *{escape_markdown(cancelled_title, version=2)}*"
            ),
            message_parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    await safe_edit_message(
        query.message,
        text="That scheduled item could not be cancelled\\.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def handle_tracking_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback router for tracking workflow actions."""
    query = update.callback_query
    if not query or not isinstance(query.message, Message):
        return

    user_data = _get_user_data_store(context)
    action = query.data or ""
    if not action.startswith(TRACKING_CALLBACK_PREFIX):
        return
    await query.answer()

    if action == "track_back":
        clear_tracking_workflow_state(user_data)
        await render_tracking_menu(context, query.message.chat_id, target_message=query.message)
        return
    if action == "track_schedule_movie":
        await _handle_movie_schedule_scope_prompt(query, context)
        return
    if action == "track_schedule_movie_single":
        await _handle_schedule_prompt(query, context, target_kind="movie")
        return
    if action == "track_schedule_movie_collection":
        await _handle_collection_name_prompt(query, context)
        return
    if action == "track_schedule_tv":
        await _handle_schedule_prompt(query, context, target_kind="tv")
        return
    if action == "track_review":
        clear_tracking_workflow_state(user_data)
        await _render_tracking_review(query, context)
        return
    if action.startswith("track_pick_"):
        await _handle_pick_candidate(query, context)
        return
    if action == "track_confirm":
        await _handle_confirm_candidate(query, context)
        return
    if action == "track_collection_confirm":
        await _handle_confirm_collection(query, context)
        return
    if action.startswith("track_cancel_confirm_"):
        await _handle_cancel_item_confirm(query, context)
        return
    if action.startswith("track_cancel_"):
        await _handle_cancel_item(query, context)
        return

    logger.warning("Unhandled tracking callback action: %s", action)


def _active_target_kind(user_data: MutableMapping[str, Any]) -> str:
    raw_kind = str(user_data.get(TRACKING_TARGET_KIND_KEY) or "movie").strip().lower()
    return "tv" if raw_kind == "tv" else "movie"


async def _resolve_tracking_candidates(
    *,
    title_query: str,
    target_kind: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> list[dict[str, Any]]:
    now_utc = tracking_manager.utc_now(
        context.application.bot_data.get(tracking_manager.TRACKING_NOW_PROVIDER_KEY)
    )
    if target_kind == "tv":
        return await TV_ONGOING_TRACKING_ADAPTER.resolve_candidates_from_user_input(
            title_query,
            application=context.application,
            now_utc=now_utc,
        )
    return await MOVIE_TRACKING_ADAPTER.resolve_candidates_from_user_input(
        title_query,
        application=context.application,
        now_utc=now_utc,
    )


async def _handle_collection_name_resolution(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    user_data: MutableMapping[str, Any],
    chat_id: int,
    prompt_message_id: int,
    collection_query: str,
) -> None:
    now_utc = tracking_manager.utc_now(
        context.application.bot_data.get(tracking_manager.TRACKING_NOW_PROVIDER_KEY)
    )
    today = now_utc.astimezone(
        tracking_manager.get_tracking_timezone(context.application.bot_data)
    ).date()
    resolution = await resolve_collection_tracking_candidates(collection_query, today=today)
    if resolution is None:
        user_data[TRACKING_NEXT_ACTION_KEY] = TRACKING_AWAIT_COLLECTION_NAME
        user_data.pop(TRACKING_COLLECTION_NAME_KEY, None)
        user_data.pop(TRACKING_COLLECTION_CANDIDATES_KEY, None)
        user_data.pop(TRACKING_COLLECTION_LIBRARY_MOVIES_KEY, None)
        user_data.pop(TRACKING_COLLECTION_SKIPPED_STREAMING_KEY, None)
        user_data.pop(TRACKING_COLLECTION_SKIPPED_PAST_YEAR_KEY, None)
        await safe_edit_message(
            context.bot,
            text=(
                "❌ No matching collection could be resolved\\.\n\n"
                "Send another collection or franchise name, or tap Cancel to exit\\."
            ),
            chat_id=chat_id,
            message_id=prompt_message_id,
            reply_markup=cancel_only_keyboard(),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    collection_name = str(resolution.get("collection_name") or "Collection")
    candidates = cast(list[CollectionTrackingCandidate], resolution.get("candidates") or [])
    library_movies = cast(list[dict[str, Any]], resolution.get("library_movies") or [])
    total_titles = int(resolution.get("total_titles") or 0)
    skipped_released_streaming = int(resolution.get("skipped_released_streaming") or 0)
    skipped_past_year_unknown = int(resolution.get("skipped_past_year_unknown_streaming") or 0)

    user_data[TRACKING_COLLECTION_NAME_KEY] = collection_name
    user_data[TRACKING_COLLECTION_CANDIDATES_KEY] = candidates
    user_data[TRACKING_COLLECTION_LIBRARY_MOVIES_KEY] = library_movies
    user_data[TRACKING_COLLECTION_SKIPPED_STREAMING_KEY] = skipped_released_streaming
    user_data[TRACKING_COLLECTION_SKIPPED_PAST_YEAR_KEY] = skipped_past_year_unknown

    if not candidates:
        user_data[TRACKING_NEXT_ACTION_KEY] = TRACKING_AWAIT_COLLECTION_NAME
        summary_lines = [
            "❌ No titles in this collection are schedulable right now\\.",
            "",
            f"Collection: *{escape_markdown(collection_name, version=2)}*",
            f"Titles resolved: *{total_titles}*",
            f"Already streaming released: *{skipped_released_streaming}*",
        ]
        if skipped_past_year_unknown:
            summary_lines.append(
                "Past\\-year with unknown streaming date: " f"*{skipped_past_year_unknown}*"
            )
        summary_lines.append("")
        summary_lines.append("Send another collection name, or tap Cancel\\.")
        await safe_edit_message(
            context.bot,
            text="\n".join(summary_lines),
            chat_id=chat_id,
            message_id=prompt_message_id,
            reply_markup=cancel_only_keyboard(),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    user_data.pop(TRACKING_NEXT_ACTION_KEY, None)
    await safe_edit_message(
        context.bot,
        text=_collection_confirm_prompt_text(
            collection_name,
            candidates,
            total_titles=total_titles,
            skipped_released_streaming=skipped_released_streaming,
            skipped_past_year_unknown_streaming=skipped_past_year_unknown,
        ),
        chat_id=chat_id,
        message_id=prompt_message_id,
        reply_markup=confirm_cancel_keyboard("✅ Confirm Collection", "track_collection_confirm"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def handle_tracking_workflow_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Handles text input for tracking workflow prompts."""
    message = update.message
    if not isinstance(message, Message) or not message.text:
        return

    user_data = _get_user_data_store(context)
    next_action = user_data.get(TRACKING_NEXT_ACTION_KEY)
    if next_action not in {
        TRACKING_AWAIT_MOVIE_TITLE,
        TRACKING_AWAIT_TV_TITLE,
        TRACKING_AWAIT_COLLECTION_NAME,
    }:
        return

    title_query = message.text.strip()
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        pass

    chat_id = message.chat_id
    prompt_message_id = get_active_prompt_message_id(context, chat_id)
    target_kind = _active_target_kind(user_data)
    if next_action == TRACKING_AWAIT_COLLECTION_NAME:
        loading_text = "🔎 Resolving collection titles and streaming release metadata\\..."
    else:
        loading_text = (
            "🔎 Looking up release metadata..."
            if target_kind == "movie"
            else "🔎 Looking up TMDB show metadata..."
        )
    if isinstance(prompt_message_id, int):
        await safe_edit_message(
            context.bot,
            text=loading_text,
            chat_id=chat_id,
            message_id=prompt_message_id,
        )
    else:
        status_message = await context.bot.send_message(
            chat_id=chat_id,
            text=loading_text,
        )
        prompt_message_id = status_message.message_id
        set_active_prompt_message_id(context, chat_id, prompt_message_id)

    if next_action == TRACKING_AWAIT_COLLECTION_NAME:
        await _handle_collection_name_resolution(
            context=context,
            user_data=user_data,
            chat_id=chat_id,
            prompt_message_id=prompt_message_id,
            collection_query=title_query,
        )
        set_active_prompt_message_id(context, chat_id, prompt_message_id)
        return

    candidates = await _resolve_tracking_candidates(
        title_query=title_query,
        target_kind=target_kind,
        context=context,
    )
    if target_kind == "movie":
        schedulable = [candidate for candidate in candidates if not candidate.get("is_released")]
        if not schedulable:
            user_data[TRACKING_NEXT_ACTION_KEY] = TRACKING_AWAIT_MOVIE_TITLE
            user_data.pop(TRACKING_CANDIDATES_KEY, None)
            user_data.pop(TRACKING_SELECTED_INDEX_KEY, None)
            await safe_edit_message(
                context.bot,
                text=(
                    "❌ This title appears to be already released, or no future release metadata "
                    "could be confirmed\\.\n\n"
                    "Send another movie title, or tap Cancel to exit\\."
                ),
                chat_id=chat_id,
                message_id=prompt_message_id,
                reply_markup=cancel_only_keyboard(),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
    else:
        schedulable = candidates
        if not schedulable:
            user_data[TRACKING_NEXT_ACTION_KEY] = TRACKING_AWAIT_TV_TITLE
            user_data.pop(TRACKING_CANDIDATES_KEY, None)
            user_data.pop(TRACKING_SELECTED_INDEX_KEY, None)
            await safe_edit_message(
                context.bot,
                text=(
                    "❌ No matching TV show could be resolved from TMDB\\.\n\n"
                    "Verify the show title, ensure TMDB credentials are configured, "
                    "then send another TV show title\\."
                ),
                chat_id=chat_id,
                message_id=prompt_message_id,
                reply_markup=cancel_only_keyboard(),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

    user_data[TRACKING_CANDIDATES_KEY] = schedulable
    user_data.pop(TRACKING_SELECTED_INDEX_KEY, None)
    user_data.pop(TRACKING_NEXT_ACTION_KEY, None)

    if len(schedulable) == 1:
        user_data[TRACKING_SELECTED_INDEX_KEY] = 0
        single_candidate = cast(dict[str, Any], schedulable[0])
        await safe_edit_message(
            context.bot,
            text=_confirm_prompt_text(single_candidate),
            chat_id=chat_id,
            message_id=prompt_message_id,
            reply_markup=confirm_cancel_keyboard("✅ Confirm Schedule", "track_confirm"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        set_active_prompt_message_id(context, chat_id, prompt_message_id)
        return

    header = (
        "*Select A TV Show To Schedule*" if target_kind == "tv" else "*Select A Movie To Schedule*"
    )
    lines = [header, ""]
    lines.extend(_candidate_selection_summary_line(candidate) for candidate in schedulable[:8])
    rows = [
        [
            InlineKeyboardButton(
                f"{idx + 1}. {_candidate_button_label(candidate)}",
                callback_data=f"track_pick_{idx}",
            )
        ]
        for idx, candidate in enumerate(schedulable[:8])
    ]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")])

    await safe_edit_message(
        context.bot,
        text="\n".join(lines),
        chat_id=chat_id,
        message_id=prompt_message_id,
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    set_active_prompt_message_id(context, chat_id, prompt_message_id)
