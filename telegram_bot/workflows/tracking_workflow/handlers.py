from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from datetime import date
from typing import Any, cast

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from telegram_bot.config import logger
from telegram_bot.domain.types import TrackingItem
from telegram_bot.services.tracking import manager as tracking_manager
from telegram_bot.services.tracking import movie_release_dates
from telegram_bot.services.tracking.movie_release_dates import MovieTrackingResolution
from telegram_bot.ui.keyboards import cancel_only_keyboard, confirm_cancel_keyboard
from telegram_bot.utils import safe_edit_message
from telegram_bot.workflows.navigation import (
    get_active_prompt_message_id,
    mark_chat_workflow_active,
    set_active_prompt_message_id,
)

from .state import (
    TRACKING_CANDIDATES_KEY,
    TRACKING_NEXT_ACTION_KEY,
    TRACKING_SELECTED_INDEX_KEY,
    clear_tracking_workflow_state,
)

TRACKING_AWAIT_MOVIE_TITLE = "await_movie_title"
TRACKING_CALLBACK_PREFIX = "track_"


def _get_user_data_store(context: ContextTypes.DEFAULT_TYPE) -> MutableMapping[str, Any]:
    if context.user_data is None:
        context.user_data = {}
    return cast(MutableMapping[str, Any], context.user_data)


def _tracking_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎬 Schedule Movie", callback_data="track_schedule_movie")],
            [InlineKeyboardButton("📋 Review Scheduled Items", callback_data="track_review")],
            [InlineKeyboardButton("🏠 Back Home", callback_data="cancel_operation")],
        ]
    )


def _tracking_menu_text() -> str:
    return (
        "*Auto\\-Download Tracking*\n\n"
        "Schedule unreleased movies, review active schedules, or cancel any schedule\\."
    )


def _candidate_summary_line(candidate: Mapping[str, Any]) -> str:
    title = str(candidate.get("canonical_title") or candidate.get("title") or "Movie")
    year = candidate.get("year")
    date_value = candidate.get("availability_date")
    source = candidate.get("availability_source")

    year_text = f" \\({int(year)}\\)" if isinstance(year, int) else ""
    if isinstance(source, str) and source.strip():
        source_label = source.strip()
    elif isinstance(date_value, date):
        source_label = "unknown"
    else:
        source_label = "metadata-only checks"

    date_label = date_value.isoformat() if isinstance(date_value, date) else "TBD"
    escaped_title = escape_markdown(title, version=2)
    escaped_source = escape_markdown(source_label, version=2)
    escaped_date = escape_markdown(date_label, version=2)
    return (
        f"\\- {escaped_title}{year_text}\n"
        f"  Source: {escaped_source}\n"
        f"  Date: {escaped_date}"
    )


def _candidate_button_label(candidate: Mapping[str, Any]) -> str:
    title = str(candidate.get("canonical_title") or candidate.get("title") or "Movie")
    year = candidate.get("year")
    if isinstance(year, int):
        title = f"{title} ({year})"
    if len(title) > 45:
        title = f"{title[:42]}..."
    return title


def _format_tracking_item_line(item: Mapping[str, Any], index: int) -> str:
    title = str(item.get("canonical_title") or item.get("title") or "Movie")
    year = item.get("year")
    status = str(item.get("status") or "unknown")
    next_check = str(item.get("next_check_at_utc") or "n/a")
    year_text = f" \\({int(year)}\\)" if isinstance(year, int) else ""
    return (
        f"{index}\\. *{escape_markdown(title, version=2)}*{year_text}\n"
        f"Status: `{escape_markdown(status, version=2)}` \\| Next: `{escape_markdown(next_check, version=2)}`"
    )


def _tracking_review_keyboard(items: list[TrackingItem]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        item_id = str(item.get("id"))
        title = str(item.get("canonical_title") or item.get("title") or "Movie")
        if len(title) > 26:
            title = f"{title[:23]}..."
        rows.append(
            [
                InlineKeyboardButton(
                    f"🛑 Cancel: {title}",
                    callback_data=f"track_cancel_{item_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="track_back")])
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


async def _handle_schedule_prompt(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not isinstance(query.message, Message):
        return
    user_data = _get_user_data_store(context)
    mark_chat_workflow_active(context, query.message.chat_id, "track")
    clear_tracking_workflow_state(user_data)
    user_data[TRACKING_NEXT_ACTION_KEY] = TRACKING_AWAIT_MOVIE_TITLE
    await safe_edit_message(
        query.message,
        text=(
            "🎬 Send the movie title you want to track for automatic future download\\.\n\n"
            "Only unreleased movies can be scheduled\\."
        ),
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
        await safe_edit_message(
            query.message,
            text="No active scheduled items\\.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="track_back")]]
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    lines = ["*Active Scheduled Items*", ""]
    lines.extend(_format_tracking_item_line(item, idx) for idx, item in enumerate(items, start=1))
    await safe_edit_message(
        query.message,
        text="\n\n".join(lines),
        reply_markup=_tracking_review_keyboard(items),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


def _parse_callback_suffix(data: str, prefix: str) -> str | None:
    if not data.startswith(prefix):
        return None
    return data[len(prefix) :]


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
                [[InlineKeyboardButton("⬅️ Back", callback_data="track_back")]]
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

    candidate = cast(MovieTrackingResolution, candidates[index])
    user_data[TRACKING_SELECTED_INDEX_KEY] = index
    await safe_edit_message(
        query.message,
        text=(
            "*Confirm Schedule*\n\n"
            f"{_candidate_summary_line(candidate)}\n\n"
            "Start tracking this movie?"
        ),
        reply_markup=confirm_cancel_keyboard(
            "✅ Confirm Schedule",
            "track_confirm",
            cancel_label="⬅️ Back",
            cancel_callback="track_back",
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_confirm_candidate(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE
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
                [[InlineKeyboardButton("⬅️ Back", callback_data="track_back")]]
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if not (0 <= selected_index < len(candidates)):
        return
    selected = cast(MovieTrackingResolution, candidates[selected_index])
    if selected.get("is_released"):
        await safe_edit_message(
            query.message,
            text=(
                "❌ This movie appears to be already released for streaming or Blu\\-ray/DVD\\.\n"
                "Only future releases can be scheduled\\."
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="track_back")]]
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
    clear_tracking_workflow_state(user_data)
    mark_chat_workflow_active(context, query.message.chat_id, "track")
    await safe_edit_message(
        query.message,
        text=(
            "✅ Scheduled tracking created\\.\n\n"
            f"*{escape_markdown(str(created.get('canonical_title') or 'Movie'), version=2)}* "
            f"will be monitored until fulfillment or manual cancellation\\."
        ),
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📋 Review Scheduled Items", callback_data="track_review")],
                [InlineKeyboardButton("⬅️ Back To Tracking Menu", callback_data="track_back")],
            ]
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
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
                [[InlineKeyboardButton("⬅️ Back", callback_data="track_review")]]
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    title = str(item.get("canonical_title") or item.get("title") or "Movie")
    await safe_edit_message(
        query.message,
        text=(f"Cancel this scheduled item\\?\n\n*{escape_markdown(title, version=2)}*"),
        reply_markup=confirm_cancel_keyboard(
            "✅ Yes, Cancel",
            f"track_cancel_confirm_{item_id}",
            cancel_label="⬅️ Back",
            cancel_callback="track_review",
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_cancel_item_confirm(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not isinstance(query.message, Message):
        return
    item_id = _parse_callback_suffix(query.data or "", "track_cancel_confirm_")
    if not item_id:
        return
    cancelled = tracking_manager.cancel_tracking_item(
        context.application,
        item_id=item_id,
        chat_id=query.message.chat_id,
    )
    if cancelled:
        await safe_edit_message(
            query.message,
            text="✅ Scheduled item cancelled\\.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📋 Review Scheduled Items", callback_data="track_review")]]
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    await safe_edit_message(
        query.message,
        text="That scheduled item could not be cancelled\\.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("📋 Review Scheduled Items", callback_data="track_review")]]
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
        await _handle_schedule_prompt(query, context)
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
    if action.startswith("track_cancel_confirm_"):
        await _handle_cancel_item_confirm(query, context)
        return
    if action.startswith("track_cancel_"):
        await _handle_cancel_item(query, context)
        return

    logger.warning("Unhandled tracking callback action: %s", action)


async def handle_tracking_workflow_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handles text input for tracking workflow prompts."""
    message = update.message
    if not isinstance(message, Message) or not message.text:
        return

    user_data = _get_user_data_store(context)
    next_action = user_data.get(TRACKING_NEXT_ACTION_KEY)
    if next_action != TRACKING_AWAIT_MOVIE_TITLE:
        return

    title_query = message.text.strip()
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        pass

    chat_id = message.chat_id
    prompt_message_id = get_active_prompt_message_id(context, chat_id)
    if isinstance(prompt_message_id, int):
        await safe_edit_message(
            context.bot,
            text="🔎 Looking up release metadata...",
            chat_id=chat_id,
            message_id=prompt_message_id,
        )
    else:
        status_message = await context.bot.send_message(
            chat_id=chat_id,
            text="🔎 Looking up release metadata...",
        )
        prompt_message_id = status_message.message_id
        set_active_prompt_message_id(context, chat_id, prompt_message_id)

    candidates = await movie_release_dates.find_movie_tracking_candidates(title_query)
    schedulable = [item for item in candidates if not item.get("is_released")]
    if not schedulable:
        clear_tracking_workflow_state(user_data)
        await safe_edit_message(
            context.bot,
            text=(
                "❌ This title appears to be already released, or no future release metadata "
                "could be confirmed\\.\n\n"
                "Try another movie title\\."
            ),
            chat_id=chat_id,
            message_id=prompt_message_id,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data="track_back")]]
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    user_data[TRACKING_CANDIDATES_KEY] = schedulable
    user_data.pop(TRACKING_SELECTED_INDEX_KEY, None)
    user_data.pop(TRACKING_NEXT_ACTION_KEY, None)

    lines = ["*Select A Movie To Schedule*", ""]
    lines.extend(_candidate_summary_line(candidate) for candidate in schedulable[:8])
    rows = [
        [
            InlineKeyboardButton(
                f"{idx + 1}. {_candidate_button_label(candidate)}",
                callback_data=f"track_pick_{idx}",
            )
        ]
        for idx, candidate in enumerate(schedulable[:8])
    ]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="track_back")])

    await safe_edit_message(
        context.bot,
        text="\n".join(lines),
        chat_id=chat_id,
        message_id=prompt_message_id,
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    set_active_prompt_message_id(context, chat_id, prompt_message_id)
