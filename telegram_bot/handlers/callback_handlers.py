from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..config import logger
from ..services.auth_service import is_user_authorized
from ..services.download_manager import (
    add_collection_to_queue,
    add_download_to_queue,
    add_season_to_queue,
    handle_cancel_all,
    handle_cancel_request,
    handle_pause_resume,
)
from ..ui.home_menu import (
    get_home_menu_message_id,
    set_home_menu_message_id,
    show_home_menu,
)
from ..workflows.delete_workflow import handle_delete_buttons
from ..workflows.navigation import return_to_home
from ..workflows.search_workflow import handle_reject_season_pack, handle_search_buttons
from .command_handlers import (
    launch_delete_workflow,
    launch_help,
    launch_link_workflow,
    launch_plex_restart,
    launch_plex_status,
    launch_search_workflow,
)

HOME_ACTIONS = {
    "home_search",
    "home_delete",
    "home_status",
    "home_restart",
    "home_help",
    "home_link",
    "home_refresh",
}


def _is_private_message(message: Message | None) -> bool:
    return bool(message and message.chat and message.chat.type == "private")


async def _route_home_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE, action: str
) -> bool:
    query = update.callback_query
    if not query or not isinstance(query.message, Message):
        return False

    if not _is_private_message(query.message):
        await query.answer(text="Please use this menu in a private chat.", show_alert=False)
        return True

    chat_id = query.message.chat_id
    application = getattr(context, "application", None)
    if application is None:
        logger.error("Callback context is missing application; cannot route home action.")
        await query.answer(text="Menu unavailable right now. Please send any message to recover.")
        return True

    canonical_message_id = get_home_menu_message_id(application, chat_id)
    if canonical_message_id is not None and canonical_message_id != query.message.message_id:
        await query.answer(text="That menu is stale. Refreshing...", show_alert=False)
        await show_home_menu(context, chat_id)
        return True

    if canonical_message_id is None:
        set_home_menu_message_id(application, chat_id, query.message.message_id)

    await query.answer()

    if action == "home_search":
        await launch_search_workflow(context, chat_id)
    elif action == "home_delete":
        await launch_delete_workflow(context, chat_id)
    elif action == "home_status":
        await launch_plex_status(context, chat_id)
    elif action == "home_restart":
        await launch_plex_restart(context, chat_id)
    elif action == "home_help":
        await launch_help(context, chat_id)
    elif action == "home_link":
        await launch_link_workflow(context, chat_id)
    elif action == "home_refresh":
        await show_home_menu(context, chat_id)
    else:
        return False

    return True


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles all callback queries from inline buttons. Acts as a central router.
    """
    if not await is_user_authorized(update, context):
        return

    query = update.callback_query
    if not query or not query.data:
        return

    action = query.data

    if action in HOME_ACTIONS:
        handled = await _route_home_action(update, context, action)
        if handled:
            return

    await query.answer()

    if action.startswith("search_"):
        await handle_search_buttons(update, context)

    elif action.startswith("delete_") or action == "confirm_delete":
        await handle_delete_buttons(update, context)

    elif action == "confirm_download":
        await add_download_to_queue(update, context)
    elif action == "confirm_season_download":
        await add_season_to_queue(update, context)
    elif action == "confirm_collection_download":
        await add_collection_to_queue(update, context)
    elif action == "reject_season_pack":
        await handle_reject_season_pack(update, context)

    elif action == "pause_resume":
        await handle_pause_resume(update, context)

    elif action.startswith("cancel_"):
        if action == "cancel_operation":
            if isinstance(query.message, Message):
                await return_to_home(
                    context,
                    query.message.chat_id,
                    source_message=query.message,
                    message_text="Operation cancelled\\.",
                    message_parse_mode=ParseMode.MARKDOWN_V2,
                    replace_home_menu=True,
                )
            else:
                await query.answer(text="Operation cancelled.", show_alert=False)
        elif action in {"cancel_all", "cancel_all_confirm", "cancel_all_deny"}:
            await handle_cancel_all(update, context)
        else:
            await handle_cancel_request(update, context)

    else:
        logger.warning("Received an unhandled callback query action: %s", action)
