# telegram_bot/handlers/error_handler.py

import html
import json
import traceback
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..config import logger


async def global_error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Catches all unhandled exceptions and logs them, providing detailed context.
    This prevents the bot from crashing and gives us the exact location of the error.
    """
    # --- Add a guard clause to ensure there's an error to handle. ---
    if not context.error:
        logger.warning("Error handler was called but context.error is None.")
        return

    # 1. Log the exception with the full traceback.
    logger.error("An unhandled exception occurred:", exc_info=context.error)

    # This gets the full traceback for the current exception without needing to access context.error directly.
    tb_string = traceback.format_exc()

    # 3. Gather as much context as possible.
    update_str = update.to_dict() if isinstance(update, Update) else str(update)

    # Create a detailed message for a potential admin notification or advanced logging.
    context_message = (
        f"An exception was raised while handling an update\n"
        f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
        "</pre>\n\n"
        f"<pre>context.chat_data = {html.escape(str(context.chat_data))}</pre>\n\n"
        f"<pre>context.user_data = {html.escape(str(context.user_data))}</pre>\n\n"
        f"<b>Traceback:</b>\n<pre>{html.escape(tb_string)}</pre>"
    )

    # For now, we will print this detailed report to the console.
    print("--- DETAILED EXCEPTION REPORT ---")
    # We will log the context_message to the logger as well for file persistence
    logger.error(f"DETAILED EXCEPTION REPORT:\n{context_message}")
    print("--- END OF REPORT ---")

    # 5. Finally, inform the user that an error occurred, without exposing technical details.
    if isinstance(update, Update) and update.effective_message:
        error_text = (
            "❌ An unexpected error occurred.\n\n"
            "I'm sorry, but I encountered a problem while processing your request\\. "
            "The issue has been automatically logged for review\\. Please try again later\\."
        )
        try:
            await update.effective_message.reply_text(
                text=error_text, parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            logger.error(f"Failed to send the user-facing error message: {e}")
