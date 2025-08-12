# telegram_bot/services/auth_service.py

from telegram import Update
from telegram.ext import ContextTypes

from telegram_bot.config import logger


async def is_user_authorized(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """
    Checks if the user interacting with the bot is on the allowlist.

    This function reads the `ALLOWED_USER_IDS` from the bot's context data
    and compares it against the ID of the user who sent the update. If the
    user is not authorized, it logs a warning and sends them a rejection
    message.

    Returns:
        True if the user is authorized, False otherwise.
    """
    # It's possible for some updates (e.g., in channels) to not have an effective_user.
    user = update.effective_user
    if not user:
        logger.warning(
            "Authorization check failed: No effective user found in the update."
        )
        return False

    allowed_ids = context.bot_data.get("ALLOWED_USER_IDS", [])
    if user.id not in allowed_ids:
        logger.warning(
            f"Unauthorized access attempt by user ID: {user.id} ({user.username})"
        )
        # Notify the user they are not authorized to use the bot.
        await context.bot.send_message(
            chat_id=user.id, text="❌ You are not authorized to use this bot."
        )
        return False

    # If the user's ID is in the list, they are authorized.
    return True
