# telegram_bot/domain/types.py

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

__all__ = [
    "SourceDict",
    "DownloadData",
    "BatchCollectionMeta",
    "BatchMeta",
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
