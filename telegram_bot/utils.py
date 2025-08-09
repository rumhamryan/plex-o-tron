# telegram_bot/utils.py

import math
import re
from typing import Optional, Union

from telegram import Message, Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest
# The escape_markdown function is no longer imported or defined here.
# Other modules will import it directly from telegram.helpers.


def format_bytes(size_bytes: int) -> str:
    """Converts bytes into a human-readable string (e.g., KB, MB, GB)."""
    if size_bytes <= 0:
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024))) if size_bytes > 0 else 0
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"


def extract_first_int(text: str) -> Optional[int]:
    """Safely extracts the first integer from a string."""
    if not text:
        return None
    match = re.search(r'\d+', text.strip())
    return int(match.group(0)) if match else None


async def safe_edit_message(
    bot_or_message: Union[Bot, Message],
    text: str,
    **kwargs
) -> None:
    """
    Safely edits a message, ignoring 'message is not modified' errors.
    This function can be called in two ways:
    1. safe_edit_message(message_object, "new text")
    2. safe_edit_message(bot_object, "new text", chat_id=123, message_id=456)
    """
    try:
        if isinstance(bot_or_message, Message):
            await bot_or_message.edit_text(text=text, **kwargs)
        else: # Assumes it's a Bot object
            await bot_or_message.edit_message_text(text=text, **kwargs)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise e