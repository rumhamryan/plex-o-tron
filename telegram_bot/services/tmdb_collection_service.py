from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import Any, Literal, TypedDict

import httpx

from telegram_bot.config import logger

TMDB_API_BASE_URL = "https://api.themoviedb.org/3"
_TMDB_LOOKUP_TIMEOUT_SECONDS = 8.0

TmdbCollectionLookupStatus = Literal[
    "ok",
    "credentials_missing",
    "movie_not_found",
    "movie_without_collection",
    "collection_not_found",
    "error",
]
TmdbCollectionReleaseStatus = Literal["released", "upcoming", "unknown"]


class TmdbMovieSummary(TypedDict):
    id: int
    title: str
    original_title: str | None
    release_date: str | None
    release_year: int | None


class TmdbCollectionMovieSummary(TmdbMovieSummary):
    release_status: TmdbCollectionReleaseStatus


class TmdbCollectionSummary(TypedDict):
    id: int
    name: str
    overview: str | None
    movie_count: int
    released_count: int
    upcoming_count: int
    unknown_count: int
    movies: list[TmdbCollectionMovieSummary]


class TmdbCollectionLookup(TypedDict):
    status: TmdbCollectionLookupStatus
    reason: str | None
    region: str | None
    matched_movie: TmdbMovieSummary | None
    collection: TmdbCollectionSummary | None


def _normalize_text_for_match(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def _normalize_title(value: str, year: int | None = None) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return "Unknown"
    if year is None:
        return cleaned
    pattern = re.compile(rf"\s*\({int(year)}(?:\s+film)?\)\s*$", re.IGNORECASE)
    normalized = pattern.sub("", cleaned).strip()
    return normalized or cleaned


def _extract_tmdb_result_year(value: Any) -> int | None:
    if not isinstance(value, str) or len(value) < 4:
        return None
    year_text = value[:4]
    if not year_text.isdigit():
        return None
    year_value = int(year_text)
    return year_value if year_value > 0 else None


def _parse_tmdb_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _coerce_tmdb_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _get_tmdb_auth() -> tuple[dict[str, str], dict[str, str], str] | None:
    access_token = (os.getenv("TMDB_ACCESS_TOKEN") or os.getenv("TMDB_BEARER_TOKEN") or "").strip()
    api_key = (os.getenv("TMDB_API_KEY") or "").strip()
    region = (os.getenv("TMDB_REGION") or "US").strip().upper() or "US"

    if access_token:
        return {"Authorization": f"Bearer {access_token}"}, {}, region
    if api_key:
        return {}, {"api_key": api_key}, region
    return None


def _choose_tmdb_search_result(
    query_title: str,
    *,
    year: int | None,
    raw_results: Any,
) -> dict[str, Any] | None:
    if not isinstance(raw_results, list):
        return None

    normalized_query = _normalize_text_for_match(_normalize_title(query_title, year))
    best_score = -1
    best: dict[str, Any] | None = None
    for result in raw_results:
        if not isinstance(result, dict):
            continue

        score = 0
        candidate_title = str(result.get("title") or result.get("original_title") or "")
        normalized_candidate = _normalize_text_for_match(candidate_title)
        if normalized_candidate and normalized_candidate == normalized_query:
            score += 120
        elif normalized_query and normalized_query in normalized_candidate:
            score += 40

        if isinstance(year, int):
            candidate_year = _extract_tmdb_result_year(result.get("release_date"))
            if candidate_year == year:
                score += 80
            elif isinstance(candidate_year, int) and abs(candidate_year - year) == 1:
                score += 15

        if isinstance(result.get("belongs_to_collection"), dict):
            score += 15

        popularity = result.get("popularity")
        if isinstance(popularity, (int, float)) and popularity > 0:
            score += min(int(popularity // 10), 10)

        if score > best_score:
            best_score = score
            best = result

    return best


def _build_movie_summary(payload: Any) -> TmdbMovieSummary | None:
    if not isinstance(payload, dict):
        return None
    movie_id = _coerce_tmdb_int(payload.get("id"))
    if movie_id is None:
        return None

    title = str(payload.get("title") or payload.get("name") or payload.get("original_title") or "")
    normalized_title = title.strip() or "Unknown"
    original_title_raw = payload.get("original_title")
    original_title = (
        str(original_title_raw).strip() if isinstance(original_title_raw, str) else None
    ) or None
    release_date_raw = payload.get("release_date")
    release_date = str(release_date_raw).strip() if isinstance(release_date_raw, str) else None
    normalized_release_date = release_date or None

    return {
        "id": movie_id,
        "title": normalized_title,
        "original_title": original_title,
        "release_date": normalized_release_date,
        "release_year": _extract_tmdb_result_year(normalized_release_date),
    }


def _classify_release_status(
    *,
    release_date: str | None,
    release_year: int | None,
    reference_day: date,
) -> TmdbCollectionReleaseStatus:
    parsed_release_date = _parse_tmdb_date(release_date)
    if parsed_release_date is not None:
        return "released" if parsed_release_date <= reference_day else "upcoming"

    if isinstance(release_year, int):
        if release_year < reference_day.year:
            return "released"
        if release_year > reference_day.year:
            return "upcoming"
    return "unknown"


def _collection_movie_sort_key(
    movie: TmdbCollectionMovieSummary,
) -> tuple[int, int, str]:
    parsed_release_date = _parse_tmdb_date(movie.get("release_date"))
    if parsed_release_date is not None:
        return (0, parsed_release_date.toordinal(), movie.get("title", "").casefold())

    release_year = movie.get("release_year")
    if isinstance(release_year, int):
        return (1, release_year, movie.get("title", "").casefold())

    # Keep entries with no release metadata at the end to reduce false positives in queues.
    return (2, 9999, movie.get("title", "").casefold())


def _build_collection_summary(
    payload: Any,
    *,
    reference_day: date,
) -> TmdbCollectionSummary | None:
    if not isinstance(payload, dict):
        return None

    collection_id = _coerce_tmdb_int(payload.get("id"))
    if collection_id is None:
        return None

    raw_name = payload.get("name")
    collection_name = str(raw_name).strip() if isinstance(raw_name, str) else ""
    if not collection_name:
        collection_name = "Collection"

    raw_parts = payload.get("parts")
    if not isinstance(raw_parts, list):
        raw_parts = []

    movies: list[TmdbCollectionMovieSummary] = []
    for part in raw_parts:
        movie_summary = _build_movie_summary(part)
        if movie_summary is None:
            continue
        movie_with_state: TmdbCollectionMovieSummary = {
            **movie_summary,
            "release_status": _classify_release_status(
                release_date=movie_summary.get("release_date"),
                release_year=movie_summary.get("release_year"),
                reference_day=reference_day,
            ),
        }
        movies.append(movie_with_state)

    movies.sort(key=_collection_movie_sort_key)

    released_count = sum(1 for movie in movies if movie["release_status"] == "released")
    upcoming_count = sum(1 for movie in movies if movie["release_status"] == "upcoming")
    unknown_count = len(movies) - released_count - upcoming_count

    overview_raw = payload.get("overview")
    overview = str(overview_raw).strip() if isinstance(overview_raw, str) else None
    return {
        "id": collection_id,
        "name": collection_name,
        "overview": overview or None,
        "movie_count": len(movies),
        "released_count": released_count,
        "upcoming_count": upcoming_count,
        "unknown_count": unknown_count,
        "movies": movies,
    }


async def resolve_tmdb_collection_from_movie(
    title: str,
    *,
    year: int | None = None,
    reference_day: date | None = None,
) -> TmdbCollectionLookup:
    lookup_title = (title or "").strip()
    if not lookup_title:
        return {
            "status": "movie_not_found",
            "reason": "Title was empty.",
            "region": None,
            "matched_movie": None,
            "collection": None,
        }

    auth = _get_tmdb_auth()
    if auth is None:
        logger.info(
            "[TMDB_COLLECTION] Lookup skipped for '%s' (year=%s): missing credentials.",
            lookup_title,
            year,
        )
        return {
            "status": "credentials_missing",
            "reason": "TMDB credentials are not configured.",
            "region": None,
            "matched_movie": None,
            "collection": None,
        }

    headers, auth_params, region = auth
    reference_day = reference_day or date.today()
    logger.info(
        "[TMDB_COLLECTION] Lookup started for '%s' (year=%s, region=%s).",
        lookup_title,
        year,
        region,
    )

    search_params: dict[str, str] = {
        **auth_params,
        "query": lookup_title,
        "include_adult": "false",
    }
    if isinstance(year, int):
        search_params["year"] = str(year)
        search_params["primary_release_year"] = str(year)

    try:
        async with httpx.AsyncClient(timeout=_TMDB_LOOKUP_TIMEOUT_SECONDS) as client:
            search_response = await client.get(
                f"{TMDB_API_BASE_URL}/search/movie",
                params=search_params,
                headers=headers,
            )
            search_response.raise_for_status()
            search_payload = search_response.json()
            search_results = (
                search_payload.get("results") if isinstance(search_payload, dict) else []
            )
            selected = _choose_tmdb_search_result(
                lookup_title, year=year, raw_results=search_results
            )
            if not isinstance(selected, dict):
                logger.info(
                    "[TMDB_COLLECTION] No movie match found for '%s' (year=%s).",
                    lookup_title,
                    year,
                )
                return {
                    "status": "movie_not_found",
                    "reason": "TMDB search returned no confident movie match.",
                    "region": region,
                    "matched_movie": None,
                    "collection": None,
                }

            matched_movie = _build_movie_summary(selected)
            if matched_movie is None:
                return {
                    "status": "movie_not_found",
                    "reason": "TMDB search result did not include a valid movie id.",
                    "region": region,
                    "matched_movie": None,
                    "collection": None,
                }

            movie_response = await client.get(
                f"{TMDB_API_BASE_URL}/movie/{matched_movie['id']}",
                params=auth_params,
                headers=headers,
            )
            if movie_response.status_code == 404:
                return {
                    "status": "movie_not_found",
                    "reason": f"TMDB movie id {matched_movie['id']} was not found.",
                    "region": region,
                    "matched_movie": matched_movie,
                    "collection": None,
                }
            movie_response.raise_for_status()
            movie_payload = movie_response.json()
            if isinstance(movie_payload, dict):
                detailed_movie = _build_movie_summary(movie_payload)
                if detailed_movie is not None:
                    matched_movie = detailed_movie
                collection_hint = movie_payload.get("belongs_to_collection")
            else:
                collection_hint = selected.get("belongs_to_collection")

            if not isinstance(collection_hint, dict):
                return {
                    "status": "movie_without_collection",
                    "reason": "Matched movie does not belong to a TMDB collection.",
                    "region": region,
                    "matched_movie": matched_movie,
                    "collection": None,
                }

            collection_id = _coerce_tmdb_int(collection_hint.get("id"))
            if collection_id is None:
                return {
                    "status": "movie_without_collection",
                    "reason": "Matched movie includes an invalid TMDB collection id.",
                    "region": region,
                    "matched_movie": matched_movie,
                    "collection": None,
                }

            collection_response = await client.get(
                f"{TMDB_API_BASE_URL}/collection/{collection_id}",
                params=auth_params,
                headers=headers,
            )
            if collection_response.status_code == 404:
                return {
                    "status": "collection_not_found",
                    "reason": f"TMDB collection id {collection_id} was not found.",
                    "region": region,
                    "matched_movie": matched_movie,
                    "collection": None,
                }
            collection_response.raise_for_status()
            collection_payload = collection_response.json()
    except Exception as exc:  # noqa: BLE001
        logger.info("[TMDB_COLLECTION] Lookup failed for '%s': %s", lookup_title, exc)
        return {
            "status": "error",
            "reason": str(exc),
            "region": region,
            "matched_movie": None,
            "collection": None,
        }

    collection_summary = _build_collection_summary(collection_payload, reference_day=reference_day)
    if collection_summary is None:
        return {
            "status": "collection_not_found",
            "reason": "TMDB returned an invalid collection payload.",
            "region": region,
            "matched_movie": matched_movie,
            "collection": None,
        }

    logger.info(
        "[TMDB_COLLECTION] Resolved '%s' -> collection '%s' (%d parts).",
        lookup_title,
        collection_summary["name"],
        collection_summary["movie_count"],
    )
    return {
        "status": "ok",
        "reason": None,
        "region": region,
        "matched_movie": matched_movie,
        "collection": collection_summary,
    }


__all__ = [
    "TmdbCollectionLookup",
    "TmdbCollectionLookupStatus",
    "TmdbCollectionMovieSummary",
    "TmdbCollectionReleaseStatus",
    "TmdbCollectionSummary",
    "TmdbMovieSummary",
    "resolve_tmdb_collection_from_movie",
]
