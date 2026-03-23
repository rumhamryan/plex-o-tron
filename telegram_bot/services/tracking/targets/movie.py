from __future__ import annotations

from datetime import date, datetime
from typing import Any

from telegram.ext import Application

from telegram_bot.domain.types import TrackingItem
from telegram_bot.services.tracking import movie_release_dates, selection
from telegram_bot.services.tracking.manager import (
    get_tracking_item,
    get_tracking_timezone,
    mark_tracking_fulfilled,
    mark_tracking_hourly_retry,
    mark_tracking_waiting_fulfillment,
    mark_tracking_weekly_metadata_retry,
    set_tracking_release_window,
)

from .base import TrackingSearchRequest


def _movie_title_and_year(item: TrackingItem) -> tuple[str, int | None]:
    payload = item.get("target_payload")
    payload_dict = payload if isinstance(payload, dict) else {}
    canonical_title = str(
        payload_dict.get("canonical_title")
        or item.get("canonical_title")
        or item.get("display_title")
        or item.get("title")
        or "Unknown"
    ).strip()
    raw_year = payload_dict.get("year", item.get("year"))
    year = raw_year if isinstance(raw_year, int) else None
    return canonical_title or "Unknown", year


def _movie_release_date(item: TrackingItem) -> date | None:
    payload = item.get("target_payload")
    payload_dict = payload if isinstance(payload, dict) else {}
    value = payload_dict.get("availability_date", item.get("availability_date"))
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


class MovieTrackingAdapter:
    target_kind = "movie"

    async def resolve_candidates_from_user_input(
        self,
        title_query: str,
        *,
        application: Application,
        now_utc: datetime,
    ) -> list[dict[str, Any]]:
        local_today = now_utc.astimezone(get_tracking_timezone(application.bot_data)).date()
        candidates = await movie_release_dates.find_movie_tracking_candidates(
            title_query,
            today=local_today,
        )
        return [
            {
                **candidate,
                "target_kind": "movie",
                "schedule_mode": "future_release",
            }
            for candidate in candidates
        ]

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

        status = str(item.get("status") or "")
        canonical_title, year = _movie_title_and_year(item)
        if status == "awaiting_metadata":
            local_today = now_utc.astimezone(get_tracking_timezone(application.bot_data)).date()
            resolved = await movie_release_dates.resolve_movie_tracking_target(
                canonical_title,
                year=year,
                today=local_today,
            )
            availability_date = resolved.get("availability_date")
            if availability_date is None:
                mark_tracking_weekly_metadata_retry(
                    application,
                    item_id=item_id,
                    now_utc=now_utc,
                )
            else:
                set_tracking_release_window(
                    application,
                    item_id=item_id,
                    availability_date=availability_date,
                    availability_source=resolved.get("availability_source"),
                    now_utc=now_utc,
                )
            return get_tracking_item(application, item_id)

        if status == "awaiting_window":
            release_day = _movie_release_date(item)
            if release_day is None:
                mark_tracking_weekly_metadata_retry(
                    application,
                    item_id=item_id,
                    now_utc=now_utc,
                    error_message="missing_movie_release_window_date",
                )
                return get_tracking_item(application, item_id)

            payload = item.get("target_payload")
            payload_dict = payload if isinstance(payload, dict) else {}
            set_tracking_release_window(
                application,
                item_id=item_id,
                availability_date=release_day,
                availability_source=payload_dict.get("availability_source"),
                now_utc=now_utc,
            )
            return get_tracking_item(application, item_id)

        return item

    def build_search_request(self, *, item: TrackingItem) -> TrackingSearchRequest | None:
        canonical_title, year = _movie_title_and_year(item)
        if not canonical_title:
            return None

        parsed_info: dict[str, Any] = {"type": "movie", "title": canonical_title}
        clean_name = canonical_title
        search_kwargs: dict[str, Any] = {}
        if isinstance(year, int):
            parsed_info["year"] = year
            clean_name = f"{canonical_title} ({year})"
            search_kwargs["year"] = str(year)

        return TrackingSearchRequest(
            query=canonical_title,
            media_type="movie",
            search_kwargs=search_kwargs,
            parsed_info=parsed_info,
            clean_name=clean_name,
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
            media_type="movie",
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
        return mark_tracking_fulfilled(
            application,
            item_id=str(item.get("id")),
            now_utc=now_utc,
        )
