# telegram_bot/services/scoring.py

import re
from typing import Any

def score_torrent_result(
    title: str, uploader: str, preferences: dict[str, Any], seeders: int = 0
) -> int:
    """
    Scores a torrent result based on user preferences (codecs, uploaders, etc.).
    This version correctly handles a dictionary of preferences with weighted scores.
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

    # Add the raw seeder count directly to the score
    score += seeders

    return score


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
