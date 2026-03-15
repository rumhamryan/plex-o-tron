from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from ..config import logger

HOME_MENU_MESSAGES_KEY = "home_menu_messages"


def build_home_menu_markup() -> InlineKeyboardMarkup:
    """Builds the reusable inline keyboard for the DM home menu."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Search", callback_data="home_search"),
                InlineKeyboardButton("Delete", callback_data="home_delete"),
                InlineKeyboardButton("Link", callback_data="home_link"),
            ],
            [
                InlineKeyboardButton("Status", callback_data="home_status"),
                InlineKeyboardButton("Restart", callback_data="home_restart"),
                InlineKeyboardButton("Help", callback_data="home_help"),
            ],
        ]
    )


def get_home_menu_text() -> str:
    """Returns the standard launcher text shown above home menu buttons."""
    return "*Plex\\-o\\-Tron Home*\n\nChoose an action:"


def _ensure_home_menu_store(bot_data: MutableMapping[str, Any]) -> dict[int, int]:
    raw_store = bot_data.get(HOME_MENU_MESSAGES_KEY)
    if isinstance(raw_store, dict):
        return raw_store

    store: dict[int, int] = {}
    bot_data[HOME_MENU_MESSAGES_KEY] = store
    return store


def _get_bot_data_from_application(application: Any) -> MutableMapping[str, Any]:
    bot_data = getattr(application, "bot_data", None)
    if not isinstance(bot_data, MutableMapping):
        raise TypeError("application.bot_data must be a mutable mapping.")
    return bot_data


def get_home_menu_message_id(application: Any, chat_id: int) -> int | None:
    """Returns the canonical home-menu message id for a chat, if known."""
    bot_data = _get_bot_data_from_application(application)
    store = _ensure_home_menu_store(bot_data)
    message_id = store.get(int(chat_id))
    return int(message_id) if isinstance(message_id, int) else None


def set_home_menu_message_id(application: Any, chat_id: int, message_id: int) -> None:
    """Stores the canonical home-menu message id for a chat."""
    bot_data = _get_bot_data_from_application(application)
    store = _ensure_home_menu_store(bot_data)
    store[int(chat_id)] = int(message_id)


def clear_home_menu_message_id(application: Any, chat_id: int) -> None:
    """Clears any canonical home-menu message id stored for a chat."""
    bot_data = _get_bot_data_from_application(application)
    store = _ensure_home_menu_store(bot_data)
    store.pop(int(chat_id), None)


def _is_message_not_modified(error: BadRequest) -> bool:
    return "message is not modified" in str(error).lower()


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
    application = getattr(context, "application", None)
    if application is None:
        # Test doubles and lightweight contexts may not expose `application`.
        # Fall back to context.bot_data to preserve canonical-menu tracking.
        application = type("ApplicationFallback", (), {})()
        setattr(application, "bot_data", getattr(context, "bot_data", {}))

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
            rendered = await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=canonical_message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            if isinstance(rendered, Message):
                set_home_menu_message_id(application, chat_id, rendered.message_id)
                return rendered
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
