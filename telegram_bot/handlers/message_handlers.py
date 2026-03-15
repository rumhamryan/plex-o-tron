import os

from telegram import Message, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ..config import logger
from ..services.auth_service import is_user_authorized
from ..services.media_manager import validate_and_enrich_torrent
from ..services.torrent_service import process_user_input
from ..ui.home_menu import get_home_menu_message_id, show_home_menu
from ..ui.views import send_confirmation_prompt
from ..workflows.delete_workflow import handle_delete_workflow
from ..workflows.navigation import clear_all_workflow_state, get_user_data_store
from ..workflows.search_session import SearchSession
from ..workflows.search_workflow import handle_search_workflow


def _is_private_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type == "private")


async def _delete_user_message_before_menu(message: Message) -> None:
    """Best-effort cleanup so home-menu bootstrap does not leave stray user text."""
    try:
        await message.delete()
    except TelegramError:
        pass


async def _delete_link_example_prompt(context: ContextTypes.DEFAULT_TYPE, *, chat_id: int) -> None:
    """Best-effort removal of the Link workflow example prompt message."""
    user_data = get_user_data_store(context)
    prompt_message_id = user_data.pop("link_prompt_message_id", None)
    if not isinstance(prompt_message_id, int):
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=prompt_message_id)
    except TelegramError:
        pass


def _has_active_home_menu(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> bool:
    application = getattr(context, "application", None)
    if application is None:
        return False
    return get_home_menu_message_id(application, chat_id) is not None


async def handle_link_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles explicit link-ingestion workflow messages.

    This is intentionally not a top-level idle entrypoint. It is only used while
    `active_workflow == "link"`.
    """
    user = update.effective_user
    message = update.message
    chat = update.effective_chat
    if not user or not isinstance(message, Message) or not message.text or not chat:
        logger.warning(
            "handle_link_message: Update received without user/chat/message text. Ignoring."
        )
        return

    user_data = get_user_data_store(context)
    text = message.text.strip()
    logger.info("User %s sent link-workflow input: %s...", user.id, text[:70])

    try:
        progress_message = await message.reply_text("✅ Link received. Analyzing...")
        await message.delete()
    except TelegramError as exc:
        logger.warning("Could not delete user message or reply in link workflow: %s", exc)
        return

    ti = await process_user_input(text, context, progress_message)
    if not ti:
        return

    await _delete_link_example_prompt(context, chat_id=chat.id)

    error_message, parsed_info = await validate_and_enrich_torrent(ti, progress_message)
    if error_message or not parsed_info:
        torrent_file_path = user_data.get("torrent_file_path")
        if isinstance(torrent_file_path, str) and os.path.exists(torrent_file_path):
            os.remove(torrent_file_path)
        return

    await send_confirmation_prompt(progress_message, context, ti, parsed_info)


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Routes all incoming text in DMs.

    Behavior summary:
    - Active workflow text is routed to that workflow.
    - Any idle DM text bootstraps or recovers the home menu.
    """
    if not await is_user_authorized(update, context):
        return

    user = update.effective_user
    message = update.message
    chat = update.effective_chat
    if not user or not isinstance(message, Message) or not message.text or not chat:
        logger.warning(
            "handle_user_message: Update received without a user/chat/valid message text."
        )
        return

    if not _is_private_chat(update):
        logger.info("Ignoring non-private message from user %s.", user.id)
        return

    user_data = get_user_data_store(context)
    active_workflow = user_data.get("active_workflow")

    if active_workflow == "search":
        session = SearchSession.from_user_data(user_data)
        if session.is_active:
            await handle_search_workflow(update, context)
            return
        logger.info("Clearing stale search workflow state for user %s.", user.id)
        clear_all_workflow_state(user_data)
        await _delete_user_message_before_menu(message)
        await show_home_menu(context, chat.id)
        return

    if active_workflow == "delete":
        if user_data.get("next_action"):
            await handle_delete_workflow(update, context)
            return
        logger.info("Clearing stale delete workflow state for user %s.", user.id)
        clear_all_workflow_state(user_data)
        await _delete_user_message_before_menu(message)
        await show_home_menu(context, chat.id)
        return

    if active_workflow == "link":
        await handle_link_message(update, context)
        return

    clear_all_workflow_state(user_data)
    if _has_active_home_menu(context, chat.id):
        await _delete_user_message_before_menu(message)
        return
    await _delete_user_message_before_menu(message)
    await show_home_menu(context, chat.id)


async def handle_search_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Backward-compatible alias for the unified text router."""
    await handle_user_message(update, context)
