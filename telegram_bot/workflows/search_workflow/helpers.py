# telegram_bot/workflows/search_workflow/helpers.py

import re
from datetime import date, datetime
from typing import Any


def _normalize_label(value: Any) -> str:
    text = ""
    if isinstance(value, str):
        text = value
    elif value is not None:
        text = str(value)
    return re.sub(r"[\W_]+", "", text.strip()).casefold()


def _format_collection_movie_label(movie: dict[str, Any]) -> str:
    title = str(movie.get("title") or "Untitled").strip() or "Untitled"
    year = movie.get("year")
    if isinstance(year, int):
        return f"{title} ({year})"
    return title


def _ensure_identifier(movie: dict[str, Any], index: int) -> str:
    title = str(movie.get("title") or "")
    year = movie.get("year")
    base = _normalize_label(title) or f"movie{index}"
    if isinstance(year, int):
        base = f"{base}{year}"
    return f"{base}-{index}"


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
