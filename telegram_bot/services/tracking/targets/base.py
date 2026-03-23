from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from telegram.ext import Application

from telegram_bot.domain.types import TrackingItem


@dataclass(frozen=True)
class TrackingSearchRequest:
    query: str
    media_type: str
    search_kwargs: dict[str, Any]
    parsed_info: dict[str, Any]
    clean_name: str
    pending_episode: dict[str, int] | None = None
    pending_episode_title: str | None = None


class TrackingTargetAdapter(Protocol):
    target_kind: str

    async def resolve_candidates_from_user_input(
        self,
        title_query: str,
        *,
        application: Application,
        now_utc: datetime,
    ) -> list[dict[str, Any]]: ...

    async def refresh_target_metadata(
        self,
        application: Application,
        *,
        item: TrackingItem,
        now_utc: datetime,
    ) -> TrackingItem | None: ...

    def build_search_request(self, *, item: TrackingItem) -> TrackingSearchRequest | None: ...

    def select_candidate(
        self,
        results: list[dict[str, Any]],
        *,
        search_config: dict[str, Any],
    ) -> dict[str, Any] | None: ...

    def on_queue_success(
        self,
        application: Application,
        *,
        item: TrackingItem,
        now_utc: datetime,
        linked_message_id: int | None,
        selected_candidate: dict[str, Any],
        search_request: TrackingSearchRequest,
    ) -> bool: ...

    def on_queue_failure(
        self,
        application: Application,
        *,
        item: TrackingItem,
        now_utc: datetime,
        error_message: str | None = None,
    ) -> bool: ...

    def on_fulfillment_success(
        self,
        application: Application,
        *,
        item: TrackingItem,
        parsed_info: dict[str, Any] | None,
        now_utc: datetime,
    ) -> bool: ...
