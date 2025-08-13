# telegram_bot/handlers/callback_handlers.py

from telegram import Update
from telegram.ext import ContextTypes

# --- Refactored Imports: Using absolute paths for clarity ---
from ..config import logger
from ..services.auth_service import is_user_authorized
from ..services.download_manager import (
    add_download_to_queue,
    add_season_to_queue,
    handle_cancel_request,
    handle_pause_request,
    handle_resume_request,
)
from ..workflows.delete_workflow import handle_delete_buttons
from ..workflows.search_workflow import handle_search_buttons


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles all callback queries from inline buttons. Acts as a central router.

    This function inspects the `callback_data` to delegate the action to the
    appropriate handler in the search, delete, or download management workflows.
    """
    if not await is_user_authorized(update, context):
        return

    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()
    action = query.data

    # --- Routing Logic ---
    if action.startswith("search_"):
        await handle_search_buttons(update, context)

    elif action.startswith("delete_") or action == "confirm_delete":
        await handle_delete_buttons(update, context)

    elif action == "confirm_download":
        await add_download_to_queue(update, context)
    elif action == "confirm_season_download":
        await add_season_to_queue(update, context)

    elif action == "pause_download":
        await handle_pause_request(update, context)

    elif action == "resume_download":
        await handle_resume_request(update, context)

    elif action.startswith("cancel_"):
        # Handles cancel_operation, cancel_download, and confirm_cancel
        if action == "cancel_operation":
            await query.edit_message_text(text="Operation cancelled.")
        else:
            await handle_cancel_request(update, context)

    else:
        logger.warning(f"Received an unhandled callback query action: {action}")
