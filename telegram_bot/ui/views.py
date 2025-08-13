# telegram_bot/ui/views.py

from typing import Any, Dict

import libtorrent as lt
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

# --- Absolute imports for clarity and consistency ---
from telegram_bot.config import logger
from telegram_bot.services.media_manager import (
    get_dominant_file_type,
    parse_resolution_from_name,
)
from telegram_bot.utils import format_bytes, safe_edit_message


async def send_confirmation_prompt(
    progress_message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    ti: lt.torrent_info,  # type: ignore
    parsed_info: dict[str, Any],
) -> None:
    """
    Formats and sends the final confirmation message with 'Confirm' and 'Cancel' buttons.
    """
    if context.user_data is None:
        context.user_data = {}

    # Build the display name for the confirmation message
    raw_display_name = ""
    if parsed_info["type"] == "movie":
        raw_display_name = (
            f"{parsed_info.get('title', 'Unknown')} ({parsed_info.get('year', 'N/A')})"
        )
    elif parsed_info["type"] == "tv":
        base_name = f"{parsed_info.get('title', 'Unknown')} - S{parsed_info.get('season', 0):02d}E{parsed_info.get('episode', 0):02d}"
        episode_title = parsed_info.get("episode_title")
        raw_display_name = (
            f"{base_name} - {episode_title}" if episode_title else base_name
        )
    else:
        raw_display_name = parsed_info.get("title", "Unknown")

    # Create escaped versions for the message
    escaped_display_name = escape_markdown(raw_display_name, version=2)
    resolution = parse_resolution_from_name(ti.name())
    file_type_str = get_dominant_file_type(ti.files())
    total_size_str = format_bytes(ti.total_size())
    details_line = f"{resolution} | {file_type_str} | {total_size_str}"
    escaped_details_line = escape_markdown(details_line)

    message_text = (
        f"✅ *Validation Passed*\n\n"
        f"*Name:* {escaped_display_name}\n"
        f"*Details:* `{escaped_details_line}`\n\n"
        f"Do you want to start this download?"
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "✅ Confirm Download", callback_data="confirm_download"
            ),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Store all necessary info in 'pending_torrent' for when the user clicks 'Confirm'
    source_type = "magnet" if "pending_magnet_link" in context.user_data else "file"
    source_value = context.user_data.pop(
        "pending_magnet_link", None
    ) or context.user_data.get("torrent_file_path")

    if not source_value:
        logger.error(
            "Could not determine source for pending torrent. Aborting confirmation."
        )
        await safe_edit_message(
            progress_message,
            "❌ An internal error occurred. Could not prepare the download.",
        )
        return

    context.user_data["pending_torrent"] = {
        "type": source_type,
        "value": source_value,
        "clean_name": raw_display_name,
        "parsed_info": parsed_info,
        "original_message_id": progress_message.message_id,
    }

    await safe_edit_message(
        progress_message,
        text=message_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
