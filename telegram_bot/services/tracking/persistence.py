from __future__ import annotations

import json
import os
import shutil
from typing import Any

from telegram_bot.config import logger
from telegram_bot.domain.types import (
    TrackingAvailabilitySource,
    TrackingEpisodeRef,
    TrackingItem,
    TrackingReleaseDateStatus,
    TrackingRetryState,
    TrackingStateFile,
    TrackingTargetKind,
    TrackingTargetPayload,
)

TRACKING_STATE_VERSION = 2
_LEGACY_TRACKING_STATE_VERSION = 1
_FILES_PENDING_V1_BACKUP: set[str] = set()

_V1_TO_V2_STATUS_MAP = {
    "pending_date": "awaiting_metadata",
    "waiting_release_window": "awaiting_window",
    "watching_release": "searching",
    "waiting_fulfillment": "waiting_fulfillment",
    "fulfilled": "fulfilled",
    "cancelled": "cancelled",
}
_VALID_V2_STATUSES = {
    "awaiting_metadata",
    "awaiting_window",
    "searching",
    "waiting_fulfillment",
    "fulfilled",
    "cancelled",
}


def _coerce_int(value: Any, *, minimum: int | None = None) -> int | None:
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip().isdigit():
        result = int(value.strip())
    else:
        return None
    if minimum is not None and result < minimum:
        return None
    return result


def _coerce_optional_iso(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _coerce_display_title(raw_item: dict[str, Any]) -> str:
    for key in ("display_title", "title", "canonical_title"):
        value = raw_item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Unknown"


def _coerce_target_kind(raw_item: dict[str, Any]) -> TrackingTargetKind:
    value = str(raw_item.get("target_kind") or "").strip().lower()
    if value == "tv":
        return "tv"
    return "movie"


def _coerce_schedule_mode(raw_item: dict[str, Any], *, target_kind: TrackingTargetKind) -> str:
    raw_mode = str(raw_item.get("schedule_mode") or "").strip().lower()
    if raw_mode in {"future_release", "ongoing_next_episode"}:
        return raw_mode
    return "future_release" if target_kind == "movie" else "ongoing_next_episode"


def _coerce_status(raw_status: Any) -> str:
    value = str(raw_status or "").strip()
    if value in _VALID_V2_STATUSES:
        return value
    mapped = _V1_TO_V2_STATUS_MAP.get(value)
    if mapped is not None:
        return mapped
    return "awaiting_metadata"


def _coerce_release_date_status(raw_value: Any) -> TrackingReleaseDateStatus:
    return "confirmed" if raw_value == "confirmed" else "unknown"


def _coerce_availability_source(raw_value: Any) -> TrackingAvailabilitySource | None:
    source = str(raw_value).strip().lower() if isinstance(raw_value, str) else ""
    if source == "streaming":
        return "streaming"
    if source == "physical":
        return "physical"
    return None


def _normalize_retry(raw_retry: Any) -> TrackingRetryState:
    if not isinstance(raw_retry, dict):
        return {"consecutive_failures": 0, "last_error": None}
    failures = _coerce_int(raw_retry.get("consecutive_failures"), minimum=0) or 0
    last_error_value = raw_retry.get("last_error")
    if last_error_value is None:
        last_error = None
    elif isinstance(last_error_value, str):
        last_error = last_error_value
    else:
        last_error = str(last_error_value)
    return {
        "consecutive_failures": failures,
        "last_error": last_error,
    }


def _normalize_episode_ref(raw_ref: Any) -> TrackingEpisodeRef | None:
    if not isinstance(raw_ref, dict):
        return None
    season = _coerce_int(raw_ref.get("season"), minimum=1)
    episode = _coerce_int(raw_ref.get("episode"), minimum=1)
    if season is None or episode is None:
        return None
    return {"season": season, "episode": episode}


def _build_movie_identity(canonical_title: str, year: int | None) -> str:
    normalized = canonical_title.strip().casefold() or "unknown"
    if isinstance(year, int):
        return f"movie:{normalized}:{year}"
    return f"movie:{normalized}"


def _build_tv_identity(canonical_title: str, tmdb_series_id: int | None) -> str:
    if isinstance(tmdb_series_id, int):
        return f"tv:tmdb:{tmdb_series_id}"
    normalized = canonical_title.strip().casefold() or "unknown"
    return f"tv:{normalized}"


def _coerce_movie_payload(raw_payload: Any, raw_item: dict[str, Any]) -> TrackingTargetPayload:
    payload: TrackingTargetPayload = {}
    raw_payload_dict = raw_payload if isinstance(raw_payload, dict) else {}

    canonical_title = raw_payload_dict.get("canonical_title")
    if not isinstance(canonical_title, str) or not canonical_title.strip():
        fallback = str(raw_item.get("canonical_title") or raw_item.get("title") or "").strip()
        payload["canonical_title"] = fallback or _coerce_display_title(raw_item)
    else:
        payload["canonical_title"] = canonical_title.strip()

    raw_year = raw_payload_dict.get("year")
    if raw_year is None:
        raw_year = raw_item.get("year")
    year = _coerce_int(raw_year, minimum=1)
    payload["year"] = year

    raw_release_status = raw_payload_dict.get("release_date_status")
    if raw_release_status != "confirmed":
        raw_release_status = raw_item.get("release_date_status")
    payload["release_date_status"] = _coerce_release_date_status(raw_release_status)

    availability_date = _coerce_optional_iso(
        raw_payload_dict.get("availability_date", raw_item.get("availability_date"))
    )
    payload["availability_date"] = availability_date

    raw_source = raw_payload_dict.get("availability_source", raw_item.get("availability_source"))
    payload["availability_source"] = _coerce_availability_source(raw_source)

    raw_collection_name = raw_payload_dict.get("collection_name")
    if isinstance(raw_collection_name, str) and raw_collection_name.strip():
        payload["collection_name"] = raw_collection_name.strip()
    else:
        payload["collection_name"] = None

    raw_collection_fs_name = raw_payload_dict.get("collection_fs_name")
    if isinstance(raw_collection_fs_name, str) and raw_collection_fs_name.strip():
        payload["collection_fs_name"] = raw_collection_fs_name.strip()
    else:
        payload["collection_fs_name"] = None
    return payload


def _coerce_tv_payload(raw_payload: Any, raw_item: dict[str, Any]) -> TrackingTargetPayload:
    payload: TrackingTargetPayload = {}
    raw_payload_dict = raw_payload if isinstance(raw_payload, dict) else {}

    canonical_title = raw_payload_dict.get("canonical_title")
    if not isinstance(canonical_title, str) or not canonical_title.strip():
        fallback = str(raw_item.get("canonical_title") or raw_item.get("title") or "").strip()
        payload["canonical_title"] = fallback or _coerce_display_title(raw_item)
    else:
        payload["canonical_title"] = canonical_title.strip()

    tmdb_series_id = _coerce_int(
        raw_payload_dict.get("tmdb_series_id", raw_item.get("tmdb_series_id")),
        minimum=1,
    )
    if tmdb_series_id is not None:
        payload["tmdb_series_id"] = tmdb_series_id

    episode_cursor = _normalize_episode_ref(raw_payload_dict.get("episode_cursor"))
    payload["episode_cursor"] = episode_cursor
    pending_episode = _normalize_episode_ref(raw_payload_dict.get("pending_episode"))
    payload["pending_episode"] = pending_episode

    pending_episode_title = raw_payload_dict.get("pending_episode_title")
    payload["pending_episode_title"] = (
        pending_episode_title.strip()
        if isinstance(pending_episode_title, str) and pending_episode_title.strip()
        else None
    )

    pending_episode_air_date = raw_payload_dict.get("pending_episode_air_date")
    payload["pending_episode_air_date"] = (
        pending_episode_air_date.strip()
        if isinstance(pending_episode_air_date, str) and pending_episode_air_date.strip()
        else None
    )
    return payload


def _hydrate_compatibility_fields(item: TrackingItem) -> None:
    raw_payload = item.get("target_payload")
    payload: TrackingTargetPayload = raw_payload if isinstance(raw_payload, dict) else {}
    canonical_title = str(
        payload.get("canonical_title")
        or item.get("display_title")
        or item.get("title")
        or "Unknown"
    ).strip()
    item["canonical_title"] = canonical_title
    item["title"] = str(item.get("display_title") or canonical_title).strip() or canonical_title
    item["fulfillment_state"] = "fulfilled" if item.get("status") == "fulfilled" else "pending"  # type: ignore[typeddict-item]

    if item.get("target_kind") == "movie":
        year = _coerce_int(payload.get("year"), minimum=1)
        item["year"] = year
        item["release_date_status"] = _coerce_release_date_status(
            payload.get("release_date_status")
        )
        item["availability_date"] = _coerce_optional_iso(payload.get("availability_date"))
        raw_source = payload.get("availability_source")
        item["availability_source"] = _coerce_availability_source(raw_source)
    else:
        item["year"] = None
        item["release_date_status"] = "unknown"
        item["availability_date"] = None
        item["availability_source"] = None


def _normalize_tracking_item_v1(item_id: str, raw_item: Any) -> TrackingItem | None:
    if not isinstance(raw_item, dict):
        return None

    canonical_title = str(raw_item.get("canonical_title") or raw_item.get("title") or "").strip()
    created_at_utc = str(raw_item.get("created_at_utc") or "").strip()
    if not canonical_title or not created_at_utc:
        return None

    year = _coerce_int(raw_item.get("year"), minimum=1)
    payload = _coerce_movie_payload(
        {
            "canonical_title": canonical_title,
            "year": year,
            "release_date_status": raw_item.get("release_date_status"),
            "availability_date": raw_item.get("availability_date"),
            "availability_source": raw_item.get("availability_source"),
        },
        raw_item,
    )
    target_identity = str(raw_item.get("target_identity") or "").strip() or _build_movie_identity(
        canonical_title, year
    )

    normalized: TrackingItem = {
        "id": str(raw_item.get("id") or item_id),
        "chat_id": _coerce_int(raw_item.get("chat_id"), minimum=0) or 0,
        "target_kind": "movie",
        "schedule_mode": "future_release",
        "target_identity": target_identity,
        "display_title": _coerce_display_title(raw_item),
        "status": _coerce_status(raw_item.get("status")),  # type: ignore[typeddict-item]
        "next_check_at_utc": _coerce_optional_iso(raw_item.get("next_check_at_utc")),
        "last_checked_at_utc": _coerce_optional_iso(raw_item.get("last_checked_at_utc")),
        "created_at_utc": created_at_utc,
        "fulfilled_at_utc": _coerce_optional_iso(raw_item.get("fulfilled_at_utc")),
        "linked_download_message_id": _coerce_int(
            raw_item.get("linked_download_message_id"),
            minimum=0,
        ),
        "target_payload": payload,
        "retry": _normalize_retry(raw_item.get("retry")),
    }
    _hydrate_compatibility_fields(normalized)
    if not normalized.get("id"):
        return None
    return normalized


def _normalize_tracking_item_v2(item_id: str, raw_item: Any) -> TrackingItem | None:
    if not isinstance(raw_item, dict):
        return None

    target_kind = _coerce_target_kind(raw_item)
    schedule_mode = _coerce_schedule_mode(raw_item, target_kind=target_kind)
    payload = (
        _coerce_movie_payload(raw_item.get("target_payload"), raw_item)
        if target_kind == "movie"
        else _coerce_tv_payload(raw_item.get("target_payload"), raw_item)
    )
    canonical_title = str(payload.get("canonical_title") or _coerce_display_title(raw_item)).strip()
    if not canonical_title:
        return None

    raw_identity = str(raw_item.get("target_identity") or "").strip()
    if raw_identity:
        target_identity = raw_identity
    elif target_kind == "movie":
        target_identity = _build_movie_identity(canonical_title, _coerce_int(payload.get("year")))
    else:
        target_identity = _build_tv_identity(
            canonical_title, _coerce_int(payload.get("tmdb_series_id"))
        )

    created_at_utc = str(raw_item.get("created_at_utc") or "").strip()
    if not created_at_utc:
        return None

    normalized: TrackingItem = {
        "id": str(raw_item.get("id") or item_id),
        "chat_id": _coerce_int(raw_item.get("chat_id"), minimum=0) or 0,
        "target_kind": target_kind,
        "schedule_mode": schedule_mode,  # type: ignore[typeddict-item]
        "target_identity": target_identity,
        "display_title": _coerce_display_title(raw_item),
        "status": _coerce_status(raw_item.get("status")),  # type: ignore[typeddict-item]
        "next_check_at_utc": _coerce_optional_iso(raw_item.get("next_check_at_utc")),
        "last_checked_at_utc": _coerce_optional_iso(raw_item.get("last_checked_at_utc")),
        "created_at_utc": created_at_utc,
        "fulfilled_at_utc": _coerce_optional_iso(raw_item.get("fulfilled_at_utc")),
        "linked_download_message_id": _coerce_int(
            raw_item.get("linked_download_message_id"),
            minimum=0,
        ),
        "target_payload": payload,
        "retry": _normalize_retry(raw_item.get("retry")),
    }
    _hydrate_compatibility_fields(normalized)
    if not normalized.get("id"):
        return None
    return normalized


def _derive_v1_backup_path(file_path: str) -> str:
    base, ext = os.path.splitext(file_path)
    if ext:
        return f"{base}.v1.bak"
    return f"{file_path}.v1.bak"


def _ensure_v1_backup_if_needed(file_path: str) -> None:
    absolute_path = os.path.abspath(file_path)
    if absolute_path not in _FILES_PENDING_V1_BACKUP:
        return
    if not os.path.exists(file_path):
        _FILES_PENDING_V1_BACKUP.discard(absolute_path)
        return

    backup_path = _derive_v1_backup_path(file_path)
    if not os.path.exists(backup_path):
        try:
            shutil.copyfile(file_path, backup_path)
            logger.info("Created v1 tracking-state backup at '%s'.", backup_path)
        except OSError as exc:
            logger.error("Could not create v1 tracking-state backup '%s': %s", backup_path, exc)
    _FILES_PENDING_V1_BACKUP.discard(absolute_path)


def load_tracking_state(file_path: str) -> dict[str, TrackingItem]:
    """Loads durable tracking items from disk with backward-compatible migration."""
    if not os.path.exists(file_path):
        logger.info("Tracking state file '%s' not found. Starting with no schedules.", file_path)
        return {}

    try:
        with open(file_path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Could not read tracking state '%s': %s", file_path, exc)
        return {}

    if not isinstance(payload, dict):
        return {}

    raw_items = payload.get("items")
    if not isinstance(raw_items, dict):
        return {}

    raw_version = _coerce_int(payload.get("version"), minimum=1)
    version = raw_version if raw_version is not None else _LEGACY_TRACKING_STATE_VERSION

    items: dict[str, TrackingItem] = {}
    skipped_count = 0
    for item_id, raw_item in raw_items.items():
        normalized = (
            _normalize_tracking_item_v1(str(item_id), raw_item)
            if version <= _LEGACY_TRACKING_STATE_VERSION
            else _normalize_tracking_item_v2(str(item_id), raw_item)
        )
        if normalized is None:
            skipped_count += 1
            continue
        items[normalized["id"]] = normalized

    if version <= _LEGACY_TRACKING_STATE_VERSION:
        _FILES_PENDING_V1_BACKUP.add(os.path.abspath(file_path))
        logger.info(
            "Loaded %d tracking item(s) from v1 state '%s' (skipped=%d).",
            len(items),
            file_path,
            skipped_count,
        )
    else:
        logger.info(
            "Loaded %d tracking item(s) from '%s' (version=%d, skipped=%d).",
            len(items),
            file_path,
            version,
            skipped_count,
        )
    return items


def save_tracking_state(file_path: str, items: dict[str, TrackingItem]) -> None:
    """Persists tracking state to disk using the v2 schema."""
    _ensure_v1_backup_if_needed(file_path)

    serializable_items: dict[str, TrackingItem] = {}
    for item_id, item in items.items():
        normalized = _normalize_tracking_item_v2(str(item_id), item)
        if normalized is None:
            continue
        serializable_items[str(normalized["id"])] = normalized

    payload: TrackingStateFile = {
        "version": TRACKING_STATE_VERSION,
        "items": serializable_items,
    }

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
        logger.info("Saved %d tracking item(s) to '%s'.", len(serializable_items), file_path)
    except OSError as exc:
        logger.error("Could not save tracking state '%s': %s", file_path, exc)
