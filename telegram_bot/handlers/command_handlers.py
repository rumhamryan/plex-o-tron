from __future__ import annotations

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from ..config import logger
from ..services.auth_service import is_user_authorized
from ..services.plex_service import get_plex_server_status, restart_plex_server
from ..ui.keyboards import cancel_only_keyboard, launcher_keyboard
from ..utils import safe_send_message
from ..workflows.navigation import (
    clear_all_workflow_state,
    get_user_data_store,
    mark_chat_workflow_active,
    set_active_prompt_message_id,
)


def get_help_message_text() -> str:
    """Returns the formatted help text for the home-menu UX."""
    return (
        "*Plex\\-o\\-Tron Home Menu*\n\n"
        "Use the buttons to:\n"
        "\\- Search for movies or TV shows\n"
        "\\- Delete media from your library\n"
        "\\- Check Plex status\n"
        "\\- Restart Plex\n"
        "\\- Start the guided link intake flow\n\n"
        "Send any DM message anytime to recover the home menu\\."
    )


async def launch_search_workflow(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> Message:
    """Starts the top-level search launcher prompt."""
    store = get_user_data_store(context)
    clear_all_workflow_state(store)
    mark_chat_workflow_active(context, chat_id, "search")

    prompt = await safe_send_message(
        context.bot,
        chat_id=chat_id,
        text=r"What type of media do you want to search for\?",
        reply_markup=launcher_keyboard(
            "🎬 Movie",
            "search_start_movie",
            "📺 TV Show",
            "search_start_tv",
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    set_active_prompt_message_id(context, chat_id, prompt.message_id)
    return prompt


async def launch_delete_workflow(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> Message:
    """Starts the top-level delete launcher prompt."""
    store = get_user_data_store(context)
    clear_all_workflow_state(store)
    mark_chat_workflow_active(context, chat_id, "delete")

    prompt = await safe_send_message(
        context.bot,
        chat_id=chat_id,
        text="What type of media do you want to delete?",
        reply_markup=launcher_keyboard(
            "🎬 Movie",
            "delete_start_movie",
            "📺 TV Show",
            "delete_start_tv",
        ),
    )
    set_active_prompt_message_id(context, chat_id, prompt.message_id)
    return prompt


async def launch_link_workflow(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> Message:
    """Starts the guided link-ingestion workflow."""
    store = get_user_data_store(context)
    clear_all_workflow_state(store)
    mark_chat_workflow_active(context, chat_id, "link")

    prompt_text = escape_markdown(
        "🔗 Send a magnet link, a .torrent URL, or a webpage URL and I'll analyze it.\n\n"
        "Example: magnet:?xt=urn:btih:...",
        version=2,
    )
    prompt = await safe_send_message(
        context.bot,
        chat_id=chat_id,
        text=prompt_text,
        reply_markup=cancel_only_keyboard(),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    store["link_prompt_message_id"] = prompt.message_id
    set_active_prompt_message_id(context, chat_id, prompt.message_id)
    return prompt


async def launch_help(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> Message:
    """Sends help as a transient message without mutating the home menu."""
    return await safe_send_message(
        context.bot,
        chat_id=chat_id,
        text=get_help_message_text(),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def launch_plex_status(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> None:
    """Reports Plex connectivity using transient status messages."""
    status_message = await safe_send_message(
        context.bot,
        chat_id=chat_id,
        text="Plex Status: 🟡 Checking connection...",
    )
    message_text = await get_plex_server_status(context)
    await status_message.edit_text(text=message_text, parse_mode=ParseMode.MARKDOWN_V2)


async def launch_plex_restart(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> None:
    """Attempts Plex restart and reports result as a transient message."""
    status_message = await safe_send_message(
        context.bot,
        chat_id=chat_id,
        text="Plex Restart: 🟡 Sending restart command...",
    )
    success, message = await restart_plex_server()
    final_text = (
        "✅ *Plex Restart Successful*"
        if success
        else f"❌ *Plex Restart Failed*\n\n{escape_markdown(message, version=2)}"
    )
    await status_message.edit_text(text=final_text, parse_mode=ParseMode.MARKDOWN_V2)


def _get_message_chat_id(update: Update) -> int | None:
    if not isinstance(update.message, Message):
        return None
    return update.message.chat_id


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_user_authorized(update, context):
        return
    chat_id = _get_message_chat_id(update)
    if chat_id is None:
        logger.warning("help_command could not resolve chat id.")
        return
    await launch_help(context, chat_id)


async def links_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_user_authorized(update, context):
        return
    chat_id = _get_message_chat_id(update)
    if chat_id is None:
        logger.warning("links_command could not resolve chat id.")
        return
    await launch_link_workflow(context, chat_id)


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_user_authorized(update, context):
        return
    chat_id = _get_message_chat_id(update)
    if chat_id is None:
        logger.warning("delete_command could not resolve chat id.")
        return
    await launch_delete_workflow(context, chat_id)


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_user_authorized(update, context):
        return
    chat_id = _get_message_chat_id(update)
    if chat_id is None:
        logger.warning("search_command could not resolve chat id.")
        return
    await launch_search_workflow(context, chat_id)


async def plex_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_user_authorized(update, context):
        return
    chat_id = _get_message_chat_id(update)
    if chat_id is None:
        logger.warning("plex_status_command could not resolve chat id.")
        return
    await launch_plex_status(context, chat_id)


async def plex_restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_user_authorized(update, context):
        return
    chat_id = _get_message_chat_id(update)
    if chat_id is None:
        logger.warning("plex_restart_command could not resolve chat id.")
        return
    await launch_plex_restart(context, chat_id)
