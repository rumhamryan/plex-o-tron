from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from telegram import Message
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ..config import logger
from ..ui.home_menu import delete_home_menu_message, show_home_menu
from ..utils import safe_send_message
from .search_session import clear_search_session

DELETE_WORKFLOW_KEYS = (
    "show_path_to_delete",
    "next_action",
    "prompt_message_id",
    "season_to_delete_num",
    "selection_choices",
    "selection_target_kind",
    "path_to_delete",
    "delete_target_kind",
)

LINK_WORKFLOW_KEYS = (
    "pending_magnet_link",
    "pending_info_url",
    "torrent_file_path",
    "temp_magnet_choices_details",
    "pending_torrent",
    "link_prompt_message_id",
)


def get_user_data_store(context: ContextTypes.DEFAULT_TYPE) -> MutableMapping[str, Any]:
    if context.user_data is None:
        context.user_data = {}
    return context.user_data


def clear_delete_workflow_state(user_data: MutableMapping[str, Any] | None) -> None:
    if not isinstance(user_data, MutableMapping):
        return
    for key in DELETE_WORKFLOW_KEYS:
        user_data.pop(key, None)


def clear_link_workflow_state(user_data: MutableMapping[str, Any] | None) -> None:
    if not isinstance(user_data, MutableMapping):
        return
    for key in LINK_WORKFLOW_KEYS:
        user_data.pop(key, None)


def clear_all_workflow_state(user_data: MutableMapping[str, Any] | None) -> None:
    if not isinstance(user_data, MutableMapping):
        return
    clear_search_session(user_data)
    clear_delete_workflow_state(user_data)
    clear_link_workflow_state(user_data)
    user_data.pop("active_workflow", None)


async def return_to_home(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    *,
    source_message: Message | None = None,
    message_text: str | None = None,
    message_parse_mode: str | None = ParseMode.MARKDOWN_V2,
    replace_home_menu: bool = False,
) -> None:
    """
    Resets conversational workflow state and renders the shared home menu.

    Best-effort cleanup is intentionally non-blocking; failures should not prevent recovery.
    """
    user_data = get_user_data_store(context)
    clear_all_workflow_state(user_data)

    if isinstance(source_message, Message):
        try:
            await source_message.delete()
        except TelegramError:
            pass

    if replace_home_menu:
        await delete_home_menu_message(context, chat_id)

    if message_text:
        await safe_send_message(
            context.bot,
            chat_id=chat_id,
            text=message_text,
            parse_mode=message_parse_mode,
        )

    try:
        await show_home_menu(context, chat_id)
    except Exception:
        logger.exception("Failed to render home menu for chat %s", chat_id)
