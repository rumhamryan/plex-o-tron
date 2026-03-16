# telegram_bot/workflows/search_workflow/helpers.py

import re
from datetime import date, datetime
from typing import Any

_TRAILING_COLLECTION_YEAR_PATTERN = re.compile(
    r"\s*\((?P<year>(?:18|19|20|21)\d{2})(?:\s+film)?\)\s*$",
    re.IGNORECASE,
)


def _normalize_label(value: Any) -> str:
    text = ""
    if isinstance(value, str):
        text = value
    elif value is not None:
        text = str(value)
    return re.sub(r"[\W_]+", "", text.strip()).casefold()


def _normalize_collection_movie_title(
    title: Any,
    year: int | None = None,
    release_date: Any = None,
) -> str:
    normalized_title = str(title or "Untitled").strip() or "Untitled"
    effective_year = year
    if effective_year is None:
        parsed_release_date = _parse_release_iso(release_date)
        if parsed_release_date is not None:
            effective_year = parsed_release_date.year

    match = _TRAILING_COLLECTION_YEAR_PATTERN.search(normalized_title)
    if match is None or effective_year is None:
        return normalized_title

    if int(match.group("year")) != effective_year:
        return normalized_title

    stripped_title = normalized_title[: match.start()].rstrip()
    return stripped_title or normalized_title


def _format_collection_movie_label(movie: dict[str, Any]) -> str:
    year = _coerce_int(movie.get("year"))
    title = _normalize_collection_movie_title(movie.get("title"), year, movie.get("release_date"))
    if isinstance(year, int):
        return f"{title} ({year})"
    return title


def _ensure_identifier(movie: dict[str, Any], index: int) -> str:
    """Returns a short, session-stable identifier safe for Telegram callback data."""
    return f"movie-{index}"


def _parse_release_iso(value: Any) -> date | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None


def _normalize_release_field(value: Any, default: str) -> str:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or default
    return default


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _infer_resolution_from_title(title: Any) -> str | None:
    if not isinstance(title, str):
        return None
    lowered = title.lower()
    if "2160p" in lowered or "4k" in lowered or "uhd" in lowered:
        return "2160p"
    if "1080p" in lowered:
        return "1080p"
    if "720p" in lowered:
        return "720p"
    return None
