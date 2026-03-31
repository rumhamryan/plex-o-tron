from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import Any, Literal

import httpx
from bs4 import BeautifulSoup, Tag

from telegram_bot.config import logger

TMDB_DIGITAL_RELEASE_TYPE = 4
TMDB_PHYSICAL_RELEASE_TYPE = 5
TMDB_API_BASE_URL = "https://api.themoviedb.org/3"
TMDB_WEB_BASE_URL = "https://www.themoviedb.org"


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


def _parse_tmdb_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _extract_tmdb_result_year(value: Any) -> int | None:
    if not isinstance(value, str) or len(value) < 4:
        return None
    year_text = value[:4]
    if not year_text.isdigit():
        return None
    return int(year_text)


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
            score += 100
        elif normalized_query and normalized_query in normalized_candidate:
            score += 40

        if isinstance(year, int):
            candidate_year = _extract_tmdb_result_year(result.get("release_date"))
            if candidate_year == year:
                score += 80
            elif isinstance(candidate_year, int) and abs(candidate_year - year) == 1:
                score += 15

        popularity = result.get("popularity")
        if isinstance(popularity, (int, float)) and popularity > 0:
            score += min(int(popularity // 10), 10)

        if score > best_score:
            best_score = score
            best = result

    return best


def _extract_tmdb_earliest_availability(
    payload: Any,
    *,
    region: str,
) -> tuple[date | None, Literal["streaming", "physical"] | None]:
    if not isinstance(payload, dict):
        return None, None
    raw_regions = payload.get("results")
    if not isinstance(raw_regions, list):
        return None, None

    def collect_candidates(
        blocks: list[dict[str, Any]],
    ) -> list[tuple[date, Literal["streaming", "physical"]]]:
        candidates: list[tuple[date, Literal["streaming", "physical"]]] = []
        for block in blocks:
            release_dates = block.get("release_dates")
            if not isinstance(release_dates, list):
                continue
            for entry in release_dates:
                if not isinstance(entry, dict):
                    continue
                release_type = entry.get("type")
                if release_type == TMDB_DIGITAL_RELEASE_TYPE:
                    source: Literal["streaming", "physical"] = "streaming"
                elif release_type == TMDB_PHYSICAL_RELEASE_TYPE:
                    source = "physical"
                else:
                    continue

                parsed = _parse_tmdb_date(entry.get("release_date"))
                if parsed is None:
                    continue
                candidates.append((parsed, source))
        return candidates

    region_upper = region.upper()
    preferred_blocks = [
        block
        for block in raw_regions
        if isinstance(block, dict) and str(block.get("iso_3166_1") or "").upper() == region_upper
    ]
    candidates = collect_candidates(preferred_blocks)
    if not candidates:
        all_blocks = [block for block in raw_regions if isinstance(block, dict)]
        candidates = collect_candidates(all_blocks)
    if not candidates:
        return None, None

    candidates.sort(key=lambda item: (item[0], 0 if item[1] == "streaming" else 1))
    return candidates[0]


def _extract_tmdb_earliest_streaming_date(payload: Any, *, region: str) -> date | None:
    if not isinstance(payload, dict):
        return None
    raw_regions = payload.get("results")
    if not isinstance(raw_regions, list):
        return None

    def collect_dates(blocks: list[dict[str, Any]]) -> list[date]:
        dates: list[date] = []
        for block in blocks:
            release_dates = block.get("release_dates")
            if not isinstance(release_dates, list):
                continue
            for entry in release_dates:
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") != TMDB_DIGITAL_RELEASE_TYPE:
                    continue
                parsed = _parse_tmdb_date(entry.get("release_date"))
                if parsed is not None:
                    dates.append(parsed)
        return dates

    region_upper = region.upper()
    preferred_blocks = [
        block
        for block in raw_regions
        if isinstance(block, dict) and str(block.get("iso_3166_1") or "").upper() == region_upper
    ]
    candidates = collect_dates(preferred_blocks)
    if not candidates:
        all_blocks = [block for block in raw_regions if isinstance(block, dict)]
        candidates = collect_dates(all_blocks)
    if not candidates:
        return None
    return min(candidates)


def _parse_tmdb_web_release_date(value: str) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _extract_tmdb_web_earliest_streaming_date(html: str, *, region: str) -> date | None:
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")
    tables = soup.select("table.card.releases")
    if not tables:
        return None

    def collect_dates(target_region: str | None) -> list[date]:
        dates: list[date] = []
        for table in tables:
            header = table.find("h2", class_=re.compile(r"\brelease\b"))
            if not isinstance(header, Tag):
                continue
            table_region = str(header.get("id") or "").strip().upper()
            if target_region is not None and table_region != target_region:
                continue

            tbody = table.find("tbody")
            if not isinstance(tbody, Tag):
                continue
            for row in tbody.find_all("tr"):
                if not isinstance(row, Tag):
                    continue
                cells = row.find_all("td")
                if len(cells) < 3:
                    continue
                release_type = cells[2].get_text(" ", strip=True).casefold()
                if "digital" not in release_type and "stream" not in release_type:
                    continue
                parsed = _parse_tmdb_web_release_date(cells[0].get_text(" ", strip=True))
                if parsed is not None:
                    dates.append(parsed)
        return dates

    normalized_region = (region or "").strip().upper()
    regional_dates = collect_dates(normalized_region) if normalized_region else []
    if regional_dates:
        return min(regional_dates)

    all_dates = collect_dates(None)
    if not all_dates:
        return None
    return min(all_dates)


async def _resolve_tmdb_streaming_date_from_release_page(
    movie_id: int, *, region: str
) -> date | None:
    url = f"{TMDB_WEB_BASE_URL}/movie/{movie_id}/releases"
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            response = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; plex-o-tron/1.0)"},
            )
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "[TRACKING] TMDB streaming webpage fallback failed for movie_id=%s: %s",
            movie_id,
            exc,
        )
        return None

    return _extract_tmdb_web_earliest_streaming_date(response.text, region=region)


async def resolve_tmdb_availability(
    title: str,
    *,
    year: int | None,
) -> tuple[date | None, Literal["streaming", "physical"] | None]:
    auth = _get_tmdb_auth()
    if auth is None:
        logger.info(
            "[TRACKING] TMDB availability skipped for '%s' (year=%s): missing credentials.",
            title,
            year,
        )
        return None, None

    headers, auth_params, region = auth
    logger.info(
        "[TRACKING] TMDB availability lookup started for '%s' (year=%s, region=%s).",
        title,
        year,
        region,
    )
    params: dict[str, str] = {
        **auth_params,
        "query": title,
        "include_adult": "false",
    }
    if isinstance(year, int):
        params["year"] = str(year)
        params["primary_release_year"] = str(year)

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            search_response = await client.get(
                f"{TMDB_API_BASE_URL}/search/movie",
                params=params,
                headers=headers,
            )
            search_response.raise_for_status()
            search_payload = search_response.json()
            search_results = (
                search_payload.get("results") if isinstance(search_payload, dict) else []
            )
            selected = _choose_tmdb_search_result(title, year=year, raw_results=search_results)
            if not isinstance(selected, dict):
                logger.info(
                    "[TRACKING] TMDB availability returned no movie match for '%s' (year=%s).",
                    title,
                    year,
                )
                return None, None

            raw_movie_id = selected.get("id")
            if isinstance(raw_movie_id, int):
                movie_id = raw_movie_id
            elif isinstance(raw_movie_id, str) and raw_movie_id.strip().isdigit():
                movie_id = int(raw_movie_id.strip())
            else:
                logger.info(
                    "[TRACKING] TMDB availability returned an invalid movie id for '%s' (year=%s).",
                    title,
                    year,
                )
                return None, None

            release_response = await client.get(
                f"{TMDB_API_BASE_URL}/movie/{movie_id}/release_dates",
                params=auth_params,
                headers=headers,
            )
            release_response.raise_for_status()
            release_payload = release_response.json()
    except Exception as exc:  # noqa: BLE001
        logger.info("[TRACKING] TMDB availability lookup failed for '%s': %s", title, exc)
        return None, None

    availability_date, availability_source = _extract_tmdb_earliest_availability(
        release_payload,
        region=region,
    )
    if availability_date is None or availability_source is None:
        logger.info(
            "[TRACKING] TMDB availability found no non-theatrical date for '%s' (year=%s).",
            title,
            year,
        )
        return None, None

    logger.info(
        "[TRACKING] TMDB availability resolved for '%s' (year=%s): %s (%s).",
        title,
        year,
        availability_date,
        availability_source,
    )
    return availability_date, availability_source


async def resolve_tmdb_streaming_release_date(title: str, *, year: int | None) -> date | None:
    """Resolves the earliest TMDB digital/streaming release date for a movie."""
    auth = _get_tmdb_auth()
    if auth is None:
        logger.info(
            "[TRACKING] TMDB streaming lookup skipped for '%s' (year=%s): missing credentials.",
            title,
            year,
        )
        return None

    headers, auth_params, region = auth
    logger.info(
        "[TRACKING] TMDB streaming lookup started for '%s' (year=%s, region=%s).",
        title,
        year,
        region,
    )
    params: dict[str, str] = {
        **auth_params,
        "query": title,
        "include_adult": "false",
    }
    if isinstance(year, int):
        params["year"] = str(year)
        params["primary_release_year"] = str(year)

    selected_movie_id: int | None = None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            search_response = await client.get(
                f"{TMDB_API_BASE_URL}/search/movie",
                params=params,
                headers=headers,
            )
            search_response.raise_for_status()
            search_payload = search_response.json()
            search_results = (
                search_payload.get("results") if isinstance(search_payload, dict) else []
            )
            selected = _choose_tmdb_search_result(title, year=year, raw_results=search_results)
            if not isinstance(selected, dict):
                logger.info(
                    "[TRACKING] TMDB streaming lookup returned no movie match for '%s' (year=%s).",
                    title,
                    year,
                )
                return None

            raw_movie_id = selected.get("id")
            if isinstance(raw_movie_id, int):
                selected_movie_id = raw_movie_id
            elif isinstance(raw_movie_id, str) and raw_movie_id.strip().isdigit():
                selected_movie_id = int(raw_movie_id.strip())
            else:
                logger.info(
                    "[TRACKING] TMDB streaming lookup returned invalid movie id for '%s' (year=%s).",
                    title,
                    year,
                )
                return None

            release_response = await client.get(
                f"{TMDB_API_BASE_URL}/movie/{selected_movie_id}/release_dates",
                params=auth_params,
                headers=headers,
            )
            release_response.raise_for_status()
            release_payload = release_response.json()
    except Exception as exc:  # noqa: BLE001
        logger.info("[TRACKING] TMDB streaming lookup failed for '%s': %s", title, exc)
        return None

    streaming_date = _extract_tmdb_earliest_streaming_date(release_payload, region=region)
    if streaming_date is None:
        fallback_streaming_date: date | None = None
        if isinstance(selected_movie_id, int):
            fallback_streaming_date = await _resolve_tmdb_streaming_date_from_release_page(
                selected_movie_id,
                region=region,
            )
        if fallback_streaming_date is not None:
            logger.info(
                "[TRACKING] TMDB streaming lookup resolved via webpage fallback for '%s' (year=%s): %s.",
                title,
                year,
                fallback_streaming_date,
            )
            return fallback_streaming_date
        logger.info(
            "[TRACKING] TMDB streaming lookup found no streaming date for '%s' (year=%s).",
            title,
            year,
        )
        return None

    logger.info(
        "[TRACKING] TMDB streaming lookup resolved for '%s' (year=%s): %s.",
        title,
        year,
        streaming_date,
    )
    return streaming_date


async def resolve_tmdb_inferred_year(title: str, *, year: int | None) -> int | None:
    auth = _get_tmdb_auth()
    if auth is None:
        logger.info(
            "[TRACKING] TMDB year inference skipped for '%s' (year=%s): missing credentials.",
            title,
            year,
        )
        return None

    headers, auth_params, region = auth
    logger.info(
        "[TRACKING] TMDB year inference started for '%s' (year=%s, region=%s).",
        title,
        year,
        region,
    )
    params: dict[str, str] = {
        **auth_params,
        "query": title,
        "include_adult": "false",
    }
    if isinstance(year, int):
        params["year"] = str(year)
        params["primary_release_year"] = str(year)

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            search_response = await client.get(
                f"{TMDB_API_BASE_URL}/search/movie",
                params=params,
                headers=headers,
            )
            search_response.raise_for_status()
            search_payload = search_response.json()
    except Exception as exc:  # noqa: BLE001
        logger.info("[TRACKING] TMDB year inference failed for '%s': %s", title, exc)
        return None

    search_results = search_payload.get("results") if isinstance(search_payload, dict) else []
    selected = _choose_tmdb_search_result(title, year=year, raw_results=search_results)
    if not isinstance(selected, dict):
        logger.info(
            "[TRACKING] TMDB year inference returned no movie match for '%s' (year=%s).",
            title,
            year,
        )
        return None

    inferred_year = _extract_tmdb_result_year(selected.get("release_date"))
    if not isinstance(inferred_year, int):
        logger.info(
            "[TRACKING] TMDB year inference had no parseable release year for '%s' (year=%s).",
            title,
            year,
        )
        return None

    logger.info(
        "[TRACKING] TMDB year inference resolved for '%s' (year=%s): %s.",
        title,
        year,
        inferred_year,
    )
    return inferred_year
