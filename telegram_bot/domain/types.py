# telegram_bot/domain/types.py

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

__all__ = [
    "SourceDict",
    "DownloadData",
    "BatchCollectionMeta",
    "BatchMeta",
    "TrackingTargetKind",
    "TrackingScheduleMode",
    "TrackingStatus",
    "TrackingItem",
    "TrackingStateFile",
    "PostProcessingResult",
]


class SourceDict(TypedDict, total=False):
    value: str
    type: Literal["magnet", "url", "file"]
    parsed_info: dict[str, Any]
    info_url: NotRequired[str | None]
    clean_name: NotRequired[str]
    batch_id: NotRequired[str]
    original_message_id: NotRequired[int]
    message_id: NotRequired[int]
    tracking_item_id: NotRequired[str]


class DownloadData(TypedDict, total=False):
    source_dict: SourceDict
    chat_id: int
    message_id: int
    save_path: str
    lock: Any
    task: Any
    handle: Any
    is_paused: bool
    cancellation_pending: bool
    requeued: bool
    metadata_timeout_occurred: bool


class BatchCollectionMeta(TypedDict, total=False):
    name: str
    fs_name: NotRequired[str]
    movies: NotRequired[list[dict[str, Any]]]


class BatchMeta(TypedDict, total=False):
    total: int
    done: int
    media_type: Literal["tv", "movie"]
    scanned: bool
    summaries: list[str]
    collection: NotRequired[BatchCollectionMeta]


TrackingTargetKind = Literal["movie", "tv"]
TrackingScheduleMode = Literal["future_release", "ongoing_next_episode"]

TrackingStatus = Literal[
    # v2 states
    "awaiting_metadata",
    "awaiting_window",
    "searching",
    "waiting_fulfillment",
    "fulfilled",
    "cancelled",
    # legacy v1 states kept for tolerant coercion/compatibility
    "pending_date",
    "waiting_release_window",
    "watching_release",
]
TrackingReleaseDateStatus = Literal["unknown", "confirmed"]
TrackingAvailabilitySource = Literal["streaming", "physical"]
TrackingFulfillmentState = Literal["pending", "fulfilled"]


class TrackingEpisodeRef(TypedDict):
    season: int
    episode: int


class TrackingRetryState(TypedDict, total=False):
    consecutive_failures: int
    last_error: str | None


class TrackingTargetPayload(TypedDict, total=False):
    # Shared
    canonical_title: str

    # Movie
    year: NotRequired[int | None]
    release_date_status: NotRequired[TrackingReleaseDateStatus]
    availability_date: NotRequired[str | None]
    availability_source: NotRequired[TrackingAvailabilitySource | None]

    # TV
    tmdb_series_id: NotRequired[int]
    episode_cursor: NotRequired[TrackingEpisodeRef | None]
    pending_episode: NotRequired[TrackingEpisodeRef | None]
    pending_episode_title: NotRequired[str | None]
    pending_episode_air_date: NotRequired[str | None]


class TrackingItem(TypedDict, total=False):
    id: str
    chat_id: int
    target_kind: TrackingTargetKind
    schedule_mode: TrackingScheduleMode
    target_identity: str
    display_title: str
    status: TrackingStatus
    next_check_at_utc: NotRequired[str | None]
    last_checked_at_utc: NotRequired[str | None]
    created_at_utc: str
    fulfilled_at_utc: NotRequired[str | None]
    linked_download_message_id: NotRequired[int | None]
    target_payload: TrackingTargetPayload
    retry: TrackingRetryState

    # Compatibility fields retained for existing read paths/logging/tests.
    title: NotRequired[str]
    canonical_title: NotRequired[str]
    year: NotRequired[int | None]
    release_date_status: NotRequired[TrackingReleaseDateStatus]
    availability_date: NotRequired[str | None]
    availability_source: NotRequired[TrackingAvailabilitySource | None]
    fulfillment_state: NotRequired[TrackingFulfillmentState]


class TrackingStateFile(TypedDict):
    version: int
    items: dict[str, TrackingItem]


class PostProcessingResult(TypedDict, total=False):
    succeeded: bool
    final_message: str
    destination_path: NotRequired[str | None]
    media_type: NotRequired[Literal["movie", "tv"] | None]
    title: NotRequired[str | None]
    year: NotRequired[int | None]
