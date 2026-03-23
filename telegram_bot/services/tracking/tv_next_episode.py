from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import Any, Awaitable, Callable, Literal, TypedDict

import httpx

from telegram_bot.config import logger

TMDB_API_BASE_URL = "https://api.themoviedb.org/3"
TMDB_REQUEST_TIMEOUT_SECONDS = 8.0


class TvTrackingCandidate(TypedDict):
    target_kind: Literal["tv"]
    schedule_mode: Literal["ongoing_next_episode"]
    title: str
    canonical_title: str
    tmdb_series_id: int
    first_air_date: date | None
    next_air_date: date | None


class TvEpisodeRecord(TypedDict):
    season: int
    episode: int
    title: str | None
    air_date: date | None


class TvNextEpisodeResolution(TypedDict):
    canonical_title: str
    tmdb_series_id: int
    state: Literal["search_now", "await_window", "awaiting_metadata"]
    next_episode: TvEpisodeRecord | None
    next_air_date: date | None


def _parse_tmdb_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _coerce_int(value: Any, *, minimum: int | None = None) -> int | None:
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip().isdigit():
        result = int(value.strip())
    else:
        return None
    if minimum is not None and result < minimum:
        return None
    return result


def _normalize_text_for_match(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def _extract_tmdb_year(value: Any) -> int | None:
    if not isinstance(value, str) or len(value) < 4:
        return None
    prefix = value[:4]
    if not prefix.isdigit():
        return None
    return int(prefix)


def _get_tmdb_auth() -> tuple[dict[str, str], dict[str, str]] | None:
    access_token = (os.getenv("TMDB_ACCESS_TOKEN") or os.getenv("TMDB_BEARER_TOKEN") or "").strip()
    api_key = (os.getenv("TMDB_API_KEY") or "").strip()

    if access_token:
        return {"Authorization": f"Bearer {access_token}"}, {}
    if api_key:
        return {}, {"api_key": api_key}
    return None


def _score_tv_search_result(query_title: str, year: int | None, result: dict[str, Any]) -> int:
    score = 0
    query_normalized = _normalize_text_for_match(query_title)
    candidate_title = str(result.get("name") or result.get("original_name") or "")
    candidate_normalized = _normalize_text_for_match(candidate_title)
    if query_normalized and candidate_normalized == query_normalized:
        score += 120
    elif query_normalized and query_normalized in candidate_normalized:
        score += 40

    if isinstance(year, int):
        candidate_year = _extract_tmdb_year(result.get("first_air_date"))
        if candidate_year == year:
            score += 80
        elif isinstance(candidate_year, int) and abs(candidate_year - year) == 1:
            score += 20

    popularity = result.get("popularity")
    if isinstance(popularity, (int, float)) and popularity > 0:
        score += min(int(popularity // 10), 10)
    return score


def _choose_tv_search_results(
    query_title: str,
    *,
    year: int | None,
    raw_results: Any,
    limit: int,
) -> list[dict[str, Any]]:
    if not isinstance(raw_results, list):
        return []

    scored: list[tuple[int, dict[str, Any]]] = []
    for result in raw_results:
        if not isinstance(result, dict):
            continue
        tv_id = _coerce_int(result.get("id"), minimum=1)
        title = str(result.get("name") or result.get("original_name") or "").strip()
        if tv_id is None or not title:
            continue
        scored.append((_score_tv_search_result(query_title, year, result), result))

    scored.sort(
        key=lambda entry: (
            entry[0],
            float(entry[1].get("popularity") or 0),
        ),
        reverse=True,
    )

    selected: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for _, result in scored:
        tv_id = _coerce_int(result.get("id"), minimum=1)
        if tv_id is None or tv_id in seen_ids:
            continue
        seen_ids.add(tv_id)
        selected.append(result)
        if len(selected) >= max(limit, 1):
            break
    return selected


def _extract_next_air_date_from_details(details_payload: Any) -> date | None:
    if not isinstance(details_payload, dict):
        return None

    direct_next_air = _parse_tmdb_date(details_payload.get("next_air_date"))
    if direct_next_air is not None:
        return direct_next_air

    next_episode_to_air = details_payload.get("next_episode_to_air")
    if not isinstance(next_episode_to_air, dict):
        return None
    return _parse_tmdb_date(next_episode_to_air.get("air_date"))


async def _fetch_tv_details_payload(
    *,
    client: httpx.AsyncClient,
    tmdb_series_id: int,
    headers: dict[str, str],
    auth_params: dict[str, str],
) -> dict[str, Any] | None:
    try:
        response = await client.get(
            f"{TMDB_API_BASE_URL}/tv/{int(tmdb_series_id)}",
            params=auth_params,
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "[TRACKING] TV details lookup failed for series_id=%s: %s",
            tmdb_series_id,
            exc,
        )
        return None

    if not isinstance(payload, dict):
        return None
    return payload


async def find_tv_tracking_candidates(
    show_title: str,
    *,
    year: int | None = None,
    limit: int = 8,
) -> list[TvTrackingCandidate]:
    normalized_title = (show_title or "").strip()
    if not normalized_title:
        return []

    auth = _get_tmdb_auth()
    if auth is None:
        logger.info(
            "[TRACKING] TV candidate lookup skipped for '%s': missing TMDB credentials.", show_title
        )
        return []

    headers, auth_params = auth
    params: dict[str, str] = {
        **auth_params,
        "query": normalized_title,
        "include_adult": "false",
    }
    if isinstance(year, int):
        params["first_air_date_year"] = str(year)

    candidates: list[TvTrackingCandidate] = []
    try:
        async with httpx.AsyncClient(timeout=TMDB_REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{TMDB_API_BASE_URL}/search/tv",
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()

            raw_results = payload.get("results") if isinstance(payload, dict) else []
            selected = _choose_tv_search_results(
                normalized_title,
                year=year,
                raw_results=raw_results,
                limit=limit,
            )
            for result in selected:
                tmdb_series_id = _coerce_int(result.get("id"), minimum=1)
                canonical_title = str(
                    result.get("name") or result.get("original_name") or ""
                ).strip()
                if tmdb_series_id is None or not canonical_title:
                    continue

                first_air_date = _parse_tmdb_date(result.get("first_air_date"))
                next_air_date = _parse_tmdb_date(result.get("next_air_date"))
                if next_air_date is None:
                    details_payload = await _fetch_tv_details_payload(
                        client=client,
                        tmdb_series_id=tmdb_series_id,
                        headers=headers,
                        auth_params=auth_params,
                    )
                    if isinstance(details_payload, dict):
                        details_title = details_payload.get("name")
                        if isinstance(details_title, str) and details_title.strip():
                            canonical_title = details_title.strip()
                        if first_air_date is None:
                            first_air_date = _parse_tmdb_date(details_payload.get("first_air_date"))
                        next_air_date = _extract_next_air_date_from_details(details_payload)

                candidates.append(
                    {
                        "target_kind": "tv",
                        "schedule_mode": "ongoing_next_episode",
                        "title": canonical_title,
                        "canonical_title": canonical_title,
                        "tmdb_series_id": tmdb_series_id,
                        "first_air_date": first_air_date,
                        "next_air_date": next_air_date,
                    }
                )
    except Exception as exc:  # noqa: BLE001
        logger.info("[TRACKING] TV candidate lookup failed for '%s': %s", normalized_title, exc)
        return []

    logger.info(
        "[TRACKING] Resolved %d TV candidate(s) for '%s'.", len(candidates), normalized_title
    )
    return candidates


def _extract_episodes_from_season_payload(payload: Any) -> list[TvEpisodeRecord]:
    if not isinstance(payload, dict):
        return []
    raw_episodes = payload.get("episodes")
    if not isinstance(raw_episodes, list):
        return []

    normalized: list[TvEpisodeRecord] = []
    for episode in raw_episodes:
        if not isinstance(episode, dict):
            continue
        season_number = _coerce_int(
            episode.get("season_number", payload.get("season_number")),
            minimum=1,
        )
        episode_number = _coerce_int(episode.get("episode_number"), minimum=1)
        if season_number is None or episode_number is None:
            continue
        title_raw = episode.get("name")
        title = title_raw.strip() if isinstance(title_raw, str) and title_raw.strip() else None
        normalized.append(
            {
                "season": season_number,
                "episode": episode_number,
                "title": title,
                "air_date": _parse_tmdb_date(episode.get("air_date")),
            }
        )

    normalized.sort(key=lambda entry: (entry["season"], entry["episode"]))
    return normalized


def _is_episode_after_cursor(
    season: int,
    episode: int,
    cursor: tuple[int, int],
) -> bool:
    return (season, episode) > cursor


async def resolve_next_ongoing_episode(
    *,
    tmdb_series_id: int,
    fallback_show_title: str,
    episode_cursor: dict[str, int] | None,
    today: date,
    existing_episode_lookup: Callable[[str, int], Awaitable[set[int]]],
) -> TvNextEpisodeResolution:
    auth = _get_tmdb_auth()
    canonical_title = (fallback_show_title or "").strip() or "TV Show"
    fallback: TvNextEpisodeResolution = {
        "canonical_title": canonical_title,
        "tmdb_series_id": int(tmdb_series_id),
        "state": "awaiting_metadata",
        "next_episode": None,
        "next_air_date": None,
    }
    if auth is None:
        logger.info(
            "[TRACKING] TV metadata refresh skipped for series_id=%s: missing TMDB credentials.",
            tmdb_series_id,
        )
        return fallback

    headers, auth_params = auth
    cursor_ref = episode_cursor or {}
    cursor = (
        int(cursor_ref.get("season") or 0),
        int(cursor_ref.get("episode") or 0),
    )

    try:
        async with httpx.AsyncClient(timeout=TMDB_REQUEST_TIMEOUT_SECONDS) as client:
            details_response = await client.get(
                f"{TMDB_API_BASE_URL}/tv/{int(tmdb_series_id)}",
                params=auth_params,
                headers=headers,
            )
            details_response.raise_for_status()
            details_payload = details_response.json()

            if isinstance(details_payload, dict):
                tmdb_name = details_payload.get("name")
                if isinstance(tmdb_name, str) and tmdb_name.strip():
                    canonical_title = tmdb_name.strip()

            raw_seasons = (
                details_payload.get("seasons") if isinstance(details_payload, dict) else []
            )
            season_numbers: list[int] = []
            if isinstance(raw_seasons, list):
                for season in raw_seasons:
                    if not isinstance(season, dict):
                        continue
                    season_number = _coerce_int(season.get("season_number"), minimum=1)
                    if season_number is None:
                        continue
                    season_numbers.append(season_number)

            if not season_numbers:
                fallback["canonical_title"] = canonical_title
                return fallback

            season_numbers = sorted(set(season_numbers))
            candidates_with_dates: list[TvEpisodeRecord] = []
            unknown_date_exists = False

            for season_number in season_numbers:
                season_response = await client.get(
                    f"{TMDB_API_BASE_URL}/tv/{int(tmdb_series_id)}/season/{season_number}",
                    params=auth_params,
                    headers=headers,
                )
                season_response.raise_for_status()
                season_payload = season_response.json()
                season_episodes = _extract_episodes_from_season_payload(season_payload)
                if not season_episodes:
                    continue

                existing_episodes = await existing_episode_lookup(canonical_title, season_number)
                for episode in season_episodes:
                    season = int(episode["season"])
                    episode_number = int(episode["episode"])
                    if not _is_episode_after_cursor(season, episode_number, cursor):
                        continue
                    if episode_number in existing_episodes:
                        continue

                    if episode["air_date"] is None:
                        unknown_date_exists = True
                        continue
                    candidates_with_dates.append(episode)

            candidates_with_dates.sort(key=lambda item: (item["season"], item["episode"]))

            released_episode = next(
                (
                    item
                    for item in candidates_with_dates
                    if item["air_date"] and item["air_date"] <= today
                ),
                None,
            )
            if released_episode is not None:
                return {
                    "canonical_title": canonical_title,
                    "tmdb_series_id": int(tmdb_series_id),
                    "state": "search_now",
                    "next_episode": released_episode,
                    "next_air_date": released_episode["air_date"],
                }

            future_episode = next(
                (
                    item
                    for item in candidates_with_dates
                    if item["air_date"] and item["air_date"] > today
                ),
                None,
            )
            if future_episode is not None:
                return {
                    "canonical_title": canonical_title,
                    "tmdb_series_id": int(tmdb_series_id),
                    "state": "await_window",
                    "next_episode": future_episode,
                    "next_air_date": future_episode["air_date"],
                }

            return {
                "canonical_title": canonical_title,
                "tmdb_series_id": int(tmdb_series_id),
                "state": "awaiting_metadata" if unknown_date_exists else "awaiting_metadata",
                "next_episode": None,
                "next_air_date": None,
            }
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "[TRACKING] TV metadata refresh failed for '%s' (series_id=%s): %s",
            canonical_title,
            tmdb_series_id,
            exc,
        )
        fallback["canonical_title"] = canonical_title
        return fallback
