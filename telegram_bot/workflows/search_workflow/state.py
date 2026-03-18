# telegram_bot/workflows/search_workflow/state.py

from typing import Any, MutableMapping

from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ...utils import (
    safe_send_message,
)
from ..navigation import set_active_prompt_message_id
from ..search_session import (
    SearchSession,
    clear_search_session,
)


def _get_user_data_store(
    context: ContextTypes.DEFAULT_TYPE,
) -> MutableMapping[str, Any]:
    if context.user_data is None:
        context.user_data = {}
    return context.user_data


def _get_session(context: ContextTypes.DEFAULT_TYPE) -> SearchSession:
    return SearchSession.from_user_data(_get_user_data_store(context))


def _save_session(context: ContextTypes.DEFAULT_TYPE, session: SearchSession) -> None:
    session.save(_get_user_data_store(context))


def _get_callback_data(query: CallbackQuery) -> str:
    """Returns callback data as a string even when Telegram omits it."""
    return query.data or ""


async def _send_prompt(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    session: SearchSession | None = None,
):
    """Sends a conversational prompt and stores its ID for later cleanup."""
    prompt_message = await safe_send_message(
        context.bot,
        chat_id,
        text,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    if session is None:
        session = _get_session(context)
    session.prompt_message_id = prompt_message.message_id
    _save_session(context, session)
    set_active_prompt_message_id(context, chat_id, prompt_message.message_id)


def _clear_search_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clears persisted search session state (legacy helper for tests)."""
    clear_search_session(getattr(context, "user_data", None))
