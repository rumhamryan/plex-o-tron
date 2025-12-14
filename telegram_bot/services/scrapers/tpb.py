from __future__ import annotations

import re
import urllib.parse
from typing import Any, Iterable

import httpx
from telegram.ext import ContextTypes
from thefuzz import fuzz

from ...config import MAX_TORRENT_SIZE_GB, logger
from ...utils import parse_codec, parse_torrent_name, score_torrent_result

_API_URL = "https://apibay.org/q.php"
_DEFAULT_LIMIT = 40
_FUZZ_THRESHOLD = 78
_STOP_WORDS = {"the", "a", "an", "of", "and"}

_TPB_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://public.popcorn-tracker.org:6969/announce",
    "udp://tracker.internetwarriors.net:1337/announce",
]
_TRACKER_QUERY = "".join(
    f"&tr={urllib.parse.quote_plus(tracker)}" for tracker in _TPB_TRACKERS
)

_CATEGORY_MAP: dict[str, tuple[str, ...]] = {
    # Include HD/UHD variants so we do not miss higher quality releases.
    "movie": ("201", "207", "211"),
    "tv": ("205", "208", "212"),
}


async def scrape_tpb(
    query: str,
    media_type: str,
    _search_url_template: str,
    context: ContextTypes.DEFAULT_TYPE,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Fetch torrents from TPB via the apibay JSON API."""

    if not isinstance(query, str) or not query.strip():
        return []

    media_type_key = (
        "tv"
        if isinstance(media_type, str) and media_type.lower().startswith("tv")
        else "movie"
    )
    prefs_key = "movies" if media_type_key == "movie" else "tv"
    preferences = (
        context.bot_data.get("SEARCH_CONFIG", {})
        .get("preferences", {})
        .get(prefs_key, {})
    )
    if not preferences:
        logger.info(
            "[SCRAPER] TPB: Preferences not configured for %s searches.", prefs_key
        )
        return []

    category_ids = _CATEGORY_MAP.get(media_type_key, _CATEGORY_MAP["movie"])
    params = {
        "q": query.strip(),
        "cat": ",".join(category_ids),
    }
    limit = kwargs.get("limit")
    try:
        limit_value = (
            int(limit) if isinstance(limit, int) and limit > 0 else _DEFAULT_LIMIT
        )
    except Exception:
        limit_value = _DEFAULT_LIMIT

    base_filter = kwargs.get("base_query_for_filter", query)

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(_API_URL, params=params)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:  # noqa: BLE001
        logger.error("[SCRAPER] TPB: Request error for query '%s': %s", query, exc)
        return []
    except ValueError as exc:  # JSON decode
        logger.error("[SCRAPER] TPB: Failed to parse JSON for '%s': %s", query, exc)
        return []

    if not isinstance(payload, list):
        logger.warning("[SCRAPER] TPB: Unexpected payload type %s", type(payload))
        return []

    results = _transform_results(
        payload,
        query=query,
        base_filter=base_filter,
        media_type_key=media_type_key,
        preferences=preferences,
        limit=limit_value,
    )
    logger.info("[SCRAPER] TPB: Found %d torrents for query '%s'.", len(results), query)
    return results


def _transform_results(
    entries: Iterable[dict[str, Any]],
    *,
    query: str,
    base_filter: str,
    media_type_key: str,
    preferences: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    target_info = parse_torrent_name(base_filter or query)
    normalized_target = _normalize(target_info.get("title") or base_filter or query)
    if not normalized_target:
        normalized_target = _normalize(query)

    target_year = _safe_int(target_info.get("year"))
    target_season = _safe_int(target_info.get("season"))
    target_episode = _safe_int(target_info.get("episode"))
    if target_episode and not target_season:
        target_episode = 0

    filtered: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        info_hash = entry.get("info_hash")
        raw_title = entry.get("name")
        if not isinstance(info_hash, str) or not isinstance(raw_title, str):
            continue
        if len(info_hash) < 10 or info_hash in seen_hashes:
            continue

        parsed = parse_torrent_name(raw_title)
        candidate_title = parsed.get("title") or raw_title
        normalized_candidate = _normalize(candidate_title)
        if not normalized_candidate:
            normalized_candidate = _normalize(raw_title)
        fuzz_score = fuzz.token_set_ratio(normalized_target, normalized_candidate)
        if fuzz_score < _FUZZ_THRESHOLD:
            continue

        if media_type_key == "movie" and target_year:
            cand_year = _safe_int(parsed.get("year"))
            if cand_year and cand_year != target_year:
                continue
            if not cand_year and not _year_in_text(target_year, raw_title):
                continue
        if media_type_key == "tv":
            if not _matches_tv(parsed, target_season, target_episode, raw_title):
                continue

        size_bytes = _safe_int(entry.get("size"))
        if size_bytes <= 0:
            continue
        size_gb = size_bytes / (1024**3)
        if size_gb > MAX_TORRENT_SIZE_GB:
            continue

        seeders = _safe_int(entry.get("seeders"))
        leechers = _safe_int(entry.get("leechers"))
        uploader = entry.get("username") or "Anonymous"
        score = score_torrent_result(raw_title, uploader, preferences, seeders=seeders)
        if score <= 0:
            continue

        magnet = _build_magnet(info_hash, raw_title)
        filtered.append(
            {
                "title": raw_title,
                "page_url": magnet,
                "score": score,
                "source": "tpb",
                "uploader": uploader,
                "size_gb": size_gb,
                "codec": parse_codec(raw_title),
                "seeders": seeders,
                "leechers": leechers,
                "year": _safe_int(parsed.get("year")) or None,
            }
        )
        seen_hashes.add(info_hash)

    filtered.sort(key=lambda item: (item["seeders"], item["score"]), reverse=True)
    return filtered[:limit]


def _build_magnet(info_hash: str, name: str) -> str:
    return (
        f"magnet:?xt=urn:btih:{info_hash}"
        f"&dn={urllib.parse.quote_plus(name)}"
        f"{_TRACKER_QUERY}"
    )


def _normalize(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9\s]", " ", value.lower())
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return text
    tokens = [tok for tok in text.split(" ") if tok and tok not in _STOP_WORDS]
    return " ".join(tokens)


def _safe_int(value: Any) -> int:
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else 0
    except (TypeError, ValueError):
        return 0


def _year_in_text(year: int, raw: str) -> bool:
    return str(year) in raw


def _matches_tv(
    parsed: dict[str, Any], target_season: int, target_episode: int, raw_title: str
) -> bool:
    if target_season <= 0:
        return True

    parsed_season = _safe_int(parsed.get("season"))
    parsed_episode = _safe_int(parsed.get("episode"))
    parsed_is_pack = bool(parsed.get("is_season_pack"))

    tokens = _season_tokens(target_season, target_episode)

    if target_episode > 0:
        if parsed_season == target_season and parsed_episode == target_episode:
            return True
        if parsed_is_pack and parsed_season == target_season:
            return True
        return _title_contains_tokens(raw_title, tokens)

    if parsed_season == target_season or (
        parsed_is_pack and parsed_season == target_season
    ):
        return True
    return _title_contains_tokens(raw_title, tokens)


def _season_tokens(season: int, episode: int) -> set[str]:
    tokens = {
        f"S{season:02d}",
        f"S{season}",
        f"SEASON{season}",
    }
    if episode > 0:
        tokens.update(
            {
                f"S{season:02d}E{episode:02d}",
                f"S{season}E{episode}",
                f"{season}X{episode:02d}",
                f"{season}X{episode}",
            }
        )
    return tokens


def _title_contains_tokens(raw_title: str, tokens: Iterable[str]) -> bool:
    tokens = list(tokens)
    if not tokens:
        return False
    upper = raw_title.upper()
    compact = re.sub(r"[^A-Z0-9]", "", upper)
    return any(token in upper or token in compact for token in tokens)
