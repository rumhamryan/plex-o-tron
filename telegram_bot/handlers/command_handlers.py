# telegram_bot/handlers/command_handlers.py

import platform
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.helpers import escape_markdown

from ..config import logger
from ..services.auth_service import is_user_authorized
from ..services.plex_service import get_plex_server_status, restart_plex_server


def get_help_message_text() -> str:
    """Returns the formatted help message string."""
    # --- FIX: Use a raw string (r"...") to prevent Python from interpreting backslashes ---
    # This ensures the backslashes are passed to Telegram for MarkdownV2 parsing.
    return r"""Here are the available commands:

`delete`   \- Delete Movie or TV Show\.
`help`       \- Display this message\.
`links`      \- List popular torrent sites\.
`restart`  \- Restart the Plex Server\.
`search`   \- Search for a Movie or TV Show\.
`status`   \- Check Plex server status\.
"""


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a formatted list of available commands."""
    if not await is_user_authorized(update, context):
        return
    if not isinstance(update.message, Message):
        return

    try:
        await update.message.delete()
    except BadRequest:
        pass  # Ignore if message is old or bot lacks permissions

    chat = update.effective_chat
    if not chat:
        logger.warning("help_command was triggered but could not find an effective_chat.")
        return

    message_text = get_help_message_text()
    await context.bot.send_message(
        chat_id=chat.id,
        text=message_text,
        parse_mode=ParseMode.MARKDOWN_V2
    )


async def links_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a message with a list of popular torrent sites."""
    if not await is_user_authorized(update, context):
        return
    if not isinstance(update.message, Message):
        return

    try:
        await update.message.delete()
    except BadRequest:
        pass

    chat = update.effective_chat
    if not chat:
        logger.warning("links_command was triggered but could not find an effective_chat.")
        return

    message_text = (
        "I can scrape webpages for magnet and torrent links. Send me a URL!\n\n"
        "**For Movies:**\n"
        "https://yts.mx/\n"
        "https://1337x.to/\n"
        "https://thepiratebay.org/\n\n"
        "**For TV Shows:**\n"
        "https://eztvx.to/\n"
        "https://1337x.to/"
    )
    await context.bot.send_message(chat_id=chat.id, text=message_text, parse_mode=ParseMode.HTML)


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts the conversation to delete media from the library."""
    if not await is_user_authorized(update, context):
        return
    if not isinstance(update.message, Message) or context.user_data is None:
        return

    try:
        await update.message.delete()
    except BadRequest:
        pass

    # Set the active workflow to 'delete' to route future text messages correctly
    context.user_data['active_workflow'] = 'delete'

    keyboard = [
        [
            InlineKeyboardButton("🎬 Movie", callback_data="delete_start_movie"),
            InlineKeyboardButton("📺 TV Show", callback_data="delete_start_tv"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    message_text = "What type of media do you want to delete?"

    await update.message.reply_text(text=message_text, reply_markup=reply_markup)


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts the conversation to search for new media."""
    if not await is_user_authorized(update, context):
        return

    # --- Refactored Guard Clause: Ensure all required objects exist before use ---
    user = update.effective_user
    message = update.message
    user_data = context.user_data

    if not user or not isinstance(message, Message) or user_data is None:
        logger.warning("search_command cannot proceed without user, message, or user_data.")
        return

    # With the guard clause, `user.id` is now safe to access, resolving the IDE error.
    logger.info(f"User {user.id} initiated /search command.")

    try:
        await message.delete()
    except BadRequest:
        pass

    # Set the active workflow to 'search' to route future text messages correctly
    user_data['active_workflow'] = 'search'

    keyboard = [
        [
            InlineKeyboardButton("🎬 Movie", callback_data="search_start_movie"),
            InlineKeyboardButton("📺 TV Show", callback_data="search_start_tv"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    # --- FIX: Use a raw string (r"...") to prevent the warning for `\?` ---
    message_text = r"What type of media do you want to search for\?"

    await message.reply_text(
        text=message_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN_V2
    )


async def plex_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Checks and reports the connection status to the Plex Media Server."""
    if not await is_user_authorized(update, context):
        return
    if not isinstance(update.message, Message):
        return

    try:
        await update.message.delete()
    except BadRequest:
        pass

    chat = update.effective_chat
    if not chat:
        logger.warning("plex_status_command was triggered but could not find an effective_chat.")
        return

    status_message = await context.bot.send_message(chat_id=chat.id, text="Plex Status: 🟡 Checking connection...")

    # Delegate the logic to the plex_service module
    message_text = await get_plex_server_status(context)

    await status_message.edit_text(text=message_text, parse_mode=ParseMode.MARKDOWN_V2)


async def plex_restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restarts the Plex server using a predefined shell script."""
    if not await is_user_authorized(update, context):
        return
    if not isinstance(update.message, Message):
        return

    try:
        await update.message.delete()
    except BadRequest:
        pass

    chat = update.effective_chat
    if not chat:
        logger.warning("plex_restart_command was triggered but could not find an effective_chat.")
        return

    if platform.system() != "Linux":
        await context.bot.send_message(chat_id=chat.id, text="This command is configured to run on Linux only.")
        return

    status_message = await context.bot.send_message(chat_id=chat.id, text="Plex Restart: 🟡 Sending restart command...")

    # Delegate the restart logic to the plex_service module
    success, message = await restart_plex_server()

    final_text = f"✅ *Plex Restart Successful*" if success else f"❌ *Plex Restart Failed*\n\n{escape_markdown(message)}"
    await status_message.edit_text(text=final_text, parse_mode=ParseMode.MARKDOWN_V2)