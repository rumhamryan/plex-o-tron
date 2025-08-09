# telegram_bot/handlers/message_handlers.py

import os
from telegram import Update, Message
from telegram.ext import ContextTypes
from telegram.error import BadRequest

# --- Refactored Imports: Using absolute paths for clarity and IDE compatibility ---
from ..config import logger
from ..services.auth_service import is_user_authorized
from ..services.torrent_service import process_user_input
from ..services.media_manager import validate_and_enrich_torrent
from ..workflows.delete_workflow import handle_delete_workflow
from ..workflows.search_workflow import handle_search_workflow
from ..ui.views import send_confirmation_prompt


async def handle_link_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles messages that are identified as links (magnet or http).
    This function initiates the download validation and confirmation process.
    """
    # Authorization must be the first step.
    if not await is_user_authorized(update, context):
        return

    # --- Refactored Guard Clause: Ensure user and message objects exist before proceeding ---
    user = update.effective_user
    message = update.message
    if not user or not isinstance(message, Message) or not message.text:
        logger.warning("handle_link_message: Update received without a user or valid message text. Ignoring.")
        return
        
    if context.user_data is None:
        context.user_data = {}

    text = message.text.strip()
    logger.info(f"User {user.id} sent a link: {text[:70]}...")

    # Acknowledge receipt and delete the original link for privacy/cleanliness
    try:
        progress_message = await message.reply_text("✅ Link received. Analyzing...")
        await message.delete()
    except BadRequest as e:
        logger.warning(f"Could not delete user message or reply: {e}")
        return  # Cannot proceed without a message to edit

    # --- Delegate to Services ---
    # 1. Process the input to get torrent info (ti)
    ti = await process_user_input(text, context, progress_message)
    if not ti:
        return

    # 2. Validate the torrent and enrich its metadata
    error_message, parsed_info = await validate_and_enrich_torrent(ti, progress_message)
    if error_message or not parsed_info:
        if 'torrent_file_path' in context.user_data and os.path.exists(context.user_data['torrent_file_path']):
            os.remove(context.user_data['torrent_file_path'])
        return

    # 3. Send the final confirmation prompt to the user
    await send_confirmation_prompt(progress_message, context, ti, parsed_info)


async def handle_search_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Routes general text messages to the appropriate workflow handler
    based on the 'active_workflow' state stored in user_data.
    """
    if not await is_user_authorized(update, context):
        return

    # --- Refactored Guard Clause: Ensure user and message objects exist ---
    user = update.effective_user
    message = update.message
    if not user or not isinstance(message, Message) or not message.text:
        logger.warning("handle_search_message: Update received without a user or valid message text. Ignoring.")
        return
        
    if context.user_data is None:
        context.user_data = {}

    # Check for an active workflow and route the message accordingly
    active_workflow = context.user_data.get('active_workflow')

    if active_workflow == 'search':
        await handle_search_workflow(update, context)
    elif active_workflow == 'delete':
        await handle_delete_workflow(update, context)
    else:
        # If no workflow is active, ignore the message to keep the bot from responding to random text.
        logger.info(f"Received a text message from user {user.id} with no active workflow.")