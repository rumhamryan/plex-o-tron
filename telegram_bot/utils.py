# telegram_bot/utils.py

import asyncio
import math
import os
import re
import time
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from telegram import Bot, Message
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from .config import logger

# In-memory per-message suppression to respect Telegram flood control without
# blocking critical workflows for very long durations.
_edit_suppression_until: dict[tuple[int, int], float] = {}


def get_site_name_from_url(url: str) -> str:
    """
    Extracts a short, readable site name from a URL.

    Examples:
        - "https://yts.lt/..." -> "YTS"
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
        # Use urlparse to reliably get the network location (e.g., 'yts.lt')
        netloc = urlparse(url).netloc
        if not netloc:
            return "Unknown"

        # Remove 'www.' prefix if it exists
        if netloc.startswith("www."):
            netloc = netloc[4:]

        # Split the domain by dots and take the primary name
        # (e.g., 'yts' from 'yts.lt', '1337x' from '1337x.to')
        primary_name = netloc.partition(".")[0]

        # Return the cleaned name, capitalized for consistency
        return primary_name.upper()
    except Exception:
        # Fallback for any unexpected parsing error
        return "Unknown"


def format_bytes(size_bytes: int) -> str:
    """Converts bytes into a human-readable binary string (e.g., KiB, MiB, GiB)."""
    if size_bytes <= 0:
        return "0B"
    size_name = ("B", "KiB", "MiB", "GiB", "TiB")
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
            if "can't parse" in msg or "can't find end of" in msg:
                logger.error(
                    "[UI] Markdown parse error while editing message: %s\nText snippet: %s",
                    e,
                    text[:500] if isinstance(text, str) else text,
                )

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
                    await safe_send_message(bot, chat_id=chat_id, text=text, **send_kwargs)
                    return
                else:
                    # Bot variant requires chat_id in kwargs; drop message_id
                    if "chat_id" in kwargs:
                        send_kwargs = dict(kwargs)
                        send_kwargs.pop("message_id", None)
                        await safe_send_message(bot_or_message, text=text, **send_kwargs)
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
                # Set suppression window and attempt a fallback send so the user still sees the update
                if key is not None:
                    _edit_suppression_until[key] = time.monotonic() + wait

                try:
                    if isinstance(bot_or_message, Message):
                        bot = bot_or_message.get_bot()
                        chat_id = bot_or_message.chat_id
                        send_kwargs = dict(kwargs)
                        send_kwargs.pop("message_id", None)
                        await safe_send_message(bot, chat_id=chat_id, text=text, **send_kwargs)
                        return
                    if "chat_id" in kwargs:
                        send_kwargs = dict(kwargs)
                        chat_id = send_kwargs.pop("chat_id")
                        send_kwargs.pop("message_id", None)
                        await safe_send_message(
                            bot_or_message, chat_id=chat_id, text=text, **send_kwargs
                        )
                        return
                except Exception as send_exc:
                    last_exc = send_exc
                    break

                last_exc = e
                break

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
    Parses a torrent name to identify if it's a movie, a single TV episode,
    or a TV season pack, and extracts relevant metadata.

    Rules of thumb:
    - Replace dots/underscores with spaces to normalize tokens.
    - Prefer explicit episode patterns (S01E01 or 1x01).
    - Detect season-only patterns (e.g., "S01", "Season 1") as season packs.
    - Strip bracketed content and common quality tags from the title segment.
    """
    cleaned_name = re.sub(r"[\._]", " ", name)

    # 1) TV episode: S01E01 or 1x01
    tv_match = re.search(
        r"(?i)\b(S(\d{1,2})E(\d{1,2})|(\d{1,2})x(\d{1,2}))\b",
        cleaned_name,
    )
    if tv_match:
        title = cleaned_name[: tv_match.start()].strip()
        tags_to_remove = [
            r"\[.*?\]",
            r"\(.*?\)",
            r"\b(1080p|720p|480p|x264|x265|hevc|BluRay|WEB-DL|AAC|DTS|HDTV|RM4k)\b",
        ]
        title = re.sub("|".join(tags_to_remove), "", title, flags=re.I).strip()
        season = int(tv_match.group(2) or tv_match.group(4))
        episode = int(tv_match.group(3) or tv_match.group(5))
        title = title.rstrip(" _.-([").strip()
        return {
            "type": "tv",
            "title": title,
            "season": season,
            "episode": episode,
        }

    # 2) TV season pack: S01 (without E##) or "Season 1" style
    season_pack_match_s = re.search(r"(?i)\bS(\d{1,2})\b", cleaned_name)
    season_pack_match_word = re.search(r"(?i)\bSeason\s+(\d{1,2})\b", cleaned_name)
    if season_pack_match_s or season_pack_match_word:
        # Choose the earliest season token occurrence to slice the title reliably
        cands = []
        if season_pack_match_s:
            cands.append((season_pack_match_s.start(), int(season_pack_match_s.group(1))))
        if season_pack_match_word:
            cands.append((season_pack_match_word.start(), int(season_pack_match_word.group(1))))
        cands.sort(key=lambda x: x[0])
        pos, season_num = cands[0]

        title = cleaned_name[:pos].strip()
        tags_to_remove = [
            r"\[.*?\]",
            r"\(.*?\)",
            r"\b(1080p|720p|480p|x264|x265|hevc|BluRay|WEB-DL|AAC|DTS|HDTV|RM4k|COMPLETE|PACK)\b",
        ]
        title = re.sub("|".join(tags_to_remove), "", title, flags=re.I).strip()
        title = title.rstrip(" _.-([").strip()
        if title:
            return {
                "type": "tv",
                "title": title,
                "season": season_num,
                "is_season_pack": True,
            }

    # 3) Movie: Look for a year (19xx or 20xx)
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", cleaned_name)
    if year_match:
        year = year_match.group(1)
        title = cleaned_name[: year_match.start()].strip()
        title = title.rstrip(" _.-([").strip()
        return {"type": "movie", "title": title, "year": year}

    # 4) Fallback: generic cleanup
    tags_to_remove = [
        r"\[.*?\]",
        r"\(.*?\)",
        r"\b(1080p|720p|480p|x264|x265|hevc|BluRay|WEB-DL|AAC|DTS|HDTV|RM4k)\b",
    ]
    no_ext = os.path.splitext(cleaned_name)[0]
    title = re.sub("|".join(tags_to_remove), "", no_ext, flags=re.I).strip()
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
        except BadRequest as e:
            msg = str(e).lower()
            if "can't parse" in msg or "can't find end of" in msg:
                logger.error(
                    "[UI] Markdown parse error while sending message: %s\nText snippet: %s",
                    e,
                    text[:500] if isinstance(text, str) else text,
                )
            raise
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


_CODEC_PATTERNS = {
    # Prefer more specific/modern codecs first
    "av1": re.compile(r"(?i)\bav1\b"),
    # x265 / HEVC / H.265 with common variants (spaces/dots)
    "x265": re.compile(r"(?i)\b(?:x\s*265|h\s*[.\s]?265|hevc)\b"),
    # x264 / AVC / H.264 with common variants (spaces/dots)
    "x264": re.compile(r"(?i)\b(?:x\s*264|h\s*[.\s]?264|h264|avc)\b"),
}


def parse_codec(title: str) -> str | None:
    """Extracts codec information from a torrent title.

    Handles common variants and spacing/punctuation, e.g.:
    - "H264", "H.264", "H 264", "x264", "AVC" -> "x264"
    - "H265", "H.265", "H 265", "x265", "HEVC" -> "x265"
    - "AV1" -> "av1"
    """
    for normalized, pattern in _CODEC_PATTERNS.items():
        if pattern.search(title):
            return normalized
    return None


_VIDEO_FORMAT_ORDER: tuple[str, ...] = (
    "dolby_vision",
    "hdr10_plus",
    "hdr10",
    "hdr",
    "hlg",
    "sdr",
)
_AUDIO_FORMAT_ORDER: tuple[str, ...] = (
    "atmos",
    "truehd",
    "ddp",
    "dd",
    "dts_hd_ma",
    "dts_hd",
    "dts",
    "aac",
    "flac",
    "opus",
)
_AUDIO_CHANNEL_ORDER: tuple[str, ...] = ("7.1", "5.1", "2.0", "1.0")

_VIDEO_DOLBY_VISION_PATTERN = re.compile(r"(?i)\b(?:dolby[\s._-]*vision|dovi)\b")
_VIDEO_DV_TOKEN_PATTERN = re.compile(r"(?i)\bdv\b")
_VIDEO_DV_GUARD_PATTERN = re.compile(
    r"(?i)\b(?:hdr|hdr10\+?|hevc|x265|h[\s._-]*265|2160p|uhd|remux|bluray)\b"
)
_VIDEO_HDR10_PLUS_PATTERN = re.compile(
    r"(?i)\bhdr[\s._-]*10\s*(?:\+|plus)(?=\b|[^a-z0-9]|$)|\bhdr10plus\b"
)
_VIDEO_HDR10_PATTERN = re.compile(r"(?i)\bhdr[\s._-]*10\b")
_VIDEO_HDR_PATTERN = re.compile(r"(?i)\bhdr\b")
_VIDEO_HDRIP_PATTERN = re.compile(r"(?i)\bhd[\s._-]*rip\b")
_VIDEO_HLG_PATTERN = re.compile(r"(?i)\bhlg\b")
_VIDEO_SDR_PATTERN = re.compile(r"(?i)\bsdr\b")

_AUDIO_ATMOS_PATTERN = re.compile(r"(?i)\b(?:dolby[\s._-]*)?atmos\b")
_AUDIO_TRUEHD_PATTERN = re.compile(r"(?i)\btrue[\s._-]*hd\b")
_AUDIO_DDP_PATTERN = re.compile(
    r"(?i)\b(?:ddp(?:[\s._-]*\d[\s._-]*\d)?|dd\+|e[\s._-]*ac[\s._-]*3|eac3|dolby[\s._-]*digital[\s._-]*plus)\b"
)
_AUDIO_DD_PATTERN = re.compile(
    r"(?i)\b(?:dd|ac[\s._-]*3)\b|\bdolby[\s._-]*digital(?![\s._-]*plus)\b"
)
_AUDIO_DTS_HD_MA_PATTERN = re.compile(r"(?i)\b(?:dts[\s._-]*hd[\s._-]*ma|dtshdma)\b")
_AUDIO_DTS_HD_PATTERN = re.compile(r"(?i)\b(?:dts[\s._-]*hd|dtshd)\b")
_AUDIO_DTS_PATTERN = re.compile(r"(?i)\bdts\b")
_AUDIO_AAC_PATTERN = re.compile(r"(?i)\baac\b")
_AUDIO_FLAC_PATTERN = re.compile(r"(?i)\bflac\b")
_AUDIO_OPUS_PATTERN = re.compile(r"(?i)\bopus\b")

_CHANNEL_7_1_PATTERN = re.compile(r"(?i)(?<!\d)(?:7[\s._-]*1|7ch|8ch)\b")
_CHANNEL_5_1_PATTERN = re.compile(r"(?i)(?<!\d)(?:5[\s._-]*1|6ch)\b")
_CHANNEL_2_0_PATTERN = re.compile(r"(?i)(?<!\d)(?:2[\s._-]*0|2ch|stereo)\b")
_CHANNEL_1_0_PATTERN = re.compile(r"(?i)(?<!\d)(?:1[\s._-]*0|mono)\b")

_VIDEO_KEY_SYNONYMS: dict[str, str] = {
    "dolbyvision": "dolby_vision",
    "dovi": "dolby_vision",
    "dv": "dolby_vision",
    "hdr10plus": "hdr10_plus",
    "hdr10+": "hdr10_plus",
    "hdr10p": "hdr10_plus",
    "hdr10": "hdr10",
    "hdr": "hdr",
    "hlg": "hlg",
    "sdr": "sdr",
}
_AUDIO_KEY_SYNONYMS: dict[str, str] = {
    "atmos": "atmos",
    "dolbyatmos": "atmos",
    "truehd": "truehd",
    "ddp": "ddp",
    "dd+": "ddp",
    "ddplus": "ddp",
    "eac3": "ddp",
    "dolbydigitalplus": "ddp",
    "dd": "dd",
    "ac3": "dd",
    "dolbydigital": "dd",
    "dtshdma": "dts_hd_ma",
    "dtshdmasteraudio": "dts_hd_ma",
    "dtshd": "dts_hd",
    "dts": "dts",
    "aac": "aac",
    "flac": "flac",
    "opus": "opus",
}


def parse_video_formats(title: str) -> set[str]:
    """Parses a title for known video format tags using specificity precedence."""
    if not isinstance(title, str) or not title:
        return set()

    matches: set[str] = set()
    if _VIDEO_DOLBY_VISION_PATTERN.search(title) or (
        _VIDEO_DV_TOKEN_PATTERN.search(title) and _VIDEO_DV_GUARD_PATTERN.search(title)
    ):
        matches.add("dolby_vision")
    elif _VIDEO_HDR10_PLUS_PATTERN.search(title):
        matches.add("hdr10_plus")
    elif _VIDEO_HDR10_PATTERN.search(title):
        matches.add("hdr10")
    elif _VIDEO_HDR_PATTERN.search(title) and not _VIDEO_HDRIP_PATTERN.search(title):
        matches.add("hdr")

    if _VIDEO_HLG_PATTERN.search(title):
        matches.add("hlg")
    if _VIDEO_SDR_PATTERN.search(title):
        matches.add("sdr")
    return matches


def parse_audio_formats(title: str) -> set[str]:
    """Parses a title for known audio format tags and avoids family double-counting."""
    if not isinstance(title, str) or not title:
        return set()

    matches: set[str] = set()
    if _AUDIO_ATMOS_PATTERN.search(title):
        matches.add("atmos")
    if _AUDIO_TRUEHD_PATTERN.search(title):
        matches.add("truehd")

    if _AUDIO_DDP_PATTERN.search(title):
        matches.add("ddp")
    elif _AUDIO_DD_PATTERN.search(title):
        matches.add("dd")

    if _AUDIO_DTS_HD_MA_PATTERN.search(title):
        matches.add("dts_hd_ma")
    elif _AUDIO_DTS_HD_PATTERN.search(title):
        matches.add("dts_hd")
    elif _AUDIO_DTS_PATTERN.search(title):
        matches.add("dts")

    if _AUDIO_AAC_PATTERN.search(title):
        matches.add("aac")
    if _AUDIO_FLAC_PATTERN.search(title):
        matches.add("flac")
    if _AUDIO_OPUS_PATTERN.search(title):
        matches.add("opus")
    return matches


def parse_audio_channels(title: str) -> set[str]:
    """Parses a title for common channel-count tags."""
    if not isinstance(title, str) or not title:
        return set()

    matches: set[str] = set()
    if _CHANNEL_7_1_PATTERN.search(title):
        matches.add("7.1")
    if _CHANNEL_5_1_PATTERN.search(title):
        matches.add("5.1")
    if _CHANNEL_2_0_PATTERN.search(title):
        matches.add("2.0")
    if _CHANNEL_1_0_PATTERN.search(title):
        matches.add("1.0")
    return matches


def _canonicalize_key(raw_key: Any, synonym_map: dict[str, str]) -> str | None:
    if not isinstance(raw_key, str):
        return None
    lowered = raw_key.strip().lower()
    compact = re.sub(r"[\s._-]+", "", lowered)
    return synonym_map.get(lowered) or synonym_map.get(compact)


def _canonicalize_audio_channel_key(raw_key: Any) -> str | None:
    if not isinstance(raw_key, str):
        return None
    compact = re.sub(r"[^0-9]", "", raw_key.strip())
    if compact == "71":
        return "7.1"
    if compact == "51":
        return "5.1"
    if compact == "20":
        return "2.0"
    if compact == "10":
        return "1.0"
    return None


def _coerce_preference_weight(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _canonical_preference_weights(
    raw_preferences: Any,
    *,
    key_normalizer,
    order: tuple[str, ...],
) -> dict[str, int]:
    if not isinstance(raw_preferences, dict):
        return {}
    out: dict[str, int] = {}
    allowed = set(order)
    for raw_key, raw_value in raw_preferences.items():
        canonical = key_normalizer(raw_key)
        if not canonical or canonical not in allowed:
            continue
        score = _coerce_preference_weight(raw_value)
        if canonical not in out or score > out[canonical]:
            out[canonical] = score
    return out


def _ordered_matches(
    parsed_tags: set[str],
    canonical_weights: dict[str, int],
    *,
    order: tuple[str, ...],
) -> list[str]:
    return [key for key in order if key in parsed_tags and key in canonical_weights]


def _max_weight_for_matches(matches: list[str], weights: dict[str, int]) -> int:
    if not matches:
        return 0
    return max(weights.get(key, 0) for key in matches)


def _has_any(values: set[str], candidates: set[str]) -> bool:
    return bool(values.intersection(candidates))


def compute_av_match_metadata(title: str, preferences: dict[str, Any]) -> dict[str, Any]:
    """Builds AV match metadata for UI and scoring consumers."""
    parsed_video_formats = parse_video_formats(title)
    parsed_audio_formats = parse_audio_formats(title)
    parsed_audio_channels = parse_audio_channels(title)

    video_weights = _canonical_preference_weights(
        preferences.get("video_formats", {}),
        key_normalizer=lambda key: _canonicalize_key(key, _VIDEO_KEY_SYNONYMS),
        order=_VIDEO_FORMAT_ORDER,
    )
    audio_weights = _canonical_preference_weights(
        preferences.get("audio_formats", {}),
        key_normalizer=lambda key: _canonicalize_key(key, _AUDIO_KEY_SYNONYMS),
        order=_AUDIO_FORMAT_ORDER,
    )
    channel_weights = _canonical_preference_weights(
        preferences.get("audio_channels", {}),
        key_normalizer=_canonicalize_audio_channel_key,
        order=_AUDIO_CHANNEL_ORDER,
    )

    matched_video_formats = _ordered_matches(
        parsed_video_formats, video_weights, order=_VIDEO_FORMAT_ORDER
    )
    matched_audio_formats = _ordered_matches(
        parsed_audio_formats, audio_weights, order=_AUDIO_FORMAT_ORDER
    )
    matched_audio_channels = _ordered_matches(
        parsed_audio_channels, channel_weights, order=_AUDIO_CHANNEL_ORDER
    )

    matched_video_set = set(matched_video_formats)
    matched_audio_set = set(matched_audio_formats)

    has_video_match = bool(matched_video_set)
    has_audio_match = bool(matched_audio_set)
    is_gold_av = "dolby_vision" in matched_video_set and "atmos" in matched_audio_set
    silver_video_tier = {"hdr10_plus", "hdr10"}
    silver_audio_tier = {"atmos", "truehd", "dts_hd_ma"}
    is_silver_av = (
        not is_gold_av
        and _has_any(matched_video_set, silver_video_tier)
        and _has_any(matched_audio_set, silver_audio_tier)
    )
    bronze_video_tier = {"dolby_vision", "hdr10_plus", "hdr10", "hdr", "hlg"}
    bronze_audio_tier = {
        "atmos",
        "truehd",
        "dts_hd_ma",
        "ddp",
        "dts_hd",
        "dts",
        "dd",
        "aac",
        "flac",
        "opus",
    }
    is_bronze_av = (
        not is_gold_av
        and not is_silver_av
        and _has_any(matched_video_set, bronze_video_tier)
        and _has_any(matched_audio_set, bronze_audio_tier)
    )

    return {
        "matched_video_formats": matched_video_formats,
        "matched_audio_formats": matched_audio_formats,
        "matched_audio_channels": matched_audio_channels,
        "has_video_match": has_video_match,
        "has_audio_match": has_audio_match,
        "is_gold_av": is_gold_av,
        "is_silver_av": is_silver_av,
        "is_bronze_av": is_bronze_av,
        # Format categories use top-match scoring to avoid double counting.
        "video_format_score": _max_weight_for_matches(matched_video_formats, video_weights),
        "audio_format_score": _max_weight_for_matches(matched_audio_formats, audio_weights),
        "audio_channel_score": sum(channel_weights[key] for key in matched_audio_channels),
    }


def calculate_torrent_health(seeders: int, leechers: int) -> float:
    """
    Calculates a health score (0-10) based on availability and contention.

    Formula:
    1. Availability: 10 * (1 - exp(-seeders / 25))
       - 1 seed: ~0.4
       - 10 seeds: ~3.3
       - 25 seeds: ~6.3
       - 50 seeds: ~8.6
    2. Contention Penalty: If Leechers > Seeders, multiply by (Seeders/Leechers).
    """
    if seeders <= 0:
        return 0.0

    # Availability Score (0-10)
    availability = 10 * (1 - math.exp(-seeders / 25))

    # Contention Penalty
    penalty = 1.0
    if leechers > seeders and leechers > 0:
        penalty = seeders / leechers

    return availability * penalty


def score_torrent_result(
    title: str,
    uploader: str,
    preferences: dict[str, Any],
    seeders: int = 0,
    leechers: int = 0,
) -> int:
    """
    Scores a torrent result based on user preferences and swarm health.
    """
    score = 0
    title_lower = title.lower()

    # Score based on codecs (e.g., "x265": 2)
    for codec, value in preferences.get("codecs", {}).items():
        if codec.lower() in title_lower:
            score += value

    # Score based on resolutions/quality (e.g., "1080p": 5)
    for quality, value in preferences.get("resolutions", {}).items():
        if quality.lower() in title_lower:
            score += value

    # Score based on trusted uploaders (e.g., "MeGusta": 5)
    for trusted_uploader, value in preferences.get("uploaders", {}).items():
        if trusted_uploader.lower() == uploader.lower():
            score += value

    av_metadata = compute_av_match_metadata(title, preferences)
    score += av_metadata["video_format_score"]
    score += av_metadata["audio_format_score"]
    score += av_metadata["audio_channel_score"]

    # Add health score (max 10 points)
    health = calculate_torrent_health(seeders, leechers)
    score += int(round(health))

    return score


_COLLECTION_SUFFIX_KEYWORDS = (
    "franchise",
    "film series",
    "films",
    "collection",
    "cinematic universe",
    "universe",
)


def sanitize_collection_name(value: str | None) -> str:
    """
    Sanitizes a collection name by removing invalid characters and common
    franchise suffixes (e.g., " (Franchise)", " Collection").
    """
    invalid_chars = '<>:"/\\|?*'
    safe_value = "".join(c for c in (value or "") if c not in invalid_chars).strip()
    if not safe_value:
        return "Collection"

    # Remove parenthetical suffixes like "(Franchise)"
    suffix_pattern = re.compile(
        r"\((?:[^)]*\b(?:franchise|collection|film series|films|cinematic universe|universe)\b[^)]*)\)",
        re.IGNORECASE,
    )
    safe_value = suffix_pattern.sub("", safe_value).strip()

    # Remove trailing keywords
    for keyword in _COLLECTION_SUFFIX_KEYWORDS:
        keyword_pattern = re.compile(rf"\b{keyword}\b\.?$", re.IGNORECASE)
        new_value = keyword_pattern.sub("", safe_value).strip()
        safe_value = new_value or safe_value

    safe_value = safe_value.strip("-_ ,")
    return safe_value or "Collection"
