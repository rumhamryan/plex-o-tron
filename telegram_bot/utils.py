# telegram_bot/utils.py

import asyncio
from datetime import timedelta
import time
import math
import os
import re
from typing import Any
from urllib.parse import urlparse

from telegram import Message, Bot
from telegram.error import BadRequest, TimedOut, RetryAfter, NetworkError

# In-memory per-message suppression to respect Telegram flood control without
# blocking critical workflows for very long durations.
_edit_suppression_until: dict[tuple[int, int], float] = {}


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


async def safe_edit_message(
    bot_or_message: Bot | Message,
    text: str,
    *,
    max_attempts: int = 3,
    base_delay: float = 0.6,
    max_retry_after: float = 10.0,
    **kwargs,
) -> None:
    """
    Safely edits a message, ignoring 'message is not modified' errors.
    This function can be called in two ways:
    1. safe_edit_message(message_object, "new text")
    2. safe_edit_message(bot_object, "new text", chat_id=123, message_id=456)
    """
    # Determine suppression key (chat_id, message_id) if available
    key: tuple[int, int] | None = None
    chat_id_kw = kwargs.get("chat_id")
    message_id_kw = kwargs.get("message_id")
    if isinstance(bot_or_message, Message):
        try:
            key = (int(bot_or_message.chat_id), int(bot_or_message.message_id))
        except Exception:
            key = None
    elif chat_id_kw is not None and message_id_kw is not None:
        try:
            key = (int(chat_id_kw), int(message_id_kw))
        except Exception:
            key = None

    # If flood control previously told us to wait, respect that by skipping
    if key is not None:
        suppressed_until = _edit_suppression_until.get(key, 0.0)
        if time.monotonic() < suppressed_until:
            return

    attempt = 0
    delay = base_delay
    last_exc: Exception | None = None

    while attempt < max_attempts:
        try:
            if isinstance(bot_or_message, Message):
                await bot_or_message.edit_text(text=text, **kwargs)
            else:  # Assumes it's a Bot object
                await bot_or_message.edit_message_text(text=text, **kwargs)

            # On success, clear any suppression for this message
            if key is not None and key in _edit_suppression_until:
                _edit_suppression_until.pop(key, None)
            return

        except BadRequest as e:
            msg = str(e).lower()
            # Ignore harmless error
            if "message is not modified" in msg:
                return

            # If we can't edit (message missing or not editable), fall back to sending a new message
            recoverable = (
                "message to edit not found" in msg
                or "message can't be edited" in msg
                or "message is too old" in msg
                or "message not found" in msg
            )
            if recoverable:
                if isinstance(bot_or_message, Message):
                    bot = bot_or_message.get_bot()
                    chat_id = bot_or_message.chat_id
                    # Remove edit-only args if present
                    send_kwargs = dict(kwargs)
                    send_kwargs.pop("message_id", None)
                    # Use safe send with retry/backoff for robustness
                    await safe_send_message(
                        bot, chat_id=chat_id, text=text, **send_kwargs
                    )
                    return
                else:
                    # Bot variant requires chat_id in kwargs; drop message_id
                    if "chat_id" in kwargs:
                        send_kwargs = dict(kwargs)
                        send_kwargs.pop("message_id", None)
                        await safe_send_message(
                            bot_or_message, text=text, **send_kwargs
                        )
                        return

            # Unhandled BadRequest: re-raise
            raise e

        except RetryAfter as e:
            # Respect server backoff; if very large, suppress further edits until then
            ra = getattr(e, "retry_after", None)
            if isinstance(ra, timedelta):
                wait = ra.total_seconds()
            else:
                try:
                    wait = float(ra) if ra is not None else delay
                except Exception:
                    wait = delay

            if wait > max_retry_after:
                # Set suppression window and exit without raising to avoid crashing tasks
                if key is not None:
                    _edit_suppression_until[key] = time.monotonic() + wait
                return

            await asyncio.sleep(wait + 0.1)
            last_exc = e

        except (TimedOut, NetworkError) as e:
            # Transient network conditions – exponential backoff with jitter
            await asyncio.sleep(delay)
            delay *= 2
            last_exc = e

        attempt += 1

    # Exhausted retries
    if last_exc is not None:
        raise last_exc
    # Should not reach here, but return safely if we do
    return


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


async def safe_send_message(
    bot_or_context: Bot | Message | Any,
    /,
    chat_id: int | None = None,
    text: str | None = None,
    *,
    max_attempts: int = 3,
    base_delay: float = 0.6,
    **kwargs: Any,
) -> Message:
    """
    Sends a message with retries on transient Telegram/network errors.

    Accepts a Bot instance, or a Message (from which a Bot can be obtained).
    Returns the sent Message on success, or raises the last exception.
    """
    if text is None:
        raise ValueError("safe_send_message requires 'text'.")

    # Resolve Bot and chat_id
    if isinstance(bot_or_context, Message):
        bot: Bot = bot_or_context.get_bot()
        if chat_id is None:
            chat_id = bot_or_context.chat_id
    else:
        bot = bot_or_context  # type: ignore[assignment]

    if chat_id is None:
        raise ValueError("safe_send_message requires 'chat_id'.")

    attempt = 0
    delay = base_delay
    last_exc: Exception | None = None

    while attempt < max_attempts:
        try:
            return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except RetryAfter as e:  # Respect server backoff
            ra = getattr(e, "retry_after", None)
            if isinstance(ra, timedelta):
                wait = ra.total_seconds()
            else:
                wait = float(ra) if ra is not None else delay
            await asyncio.sleep(wait + 0.1)
            last_exc = e
        except (TimedOut, NetworkError) as e:
            # Transient network conditions – exponential backoff with jitter
            await asyncio.sleep(delay)
            delay *= 2
            last_exc = e
        attempt += 1

    # Exhausted retries
    assert last_exc is not None
    raise last_exc
