from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import date, datetime
from typing import Any, Literal, TypedDict

import httpx
import wikipedia
from bs4 import BeautifulSoup, Tag

from telegram_bot.config import logger
from telegram_bot.services import scraping_service
from telegram_bot.services.scrapers.wikipedia.dates import _extract_release_date_iso
from telegram_bot.services.scrapers.wikipedia.fetch import _fetch_html_from_page

STREAMING_KEYWORDS = (
    "stream",
    "streaming",
    "digital",
    "vod",
    "video on demand",
    "on demand",
)
PHYSICAL_KEYWORDS = (
    "blu-ray",
    "blu ray",
    "dvd",
    "home media",
    "home video",
    "physical media",
    "4k ultra hd",
    "uhd",
)
TMDB_DIGITAL_RELEASE_TYPE = 4
TMDB_PHYSICAL_RELEASE_TYPE = 5
TMDB_API_BASE_URL = "https://api.themoviedb.org/3"
DEFAULT_TRACKING_RELEASE_OVERRIDES_FILE = "tracking_release_overrides.json"
TRACKING_RELEASE_OVERRIDES_ENV = "TRACKING_RELEASE_OVERRIDES_FILE"


class MovieTrackingResolution(TypedDict):
    title: str
    canonical_title: str
    year: int | None
    is_released: bool
    release_date_status: Literal["unknown", "confirmed"]
    availability_date: date | None
    availability_source: Literal["streaming", "physical"] | None


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


def _contains_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(keyword in lowered for keyword in keywords)


def _parse_iso_to_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


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


def _parse_availability_source(value: Any) -> Literal["streaming", "physical"] | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().casefold()
    if lowered == "streaming":
        return "streaming"
    if lowered == "physical":
        return "physical"
    return None


def _load_manual_release_overrides(
    file_path: str,
) -> dict[tuple[str, int | None], tuple[date, Literal["streaming", "physical"]]]:
    if not os.path.exists(file_path):
        return {}
    try:
        with open(file_path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:  # noqa: BLE001
        return {}

    entries = payload.get("movies") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return {}

    overrides: dict[tuple[str, int | None], tuple[date, Literal["streaming", "physical"]]] = {}
    for raw in entries:
        if not isinstance(raw, dict):
            continue

        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            continue

        year_raw = raw.get("year")
        year: int | None
        if isinstance(year_raw, int):
            year = year_raw
        elif isinstance(year_raw, str) and year_raw.strip().isdigit():
            year = int(year_raw.strip())
        else:
            year = None

        date_raw = raw.get("availability_date")
        if not isinstance(date_raw, str):
            continue
        availability_date = _parse_iso_to_date(date_raw)
        if availability_date is None:
            continue

        source = _parse_availability_source(raw.get("availability_source"))
        if source is None:
            continue

        normalized = _normalize_title(title, year).casefold()
        overrides[(normalized, year)] = (availability_date, source)

    return overrides


def _resolve_manual_override(
    title: str,
    *,
    year: int | None,
) -> tuple[date | None, Literal["streaming", "physical"] | None]:
    file_path = (
        os.getenv(TRACKING_RELEASE_OVERRIDES_ENV) or DEFAULT_TRACKING_RELEASE_OVERRIDES_FILE
    ).strip()
    if not file_path:
        return None, None

    overrides = _load_manual_release_overrides(file_path)
    if not overrides:
        return None, None

    normalized = _normalize_title(title, year).casefold()
    if isinstance(year, int):
        direct = overrides.get((normalized, year))
        if direct is not None:
            return direct
    fallback = overrides.get((normalized, None))
    if fallback is not None:
        return fallback
    return None, None


def _extract_earliest_availability_from_html(
    html: str,
) -> tuple[date | None, Literal["streaming", "physical"] | None]:
    soup = BeautifulSoup(html, "html.parser")
    infobox = soup.find("table", class_=re.compile(r"\binfobox\b"))
    if not isinstance(infobox, Tag):
        return None, None

    streaming_dates: list[date] = []
    physical_dates: list[date] = []
    for row in infobox.find_all("tr"):
        if not isinstance(row, Tag):
            continue
        header = row.find("th")
        value = row.find("td")
        if not isinstance(header, Tag) or not isinstance(value, Tag):
            continue

        header_text = header.get_text(" ", strip=True)
        value_text = value.get_text(" ", strip=True)
        combined = f"{header_text} {value_text}"

        release_iso = _extract_release_date_iso(value_text)
        release_date = _parse_iso_to_date(release_iso)
        if release_date is None:
            continue

        is_streaming = _contains_keyword(combined, STREAMING_KEYWORDS)
        is_physical = _contains_keyword(combined, PHYSICAL_KEYWORDS)
        if is_streaming:
            streaming_dates.append(release_date)
        if is_physical:
            physical_dates.append(release_date)

    earliest_streaming = min(streaming_dates) if streaming_dates else None
    earliest_physical = min(physical_dates) if physical_dates else None
    if earliest_streaming is None and earliest_physical is None:
        return None, None
    if earliest_streaming is None:
        return earliest_physical, "physical"
    if earliest_physical is None:
        return earliest_streaming, "streaming"
    if earliest_streaming <= earliest_physical:
        return earliest_streaming, "streaming"
    return earliest_physical, "physical"


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


async def _resolve_tmdb_availability(
    title: str,
    *,
    year: int | None,
) -> tuple[date | None, Literal["streaming", "physical"] | None]:
    auth = _get_tmdb_auth()
    if auth is None:
        return None, None

    headers, auth_params, region = auth
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
                return None, None

            raw_movie_id = selected.get("id")
            if isinstance(raw_movie_id, int):
                movie_id = raw_movie_id
            elif isinstance(raw_movie_id, str) and raw_movie_id.strip().isdigit():
                movie_id = int(raw_movie_id.strip())
            else:
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

    return _extract_tmdb_earliest_availability(release_payload, region=region)


async def _resolve_tmdb_inferred_year(title: str, *, year: int | None) -> int | None:
    auth = _get_tmdb_auth()
    if auth is None:
        return None

    headers, auth_params, _ = auth
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
        return None
    return _extract_tmdb_result_year(selected.get("release_date"))


async def _resolve_movie_page_html(title: str, year: int | None) -> tuple[str | None, str]:
    queries: list[str] = []
    normalized_title = _normalize_title(title, year)
    if year is not None:
        queries.extend(
            [
                f"{normalized_title} ({year} film)",
                f"{normalized_title} ({year})",
            ]
        )
    queries.extend(
        [
            f"{normalized_title} (film)",
            normalized_title,
        ]
    )

    seen: set[str] = set()
    for query in queries:
        cleaned_query = query.strip()
        if not cleaned_query or cleaned_query in seen:
            continue
        seen.add(cleaned_query)

        try:
            page = await asyncio.to_thread(wikipedia.page, cleaned_query, auto_suggest=False)
        except Exception:  # noqa: BLE001
            continue

        html = await _fetch_html_from_page(page)
        if not html:
            continue
        canonical_title = _normalize_title(page.title, year)
        return html, canonical_title

    return None, normalized_title


async def resolve_movie_tracking_target(
    title: str,
    *,
    year: int | None = None,
    today: date | None = None,
) -> MovieTrackingResolution:
    """Resolves release availability for a movie target."""
    reference_day = today or date.today()
    resolved_year = year
    html, canonical_title = await _resolve_movie_page_html(title, year)
    availability_date: date | None = None
    availability_source: Literal["streaming", "physical"] | None = None
    if html:
        availability_date, availability_source = _extract_earliest_availability_from_html(html)

    manual_date, manual_source = _resolve_manual_override(canonical_title, year=year)
    if manual_date is None and canonical_title != title:
        manual_date, manual_source = _resolve_manual_override(title, year=year)
    if manual_date is not None and manual_source is not None:
        availability_date, availability_source = manual_date, manual_source
        logger.info(
            "[TRACKING] Using manual release override for '%s' (%s): %s (%s).",
            canonical_title,
            year,
            availability_date,
            availability_source,
        )

    if availability_date is None:
        tmdb_lookup_title = (
            canonical_title if canonical_title and canonical_title != "Unknown" else title
        )
        tmdb_date, tmdb_source = await _resolve_tmdb_availability(
            tmdb_lookup_title,
            year=year,
        )
        if tmdb_date is None and tmdb_lookup_title != title:
            tmdb_date, tmdb_source = await _resolve_tmdb_availability(title, year=year)
        if tmdb_date is not None and tmdb_source is not None:
            if availability_date is not None and (
                availability_date != tmdb_date or availability_source != tmdb_source
            ):
                logger.info(
                    "[TRACKING] Availability mismatch for '%s': wiki=%s/%s tmdb=%s/%s. "
                    "Preferring TMDB.",
                    canonical_title,
                    availability_date,
                    availability_source,
                    tmdb_date,
                    tmdb_source,
                )
            availability_date, availability_source = tmdb_date, tmdb_source

        if resolved_year is None:
            inferred_year = await _resolve_tmdb_inferred_year(tmdb_lookup_title, year=year)
            if inferred_year is None and tmdb_lookup_title != title:
                inferred_year = await _resolve_tmdb_inferred_year(title, year=year)
            if isinstance(inferred_year, int):
                resolved_year = inferred_year

    is_released = False
    if availability_date is not None:
        is_released = availability_date <= reference_day
    elif isinstance(resolved_year, int):
        is_released = resolved_year < reference_day.year
    else:
        # Unknown availability should remain schedulable for metadata-only weekly checks.
        is_released = False

    return {
        "title": title.strip() or canonical_title,
        "canonical_title": canonical_title,
        "year": resolved_year,
        "is_released": is_released,
        "release_date_status": "confirmed" if availability_date else "unknown",
        "availability_date": availability_date,
        "availability_source": availability_source,
    }


async def find_movie_tracking_candidates(
    movie_title: str,
    *,
    today: date | None = None,
) -> list[MovieTrackingResolution]:
    """Returns normalized movie candidates with release metadata."""
    title = (movie_title or "").strip()
    if not title:
        return []

    years, corrected_title = await scraping_service.fetch_movie_years_from_wikipedia(title)
    normalized_years = sorted({int(year) for year in years if isinstance(year, int)})
    wikipedia_canonical = corrected_title.strip() if isinstance(corrected_title, str) else ""
    has_wikipedia_match = bool(normalized_years) or bool(wikipedia_canonical)
    if not has_wikipedia_match:
        logger.info(
            "[TRACKING] No confident Wikipedia movie match for '%s'; refusing to schedule candidate.",
            title,
        )
        return []

    canonical_input = wikipedia_canonical or title

    candidates: list[MovieTrackingResolution] = []
    if normalized_years:
        for year in normalized_years:
            resolved = await resolve_movie_tracking_target(
                canonical_input,
                year=year,
                today=today,
            )
            candidates.append(resolved)
    else:
        candidates.append(
            await resolve_movie_tracking_target(
                canonical_input,
                year=None,
                today=today,
            )
        )

    deduped: dict[tuple[str, int | None], MovieTrackingResolution] = {}
    for candidate in candidates:
        key = (candidate["canonical_title"].casefold(), candidate.get("year"))
        deduped[key] = candidate

    ordered = sorted(
        deduped.values(),
        key=lambda item: (
            item.get("year") is None,
            int(item.get("year") or 0),
            item.get("canonical_title", "").casefold(),
        ),
    )
    logger.info("[TRACKING] Resolved %d candidate(s) for '%s'.", len(ordered), title)
    return ordered
