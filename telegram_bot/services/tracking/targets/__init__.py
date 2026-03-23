from __future__ import annotations

from typing import Any

from telegram_bot.domain.types import TrackingItem

from .base import TrackingTargetAdapter
from .movie import MovieTrackingAdapter
from .tv_ongoing import TvOngoingTrackingAdapter

MOVIE_TRACKING_ADAPTER = MovieTrackingAdapter()
TV_ONGOING_TRACKING_ADAPTER = TvOngoingTrackingAdapter()

TRACKING_ADAPTERS_BY_KIND: dict[str, TrackingTargetAdapter] = {
    "movie": MOVIE_TRACKING_ADAPTER,
    "tv": TV_ONGOING_TRACKING_ADAPTER,
}


def get_tracking_adapter_for_kind(target_kind: str | Any) -> TrackingTargetAdapter | None:
    normalized = str(target_kind or "").strip().lower()
    return TRACKING_ADAPTERS_BY_KIND.get(normalized)


def get_tracking_adapter_for_item(
    item: TrackingItem | dict[str, Any] | None,
) -> TrackingTargetAdapter | None:
    if not isinstance(item, dict):
        return None
    return get_tracking_adapter_for_kind(item.get("target_kind"))
