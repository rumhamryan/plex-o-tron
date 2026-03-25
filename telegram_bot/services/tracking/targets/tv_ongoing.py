from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, cast

from telegram.ext import Application

from telegram_bot.domain.types import TrackingEpisodeRef, TrackingItem, TrackingTargetPayload
from telegram_bot.services import plex_service
from telegram_bot.services.tracking import selection, tv_next_episode
from telegram_bot.services.tracking.manager import (
    calculate_next_hourly_check,
    calculate_next_weekly_metadata_check,
    calculate_release_day_first_check_utc,
    get_tracking_item,
    get_tracking_timezone,
    isoformat_utc,
    mark_tracking_fulfillment_success,
    mark_tracking_hourly_retry,
    mark_tracking_waiting_fulfillment,
    persist_tracking_state_from_bot_data,
)

from .base import TrackingSearchRequest


def _coerce_episode_ref(raw_value: Any) -> TrackingEpisodeRef | None:
    if not isinstance(raw_value, dict):
        return None
    season = raw_value.get("season")
    episode = raw_value.get("episode")
    if isinstance(season, int) and isinstance(episode, int) and season > 0 and episode > 0:
        return {"season": season, "episode": episode}
    return None


def _ensure_payload(item: TrackingItem) -> TrackingTargetPayload:
    raw_payload = item.get("target_payload")
    if isinstance(raw_payload, dict):
        return raw_payload
    payload: TrackingTargetPayload = {}
    item["target_payload"] = payload
    return payload


def _coerce_iso_date(raw_value: Any) -> date | None:
    if not isinstance(raw_value, str):
        return None
    normalized = raw_value.strip()
    if not normalized:
        return None
    if "T" in normalized:
        normalized = normalized.split("T", 1)[0]
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        return None


def _resolve_display_title(item: TrackingItem) -> str:
    payload = _ensure_payload(item)
    value = str(
        item.get("display_title")
        or payload.get("canonical_title")
        or item.get("canonical_title")
        or item.get("title")
        or "TV Show"
    ).strip()
    return value or "TV Show"


class TvOngoingTrackingAdapter:
    target_kind = "tv"

    async def resolve_candidates_from_user_input(
        self,
        title_query: str,
        *,
        application: Application,
        now_utc: datetime,
    ) -> list[dict[str, Any]]:
        candidates = await tv_next_episode.find_tv_tracking_candidates(title_query)
        return [dict(candidate) for candidate in candidates]

    async def refresh_target_metadata(
        self,
        application: Application,
        *,
        item: TrackingItem,
        now_utc: datetime,
    ) -> TrackingItem | None:
        item_id = str(item.get("id") or "")
        if not item_id:
            return item

        payload = _ensure_payload(item)
        tmdb_series_id = payload.get("tmdb_series_id")
        if not isinstance(tmdb_series_id, int) or tmdb_series_id <= 0:
            item["status"] = "awaiting_metadata"  # type: ignore[typeddict-item]
            item["next_check_at_utc"] = isoformat_utc(calculate_next_weekly_metadata_check(now_utc))
            item["last_checked_at_utc"] = isoformat_utc(now_utc)
            persist_tracking_state_from_bot_data(application)
            return get_tracking_item(application, item_id)

        local_today = now_utc.astimezone(get_tracking_timezone(application.bot_data)).date()
        fallback_title = _resolve_display_title(item)

        context = cast(Any, SimpleNamespace(bot_data=application.bot_data))
        episode_cursor = _coerce_episode_ref(payload.get("episode_cursor"))

        async def _existing_episode_lookup(show_title: str, season: int) -> set[int]:
            return await plex_service.get_existing_episodes_for_season(
                context,
                show_title=show_title,
                season=season,
            )

        resolution = await tv_next_episode.resolve_next_ongoing_episode(
            tmdb_series_id=tmdb_series_id,
            fallback_show_title=fallback_title,
            episode_cursor=(
                {
                    "season": int(episode_cursor["season"]),
                    "episode": int(episode_cursor["episode"]),
                }
                if episode_cursor is not None
                else None
            ),
            today=local_today,
            existing_episode_lookup=_existing_episode_lookup,
        )

        canonical_title = (
            str(resolution.get("canonical_title") or fallback_title).strip() or fallback_title
        )
        payload["canonical_title"] = canonical_title
        payload["tmdb_series_id"] = int(tmdb_series_id)
        item["display_title"] = canonical_title
        item["canonical_title"] = canonical_title
        item["title"] = canonical_title
        item["schedule_mode"] = "ongoing_next_episode"  # type: ignore[typeddict-item]
        item["target_kind"] = "tv"
        item["target_identity"] = f"tv:tmdb:{int(tmdb_series_id)}"
        item["year"] = None
        item["release_date_status"] = "unknown"
        item["availability_date"] = None
        item["availability_source"] = None
        item["fulfillment_state"] = "fulfilled" if item.get("status") == "fulfilled" else "pending"  # type: ignore[typeddict-item]

        metadata_refresh_failed = bool(resolution.get("metadata_refresh_failed"))
        next_episode = resolution.get("next_episode")
        if isinstance(next_episode, dict):
            payload["pending_episode"] = _coerce_episode_ref(next_episode)
            title_raw = next_episode.get("title")
            payload["pending_episode_title"] = (
                title_raw.strip() if isinstance(title_raw, str) and title_raw.strip() else None
            )
            air_date = next_episode.get("air_date")
            payload["pending_episode_air_date"] = (
                air_date.isoformat() if isinstance(air_date, date) else None
            )
        elif not metadata_refresh_failed:
            payload["pending_episode"] = None
            payload["pending_episode_title"] = None
            payload["pending_episode_air_date"] = None

        if metadata_refresh_failed:
            # Preserve any known pending episode window and retry quickly after transient TMDB errors.
            pending_air_date = _coerce_iso_date(payload.get("pending_episode_air_date"))
            if isinstance(pending_air_date, date):
                first_check = calculate_release_day_first_check_utc(
                    pending_air_date,
                    local_timezone=get_tracking_timezone(application.bot_data),
                    now_utc=now_utc,
                )
                if first_check <= now_utc:
                    item["status"] = "searching"  # type: ignore[typeddict-item]
                    item["next_check_at_utc"] = isoformat_utc(now_utc)
                else:
                    item["status"] = "awaiting_window"  # type: ignore[typeddict-item]
                    item["next_check_at_utc"] = isoformat_utc(first_check)
            else:
                item["status"] = "awaiting_metadata"  # type: ignore[typeddict-item]
                item["next_check_at_utc"] = isoformat_utc(calculate_next_hourly_check(now_utc))

            item["last_checked_at_utc"] = isoformat_utc(now_utc)
            persist_tracking_state_from_bot_data(application)
            return get_tracking_item(application, item_id)

        state = str(resolution.get("state") or "awaiting_metadata")
        if state == "search_now":
            item["status"] = "searching"  # type: ignore[typeddict-item]
            item["next_check_at_utc"] = isoformat_utc(now_utc)
        elif state == "await_window":
            next_air_date = resolution.get("next_air_date")
            if isinstance(next_air_date, date):
                first_check = calculate_release_day_first_check_utc(
                    next_air_date,
                    local_timezone=get_tracking_timezone(application.bot_data),
                    now_utc=now_utc,
                )
                if first_check <= now_utc:
                    item["status"] = "searching"  # type: ignore[typeddict-item]
                    item["next_check_at_utc"] = isoformat_utc(now_utc)
                else:
                    item["status"] = "awaiting_window"  # type: ignore[typeddict-item]
                    item["next_check_at_utc"] = isoformat_utc(first_check)
            else:
                item["status"] = "awaiting_metadata"  # type: ignore[typeddict-item]
                item["next_check_at_utc"] = isoformat_utc(
                    calculate_next_weekly_metadata_check(now_utc)
                )
        else:
            item["status"] = "awaiting_metadata"  # type: ignore[typeddict-item]
            item["next_check_at_utc"] = isoformat_utc(calculate_next_weekly_metadata_check(now_utc))
            payload["pending_episode"] = None
            payload["pending_episode_title"] = None
            payload["pending_episode_air_date"] = None

        item["last_checked_at_utc"] = isoformat_utc(now_utc)
        persist_tracking_state_from_bot_data(application)
        return get_tracking_item(application, item_id)

    def build_search_request(self, *, item: TrackingItem) -> TrackingSearchRequest | None:
        payload = _ensure_payload(item)
        episode_ref = _coerce_episode_ref(payload.get("pending_episode"))
        if episode_ref is None:
            return None

        show_title = _resolve_display_title(item)
        season = int(episode_ref["season"])
        episode = int(episode_ref["episode"])
        query = f"{show_title} S{season:02d}E{episode:02d}"
        parsed_info: dict[str, Any] = {
            "type": "tv",
            "title": show_title,
            "season": season,
            "episode": episode,
        }
        episode_title = payload.get("pending_episode_title")
        if isinstance(episode_title, str) and episode_title.strip():
            parsed_info["episode_title"] = episode_title.strip()

        return TrackingSearchRequest(
            query=query,
            media_type="tv",
            search_kwargs={},
            parsed_info=parsed_info,
            clean_name=f"{show_title} S{season:02d}E{episode:02d}",
            pending_episode={
                "season": int(episode_ref["season"]),
                "episode": int(episode_ref["episode"]),
            },
            pending_episode_title=episode_title if isinstance(episode_title, str) else None,
        )

    def select_candidate(
        self,
        results: list[dict[str, Any]],
        *,
        search_config: dict[str, Any],
    ) -> dict[str, Any] | None:
        return selection.select_best_auto_download_candidate(
            results,
            search_config=search_config,
            media_type="tv",
        )

    def on_queue_success(
        self,
        application: Application,
        *,
        item: TrackingItem,
        now_utc: datetime,
        linked_message_id: int | None,
        selected_candidate: dict[str, Any],
        search_request: TrackingSearchRequest,
    ) -> bool:
        return mark_tracking_waiting_fulfillment(
            application,
            item_id=str(item.get("id")),
            linked_message_id=linked_message_id,
            pending_episode=search_request.pending_episode,
            pending_episode_title=search_request.pending_episode_title,
            now_utc=now_utc,
        )

    def on_queue_failure(
        self,
        application: Application,
        *,
        item: TrackingItem,
        now_utc: datetime,
        error_message: str | None = None,
    ) -> bool:
        return mark_tracking_hourly_retry(
            application,
            item_id=str(item.get("id")),
            now_utc=now_utc,
            error_message=error_message,
        )

    def on_fulfillment_success(
        self,
        application: Application,
        *,
        item: TrackingItem,
        parsed_info: dict[str, Any] | None,
        now_utc: datetime,
    ) -> bool:
        return mark_tracking_fulfillment_success(
            application,
            item_id=str(item.get("id")),
            parsed_info=parsed_info,
            now_utc=now_utc,
        )
