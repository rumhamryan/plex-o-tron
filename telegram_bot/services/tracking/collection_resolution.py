from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, TypedDict

from telegram_bot.config import logger
from telegram_bot.services import scraping_service
from telegram_bot.services.tracking import tmdb_release_service
from telegram_bot.utils import sanitize_collection_name
from telegram_bot.workflows.search_workflow.helpers import _normalize_collection_movie_title


class CollectionTrackingCandidate(TypedDict):
    title: str
    canonical_title: str
    year: int | None
    availability_date: date | None
    availability_source: Literal["streaming"] | None


class CollectionTrackingMovie(TypedDict):
    title: str
    year: int | None


class CollectionTrackingResolution(TypedDict):
    collection_name: str
    candidates: list[CollectionTrackingCandidate]
    library_movies: list[CollectionTrackingMovie]
    total_titles: int
    skipped_released_streaming: int
    skipped_past_year_unknown_streaming: int


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_release_iso(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _resolve_candidate_year(raw_movie: dict[str, Any]) -> int | None:
    year = _coerce_int(raw_movie.get("year"))
    if year is not None:
        return year
    parsed_release = _parse_release_iso(raw_movie.get("release_date"))
    if parsed_release is not None:
        return parsed_release.year
    return None


def _build_collection_movie_entry(
    normalized_title: str,
    year: int | None,
) -> CollectionTrackingMovie:
    entry: CollectionTrackingMovie = {"title": normalized_title, "year": None}
    if isinstance(year, int):
        entry["year"] = year
    return entry


async def resolve_collection_tracking_candidates(
    collection_query: str,
    *,
    today: date | None = None,
) -> CollectionTrackingResolution | None:
    """Resolve collection entries and keep only titles not yet streaming-released."""
    search_query = str(collection_query or "").strip()
    if not search_query:
        return None

    try:
        resolved = await scraping_service.fetch_movie_franchise_details(search_query)
    except Exception as exc:  # noqa: BLE001
        logger.info("[TRACKING] Collection lookup failed for '%s': %s", search_query, exc)
        return None

    if not resolved or not isinstance(resolved, tuple) or len(resolved) != 2:
        logger.info("[TRACKING] No collection match found for '%s'.", search_query)
        return None

    raw_name, raw_movies = resolved
    collection_name = sanitize_collection_name(str(raw_name or search_query))
    movie_entries = raw_movies if isinstance(raw_movies, list) else []

    normalized_movies: list[tuple[str, int | None]] = []
    seen_movie_keys: set[tuple[str, int | None]] = set()
    for raw_movie in movie_entries:
        if not isinstance(raw_movie, dict):
            continue
        raw_title = raw_movie.get("title") or raw_movie.get("name")
        if raw_title is None:
            continue
        year = _resolve_candidate_year(raw_movie)
        normalized_title = _normalize_collection_movie_title(
            raw_title,
            year,
            raw_movie.get("release_date"),
        )
        if not normalized_title:
            continue
        dedupe_key = (normalized_title.casefold(), year)
        if dedupe_key in seen_movie_keys:
            continue
        seen_movie_keys.add(dedupe_key)
        normalized_movies.append((normalized_title, year))

    reference_day = today or date.today()
    candidates: list[CollectionTrackingCandidate] = []
    library_movies: list[CollectionTrackingMovie] = []
    skipped_released_streaming = 0
    skipped_past_year_unknown_streaming = 0
    for normalized_title, year in normalized_movies:
        streaming_date = await tmdb_release_service.resolve_tmdb_streaming_release_date(
            normalized_title,
            year=year,
        )
        if streaming_date is not None and streaming_date <= reference_day:
            skipped_released_streaming += 1
            library_movies.append(_build_collection_movie_entry(normalized_title, year))
            continue
        if streaming_date is None and isinstance(year, int) and year < reference_day.year:
            skipped_past_year_unknown_streaming += 1
            library_movies.append(_build_collection_movie_entry(normalized_title, year))
            continue

        candidates.append(
            {
                "title": normalized_title,
                "canonical_title": normalized_title,
                "year": year,
                "availability_date": streaming_date,
                "availability_source": "streaming" if streaming_date is not None else None,
            }
        )

    candidates.sort(
        key=lambda entry: (
            entry.get("year") is None,
            int(entry.get("year") or 0),
            str(entry.get("canonical_title") or "").casefold(),
        )
    )
    library_movies.sort(
        key=lambda entry: (
            entry.get("year") is None,
            int(entry.get("year") or 0),
            str(entry.get("title") or "").casefold(),
        )
    )

    logger.info(
        "[TRACKING] Collection '%s' resolved from '%s': total=%d, schedulable=%d, "
        "skipped_streaming=%d, skipped_past_year_unknown=%d",
        collection_name,
        search_query,
        len(normalized_movies),
        len(candidates),
        skipped_released_streaming,
        skipped_past_year_unknown_streaming,
    )
    return {
        "collection_name": collection_name,
        "candidates": candidates,
        "library_movies": library_movies,
        "total_titles": len(normalized_movies),
        "skipped_released_streaming": skipped_released_streaming,
        "skipped_past_year_unknown_streaming": skipped_past_year_unknown_streaming,
    }
