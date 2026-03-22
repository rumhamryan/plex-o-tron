from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from ..config import logger
from ..workflows.navigation import (
    clear_home_menu_message_id_for_application,
    get_home_menu_message_id_from_application,
    set_home_menu_message_id_for_application,
)


def build_home_menu_markup() -> InlineKeyboardMarkup:
    """Builds the reusable inline keyboard for the DM home menu."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Search", callback_data="home_search"),
                InlineKeyboardButton("Delete", callback_data="home_delete"),
            ],
            [
                InlineKeyboardButton("Track", callback_data="home_track"),
                InlineKeyboardButton("Link", callback_data="home_link"),
            ],
            [
                InlineKeyboardButton("Status", callback_data="home_status"),
                InlineKeyboardButton("Restart", callback_data="home_restart"),
            ],
            [
                InlineKeyboardButton("Help", callback_data="home_help"),
            ],
        ]
    )


def get_home_menu_text() -> str:
    """Returns the standard launcher text shown above home menu buttons."""
    return "*Plex\\-o\\-Tron*\nManage search, downloads, and Plex maintenance from here\\."


def _get_application_from_context(context: ContextTypes.DEFAULT_TYPE) -> Any:
    application = getattr(context, "application", None)
    if application is not None:
        return application

    fallback = type("ApplicationFallback", (), {})()
    setattr(fallback, "bot_data", getattr(context, "bot_data", {}))
    return fallback


def get_home_menu_message_id(application: Any, chat_id: int) -> int | None:
    """Returns the canonical home-menu message id for a chat, if known."""
    return get_home_menu_message_id_from_application(application, chat_id)


def set_home_menu_message_id(application: Any, chat_id: int, message_id: int) -> None:
    """Stores the canonical home-menu message id for a chat."""
    set_home_menu_message_id_for_application(application, chat_id, message_id)


def clear_home_menu_message_id(application: Any, chat_id: int) -> None:
    """Clears any canonical home-menu message id stored for a chat."""
    clear_home_menu_message_id_for_application(application, chat_id)


def _is_message_not_modified(error: BadRequest) -> bool:
    return "message is not modified" in str(error).lower()


async def delete_home_menu_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Deletes the canonical home menu message for a chat, if it exists."""
    application = _get_application_from_context(context)
    message_id = get_home_menu_message_id(application, chat_id)
    if message_id is None:
        return

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError as exc:
        logger.info("Failed to delete canonical home menu for chat %s: %s", chat_id, exc)
    finally:
        clear_home_menu_message_id(application, chat_id)


async def _try_edit_target_message(
    target_message: Message,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> Message | None:
    try:
        edited = await target_message.edit_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except BadRequest as exc:
        if _is_message_not_modified(exc):
            return target_message
        raise
    return edited if isinstance(edited, Message) else target_message


async def show_home_menu(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    *,
    target_message: Message | None = None,
) -> Message:
    """
    Renders the home menu.

    Rendering strategy:
    1. Try editing the provided target message.
    2. Otherwise try editing the canonical stored home-menu message.
    3. If editing fails, send a fresh launcher and make it canonical.
    """
    application = _get_application_from_context(context)

    text = get_home_menu_text()
    reply_markup = build_home_menu_markup()

    if isinstance(target_message, Message):
        try:
            rendered = await _try_edit_target_message(
                target_message,
                text=text,
                reply_markup=reply_markup,
            )
            if rendered is not None:
                set_home_menu_message_id(application, chat_id, rendered.message_id)
                return rendered
        except TelegramError as exc:
            logger.info("Failed to edit provided home-menu target message: %s", exc)

    canonical_message_id = get_home_menu_message_id(application, chat_id)
    if canonical_message_id is not None:
        try:
            edited_result = await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=canonical_message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            if isinstance(edited_result, Message):
                set_home_menu_message_id(application, chat_id, edited_result.message_id)
                return edited_result
            return await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except BadRequest as exc:
            if _is_message_not_modified(exc) and isinstance(target_message, Message):
                set_home_menu_message_id(application, chat_id, target_message.message_id)
                return target_message
            logger.info("Canonical home menu message stale for chat %s: %s", chat_id, exc)
            clear_home_menu_message_id(application, chat_id)
        except TelegramError as exc:
            logger.info("Failed to edit canonical home-menu message for chat %s: %s", chat_id, exc)
            clear_home_menu_message_id(application, chat_id)

    fresh_message = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    set_home_menu_message_id(application, chat_id, fresh_message.message_id)
    return fresh_message
