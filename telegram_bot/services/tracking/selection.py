from __future__ import annotations

from typing import Any

from telegram_bot.config import logger

RESOLUTION_ALIASES = {
    "2160p": "2160p",
    "4k": "2160p",
    "uhd": "2160p",
    "1080p": "1080p",
    "fhd": "1080p",
    "720p": "720p",
    "hd": "720p",
    "480p": "480p",
    "sd": "480p",
}

DEFAULT_TOP_MOVIE_RESOLUTION_TIER = {"1080p"}


def _normalize_resolution_label(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    return RESOLUTION_ALIASES.get(value.strip().lower())


def infer_result_resolution_tier(result: dict[str, Any]) -> str | None:
    title = str(result.get("title") or "").lower()
    if any(token in title for token in ("2160p", "4k", "uhd")):
        return "2160p"
    if any(token in title for token in ("1080p", "fhd")):
        return "1080p"
    if any(token in title for token in ("720p",)):
        return "720p"
    if any(token in title for token in ("480p", "dvdrip", "sd")):
        return "480p"
    return None


def resolve_top_movie_resolution_tiers(search_config: dict[str, Any]) -> set[str]:
    preferences = search_config.get("preferences") if isinstance(search_config, dict) else None
    movies = preferences.get("movies") if isinstance(preferences, dict) else None
    resolutions = movies.get("resolutions") if isinstance(movies, dict) else None
    if not isinstance(resolutions, dict):
        return set(DEFAULT_TOP_MOVIE_RESOLUTION_TIER)

    tier_scores: dict[str, float] = {}
    for raw_label, raw_score in resolutions.items():
        normalized = _normalize_resolution_label(str(raw_label))
        if normalized is None:
            continue
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            continue
        current = tier_scores.get(normalized)
        if current is None or score > current:
            tier_scores[normalized] = score

    if not tier_scores:
        return set(DEFAULT_TOP_MOVIE_RESOLUTION_TIER)

    top_score = max(tier_scores.values())
    return {tier for tier, score in tier_scores.items() if score == top_score}


def select_best_auto_download_candidate(
    results: list[dict[str, Any]],
    *,
    search_config: dict[str, Any],
) -> dict[str, Any] | None:
    """Selects the best scored result from the highest configured resolution tier."""
    if not results:
        return None

    top_tiers = resolve_top_movie_resolution_tiers(search_config)
    eligible: list[dict[str, Any]] = []
    for result in sorted(results, key=lambda item: item.get("score", 0), reverse=True):
        tier = infer_result_resolution_tier(result)
        if tier in top_tiers:
            eligible.append(result)

    if not eligible:
        logger.info(
            "[TRACKING] No candidate matched configured top resolution tier(s): %s.",
            ", ".join(sorted(top_tiers)),
        )
        return None

    return eligible[0]
