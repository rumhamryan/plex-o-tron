# telegram_bot/utils.py

import math
import os
import re
from typing import Any
from urllib.parse import urlparse

from telegram import Message, Bot
from telegram.error import BadRequest


def get_site_name_from_url(url: str) -> str:
    """
    Extracts a short, readable site name from a URL.

    Examples:
        - "https://yts.mx/..." -> "YTS"
        - "https://1337x.to/..." -> "1337x"
        - "https://eztvx.to/..." -> "EZTVx"

    Args:
        url: The full URL of the website.

    Returns:
        A cleaned, uppercase site name or "Unknown" if parsing fails.
    """
    if not url:
        return "Unknown"
    try:
        # Use urlparse to reliably get the network location (e.g., 'yts.mx')
        netloc = urlparse(url).netloc
        if not netloc:
            return "Unknown"

        # Remove 'www.' prefix if it exists
        if netloc.startswith("www."):
            netloc = netloc[4:]

        # Split the domain by dots and take the primary name
        # (e.g., 'yts' from 'yts.mx', '1337x' from '1337x.to')
        primary_name = netloc.partition(".")[0]

        # Return the cleaned name, capitalized for consistency
        return primary_name.upper()
    except Exception:
        # Fallback for any unexpected parsing error
        return "Unknown"


def format_bytes(size_bytes: int) -> str:
    """Converts bytes into a human-readable string (e.g., KB, MB, GB)."""
    if size_bytes <= 0:
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024))) if size_bytes > 0 else 0
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"


def extract_first_int(text: str) -> int | None:
    """Safely extracts the first integer from a string."""
    if not text:
        return None
    match = re.search(r"\d+", text.strip())
    return int(match.group(0)) if match else None


async def safe_edit_message(bot_or_message: Bot | Message, text: str, **kwargs) -> None:
    """
    Safely edits a message, ignoring 'message is not modified' errors.
    This function can be called in two ways:
    1. safe_edit_message(message_object, "new text")
    2. safe_edit_message(bot_object, "new text", chat_id=123, message_id=456)
    """
    try:
        if isinstance(bot_or_message, Message):
            await bot_or_message.edit_text(text=text, **kwargs)
        else:  # Assumes it's a Bot object
            await bot_or_message.edit_message_text(text=text, **kwargs)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise e


def parse_torrent_name(name: str) -> dict[str, Any]:
    """
    Parses a torrent name to identify if it's a movie or a TV show
    and extracts relevant metadata.
    """
    cleaned_name = re.sub(r"[\._]", " ", name)

    # TV Show Detection: S01E01 or 1x01 formats
    tv_match = re.search(
        r"(?i)\b(S(\d{1,2})E(\d{1,2})|(\d{1,2})x(\d{1,2}))\b", cleaned_name
    )
    if tv_match:
        title = cleaned_name[: tv_match.start()].strip()
        tags_to_remove = [
            r"\[.*?\]",
            r"\(.*?\)",
            r"\b(1080p|720p|480p|x264|x265|hevc|BluRay|WEB-DL|AAC|DTS|HDTV|RM4k)\b",
        ]
        regex_pattern = "|".join(tags_to_remove)
        title = re.sub(regex_pattern, "", title, flags=re.I).strip()
        season = int(tv_match.group(2) or tv_match.group(4))
        episode = int(tv_match.group(3) or tv_match.group(5))
        title = title.rstrip(" _.-([").strip()
        return {"type": "tv", "title": title, "season": season, "episode": episode}

    # Movie Detection: Look for a year (19xx or 20xx)
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", cleaned_name)
    if year_match:
        year = year_match.group(1)
        title = cleaned_name[: year_match.start()].strip()
        # Remove residual trailing separators or brackets before the year
        # e.g., "Happy Gilmore 2 (" -> "Happy Gilmore 2"
        title = title.rstrip(" _.-([").strip()
        return {"type": "movie", "title": title, "year": year}

    # Fallback for names that don't match standard patterns
    tags_to_remove = [
        r"\[.*?\]",
        r"\(.*?\)",
        r"\b(1080p|720p|480p|x264|x265|hevc|BluRay|WEB-DL|AAC|DTS|HDTV|RM4k)\b",
    ]
    regex_pattern = "|".join(tags_to_remove)
    no_ext = os.path.splitext(cleaned_name)[0]
    title = re.sub(regex_pattern, "", no_ext, flags=re.I).strip()
    title = re.sub(r"\s+", " ", title).strip()
    return {"type": "unknown", "title": title}
