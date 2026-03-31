from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4

from telegram.ext import Application

from telegram_bot.config import TRACKING_STATE_FILE, logger
from telegram_bot.domain.types import (
    TrackingAvailabilitySource,
    TrackingEpisodeRef,
    TrackingItem,
    TrackingReleaseDateStatus,
    TrackingRetryState,
    TrackingScheduleMode,
    TrackingStatus,
    TrackingTargetKind,
    TrackingTargetPayload,
)

from .persistence import load_tracking_state, save_tracking_state

TRACKING_ITEMS_KEY = "tracking_items"
TRACKING_IN_PROGRESS_KEY = "tracking_in_progress_ids"
TRACKING_LOOP_TASK_KEY = "tracking_loop_task"
TRACKING_TIMEZONE_KEY = "tracking_timezone"
TRACKING_NOW_PROVIDER_KEY = "tracking_now_provider"

TRACKING_FULFILLMENT_WATCHDOG_HOURS = 6
TERMINAL_TRACKING_STATES = {"fulfilled", "cancelled"}


def utc_now(now_provider: Any | None = None) -> datetime:
    if callable(now_provider):
        value = now_provider()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc_iso(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_tracking_timezone(bot_data: dict[str, Any]) -> timezone | Any:
    configured = bot_data.get(TRACKING_TIMEZONE_KEY)
    if configured is not None:
        return configured

    detected = datetime.now().astimezone().tzinfo
    if detected is None:
        logger.warning("[TRACKING] Could not detect local timezone. Falling back to UTC.")
        return timezone.utc
    return detected


def get_tracking_items(bot_data: dict[str, Any]) -> dict[str, TrackingItem]:
    raw = bot_data.get(TRACKING_ITEMS_KEY)
    if isinstance(raw, dict):
        return raw
    store: dict[str, TrackingItem] = {}
    bot_data[TRACKING_ITEMS_KEY] = store
    return store


def get_tracking_in_progress_ids(bot_data: dict[str, Any]) -> set[str]:
    raw = bot_data.get(TRACKING_IN_PROGRESS_KEY)
    if isinstance(raw, set):
        return raw
    store: set[str] = set()
    bot_data[TRACKING_IN_PROGRESS_KEY] = store
    return store


def _coerce_allowed_user_ids(bot_data: dict[str, Any]) -> set[int] | None:
    raw_allowed = bot_data.get("ALLOWED_USER_IDS")
    if not isinstance(raw_allowed, list) or not raw_allowed:
        return None

    allowed_ids: set[int] = set()
    for raw_value in raw_allowed:
        if isinstance(raw_value, int):
            allowed_ids.add(raw_value)
            continue
        if isinstance(raw_value, str):
            normalized = raw_value.strip()
            if normalized.startswith("-"):
                normalized = normalized[1:]
            if normalized.isdigit():
                allowed_ids.add(int(raw_value.strip()))
    return allowed_ids or None


def _prune_tracking_items_for_allowed_users(
    items: dict[str, TrackingItem],
    *,
    allowed_user_ids: set[int] | None,
) -> int:
    if not allowed_user_ids:
        return 0

    removed_ids: list[str] = []
    for item_id, item in items.items():
        chat_id_raw = item.get("chat_id")
        if isinstance(chat_id_raw, int):
            chat_id = chat_id_raw
        elif isinstance(chat_id_raw, str):
            normalized = chat_id_raw.strip()
            chat_id = int(normalized) if normalized.lstrip("-").isdigit() else None
        else:
            chat_id = None
        if chat_id in allowed_user_ids:
            continue
        removed_ids.append(item_id)

    for item_id in removed_ids:
        items.pop(item_id, None)
    return len(removed_ids)


def _prune_duplicate_active_targets(items: dict[str, TrackingItem]) -> int:
    groups: dict[tuple[int, str, str, str], list[TrackingItem]] = {}
    for item in items.values():
        if item.get("status") in TERMINAL_TRACKING_STATES:
            continue
        chat_id_raw = item.get("chat_id")
        if not isinstance(chat_id_raw, int):
            continue
        target_identity = str(item.get("target_identity") or "").strip()
        if not target_identity:
            continue
        key = (
            chat_id_raw,
            str(item.get("target_kind") or "").strip().lower(),
            str(item.get("schedule_mode") or "").strip().lower(),
            target_identity.casefold(),
        )
        groups.setdefault(key, []).append(item)

    duplicate_ids: list[str] = []
    for grouped_items in groups.values():
        if len(grouped_items) <= 1:
            continue
        grouped_items.sort(
            key=lambda entry: (
                str(entry.get("created_at_utc") or ""),
                str(entry.get("id") or ""),
            )
        )
        duplicate_ids.extend(str(item.get("id") or "") for item in grouped_items[1:])

    for item_id in duplicate_ids:
        if item_id:
            items.pop(item_id, None)
    return len([item_id for item_id in duplicate_ids if item_id])


def load_tracking_state_into_bot_data(
    application: Application,
    *,
    file_path: str = TRACKING_STATE_FILE,
) -> dict[str, TrackingItem]:
    items = load_tracking_state(file_path)
    allowed_user_ids = _coerce_allowed_user_ids(application.bot_data)
    removed_for_user_policy = _prune_tracking_items_for_allowed_users(
        items,
        allowed_user_ids=allowed_user_ids,
    )
    removed_duplicates = _prune_duplicate_active_targets(items)
    if removed_for_user_policy or removed_duplicates:
        save_tracking_state(file_path, items)
        logger.info(
            "[TRACKING] Pruned tracking state entries (unauthorized=%d, duplicates=%d).",
            removed_for_user_policy,
            removed_duplicates,
        )
    application.bot_data[TRACKING_ITEMS_KEY] = items
    application.bot_data.setdefault(TRACKING_IN_PROGRESS_KEY, set())
    return items


def persist_tracking_state_from_bot_data(
    application: Application,
    *,
    file_path: str = TRACKING_STATE_FILE,
) -> None:
    save_tracking_state(file_path, get_tracking_items(application.bot_data))


def _next_tracking_item_id(items: dict[str, TrackingItem]) -> str:
    candidate = f"trk_{uuid4().hex[:8]}"
    while candidate in items:
        candidate = f"trk_{uuid4().hex[:8]}"
    return candidate


def calculate_release_day_first_check_utc(
    availability_day: date,
    *,
    local_timezone: timezone | Any,
    now_utc: datetime,
) -> datetime:
    """First permitted search time for a release window: local noon on release day."""
    noon_local = datetime.combine(availability_day, time(hour=12), tzinfo=local_timezone)
    noon_utc = noon_local.astimezone(timezone.utc)

    now_local = now_utc.astimezone(local_timezone)
    if now_local.date() < availability_day:
        return noon_utc
    if now_local.date() == availability_day and now_local < noon_local:
        return noon_utc
    return now_utc


def calculate_next_hourly_check(now_utc: datetime) -> datetime:
    rounded = now_utc.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return rounded + timedelta(hours=1)


def calculate_next_weekly_metadata_check(now_utc: datetime) -> datetime:
    return now_utc.astimezone(timezone.utc) + timedelta(days=7)


def calculate_fulfillment_watchdog_check(now_utc: datetime) -> datetime:
    return now_utc.astimezone(timezone.utc) + timedelta(hours=TRACKING_FULFILLMENT_WATCHDOG_HOURS)


def _normalize_display_title(title: str | None, fallback: str) -> str:
    candidate = (title or "").strip()
    if candidate:
        return candidate
    normalized_fallback = (fallback or "").strip()
    return normalized_fallback or "Unknown"


def _coerce_episode_ref(value: Any) -> TrackingEpisodeRef | None:
    if not isinstance(value, dict):
        return None
    season = value.get("season")
    episode = value.get("episode")
    if isinstance(season, int) and isinstance(episode, int) and season > 0 and episode > 0:
        return {"season": season, "episode": episode}
    return None


def _coerce_release_date_status(value: Any) -> TrackingReleaseDateStatus:
    return "confirmed" if value == "confirmed" else "unknown"


def _coerce_availability_source(value: Any) -> TrackingAvailabilitySource | None:
    source = str(value).strip().lower() if isinstance(value, str) else ""
    if source == "streaming":
        return "streaming"
    if source == "physical":
        return "physical"
    return None


def _clone_target_payload(source: TrackingTargetPayload) -> TrackingTargetPayload:
    cloned: TrackingTargetPayload = {}
    cloned.update(source)
    return cloned


def _ensure_target_payload(item: TrackingItem) -> TrackingTargetPayload:
    payload = item.get("target_payload")
    if isinstance(payload, dict):
        return payload
    new_payload: TrackingTargetPayload = {}
    item["target_payload"] = new_payload
    return new_payload


def _ensure_retry(item: TrackingItem) -> TrackingRetryState:
    raw_retry = item.get("retry")
    if isinstance(raw_retry, dict):
        consecutive = raw_retry.get("consecutive_failures")
        if not isinstance(consecutive, int) or consecutive < 0:
            raw_retry["consecutive_failures"] = 0
        if "last_error" not in raw_retry:
            raw_retry["last_error"] = None
        return raw_retry

    retry: TrackingRetryState = {
        "consecutive_failures": 0,
        "last_error": None,
    }
    item["retry"] = retry
    return retry


def _movie_title_and_year(item: TrackingItem) -> tuple[str, int | None]:
    payload = _ensure_target_payload(item)
    canonical_title = str(
        payload.get("canonical_title")
        or item.get("canonical_title")
        or item.get("display_title")
        or ""
    ).strip()
    if not canonical_title:
        canonical_title = "Unknown"
    raw_year = payload.get("year", item.get("year"))
    year = raw_year if isinstance(raw_year, int) else None
    return canonical_title, year


def _sync_movie_compatibility_fields(item: TrackingItem) -> None:
    payload = _ensure_target_payload(item)
    canonical_title, year = _movie_title_and_year(item)
    item["canonical_title"] = canonical_title
    item["title"] = str(item.get("display_title") or canonical_title)
    item["year"] = year
    item["release_date_status"] = _coerce_release_date_status(payload.get("release_date_status"))
    item["availability_date"] = payload.get("availability_date")
    item["availability_source"] = _coerce_availability_source(payload.get("availability_source"))
    item["fulfillment_state"] = "fulfilled" if item.get("status") == "fulfilled" else "pending"  # type: ignore[typeddict-item]


def _sync_tv_compatibility_fields(item: TrackingItem) -> None:
    payload = _ensure_target_payload(item)
    canonical_title = str(
        payload.get("canonical_title")
        or item.get("display_title")
        or item.get("title")
        or "Unknown"
    ).strip()
    item["canonical_title"] = canonical_title or "Unknown"
    item["title"] = str(item.get("display_title") or canonical_title or "Unknown")
    item["year"] = None
    item["release_date_status"] = "unknown"
    item["availability_date"] = None
    item["availability_source"] = None
    item["fulfillment_state"] = "fulfilled" if item.get("status") == "fulfilled" else "pending"  # type: ignore[typeddict-item]


def get_tracking_target_kind(item: TrackingItem) -> TrackingTargetKind:
    raw_kind = str(item.get("target_kind") or "").strip().lower()
    return "tv" if raw_kind == "tv" else "movie"


def get_tracking_schedule_mode(item: TrackingItem) -> str:
    raw_mode = str(item.get("schedule_mode") or "").strip().lower()
    if raw_mode in {"future_release", "ongoing_next_episode"}:
        return raw_mode
    return "future_release" if get_tracking_target_kind(item) == "movie" else "ongoing_next_episode"


def get_tracking_display_title(item: TrackingItem) -> str:
    value = item.get("display_title")
    if isinstance(value, str) and value.strip():
        return value.strip()
    canonical = str(item.get("canonical_title") or item.get("title") or "").strip()
    return canonical or "Unknown"


def _find_existing_active_tracking_item(
    items: dict[str, TrackingItem],
    *,
    chat_id: int,
    target_kind: TrackingTargetKind,
    schedule_mode: TrackingScheduleMode,
    target_identity: str,
) -> TrackingItem | None:
    identity_key = target_identity.strip().casefold()
    if not identity_key:
        return None

    def _coerce_chat_id(raw_value: Any) -> int | None:
        if isinstance(raw_value, int):
            return raw_value
        if isinstance(raw_value, str):
            normalized = raw_value.strip()
            if normalized.lstrip("-").isdigit():
                return int(normalized)
        return None

    candidates = [
        item
        for item in items.values()
        if _coerce_chat_id(item.get("chat_id")) == int(chat_id)
        and str(item.get("target_identity") or "").strip().casefold() == identity_key
        and get_tracking_target_kind(item) == target_kind
        and get_tracking_schedule_mode(item) == schedule_mode
        and item.get("status") not in TERMINAL_TRACKING_STATES
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda entry: (
            str(entry.get("created_at_utc") or ""),
            str(entry.get("id") or ""),
        )
    )
    return candidates[0]


def create_tracking_item(
    application: Application,
    *,
    chat_id: int,
    target_kind: TrackingTargetKind,
    schedule_mode: TrackingScheduleMode,
    target_identity: str,
    display_title: str,
    target_payload: TrackingTargetPayload,
    status: TrackingStatus,
    next_check_at_utc: datetime | None,
    now_utc: datetime | None = None,
) -> TrackingItem:
    """Creates and persists a generic tracking item."""
    items = get_tracking_items(application.bot_data)
    existing_item = _find_existing_active_tracking_item(
        items,
        chat_id=int(chat_id),
        target_kind=target_kind,
        schedule_mode=schedule_mode,
        target_identity=target_identity,
    )
    if existing_item is not None:
        logger.info(
            "[TRACKING] Reusing existing %s item %s for '%s'.",
            target_kind,
            str(existing_item.get("id") or "(unknown)"),
            get_tracking_display_title(existing_item),
        )
        return existing_item

    now = now_utc or utc_now(application.bot_data.get(TRACKING_NOW_PROVIDER_KEY))
    item_id = _next_tracking_item_id(items)
    initial_retry: TrackingRetryState = {"consecutive_failures": 0, "last_error": None}

    item: TrackingItem = {
        "id": item_id,
        "chat_id": int(chat_id),
        "target_kind": target_kind,
        "schedule_mode": schedule_mode,
        "target_identity": target_identity,
        "display_title": _normalize_display_title(
            display_title, str(target_payload.get("canonical_title"))
        ),
        "status": status,
        "next_check_at_utc": isoformat_utc(next_check_at_utc) if next_check_at_utc else None,
        "last_checked_at_utc": None,
        "created_at_utc": isoformat_utc(now),
        "fulfilled_at_utc": None,
        "linked_download_message_id": None,
        "target_payload": _clone_target_payload(target_payload),
        "retry": initial_retry,
    }

    if target_kind == "movie":
        _sync_movie_compatibility_fields(item)
    else:
        _sync_tv_compatibility_fields(item)

    items[item_id] = item
    persist_tracking_state_from_bot_data(application)
    logger.info(
        "[TRACKING] Created %s item %s for '%s' (status=%s).",
        target_kind,
        item_id,
        get_tracking_display_title(item),
        status,
    )
    return item


def create_movie_tracking_item(
    application: Application,
    *,
    chat_id: int,
    canonical_title: str,
    year: int | None,
    availability_date: date | None,
    availability_source: str | None,
    collection_name: str | None = None,
    collection_fs_name: str | None = None,
    title: str | None = None,
    now_utc: datetime | None = None,
) -> TrackingItem:
    """Backwards-compatible movie-specific creator on top of the generic schema."""
    now = now_utc or utc_now(application.bot_data.get(TRACKING_NOW_PROVIDER_KEY))
    normalized_title = canonical_title.strip() or (title or "").strip() or "Unknown"

    if isinstance(availability_date, date):
        first_check = calculate_release_day_first_check_utc(
            availability_date,
            local_timezone=get_tracking_timezone(application.bot_data),
            now_utc=now,
        )
        status: TrackingStatus = "searching" if first_check <= now else "awaiting_window"
        next_check = now if status == "searching" else first_check
        release_status: TrackingReleaseDateStatus = "confirmed"
        availability_iso = availability_date.isoformat()
        source = _coerce_availability_source(availability_source)
    else:
        status = "awaiting_metadata"
        next_check = calculate_next_weekly_metadata_check(now)
        release_status = "unknown"
        availability_iso = None
        source = None

    identity = (
        f"movie:{normalized_title.casefold()}:{year}"
        if isinstance(year, int)
        else f"movie:{normalized_title.casefold()}"
    )
    payload: TrackingTargetPayload = {
        "canonical_title": normalized_title,
        "year": year,
        "release_date_status": release_status,
        "availability_date": availability_iso,
        "availability_source": source,
    }
    if isinstance(collection_name, str) and collection_name.strip():
        payload["collection_name"] = collection_name.strip()
    if isinstance(collection_fs_name, str) and collection_fs_name.strip():
        payload["collection_fs_name"] = collection_fs_name.strip()
    return create_tracking_item(
        application,
        chat_id=chat_id,
        target_kind="movie",
        schedule_mode="future_release",
        target_identity=identity,
        display_title=title or normalized_title,
        target_payload=payload,
        status=status,
        next_check_at_utc=next_check,
        now_utc=now,
    )


def create_tv_tracking_item(
    application: Application,
    *,
    chat_id: int,
    canonical_title: str,
    tmdb_series_id: int,
    title: str | None = None,
    episode_cursor: dict[str, int] | None = None,
    next_air_date: date | None = None,
    now_utc: datetime | None = None,
) -> TrackingItem:
    now = now_utc or utc_now(application.bot_data.get(TRACKING_NOW_PROVIDER_KEY))
    normalized_title = canonical_title.strip() or (title or "").strip() or "Unknown"

    payload: TrackingTargetPayload = {
        "canonical_title": normalized_title,
        "tmdb_series_id": int(tmdb_series_id),
        "episode_cursor": _coerce_episode_ref(episode_cursor),
        "pending_episode": None,
        "pending_episode_title": None,
        "pending_episode_air_date": next_air_date.isoformat()
        if isinstance(next_air_date, date)
        else None,
    }
    return create_tracking_item(
        application,
        chat_id=chat_id,
        target_kind="tv",
        schedule_mode="ongoing_next_episode",
        target_identity=f"tv:tmdb:{int(tmdb_series_id)}",
        display_title=title or normalized_title,
        target_payload=payload,
        status="awaiting_metadata",
        next_check_at_utc=now,
        now_utc=now,
    )


def get_tracking_item(application: Application, item_id: str) -> TrackingItem | None:
    return get_tracking_items(application.bot_data).get(item_id)


def list_tracking_items(
    application: Application,
    *,
    chat_id: int | None = None,
    include_terminal: bool = False,
) -> list[TrackingItem]:
    items = list(get_tracking_items(application.bot_data).values())
    if chat_id is not None:
        items = [item for item in items if item.get("chat_id") == int(chat_id)]
    if not include_terminal:
        items = [item for item in items if item.get("status") not in TERMINAL_TRACKING_STATES]
    return sorted(items, key=lambda item: str(item.get("created_at_utc") or ""), reverse=True)


def cancel_tracking_item(
    application: Application,
    *,
    item_id: str,
    chat_id: int | None = None,
    now_utc: datetime | None = None,
) -> bool:
    items = get_tracking_items(application.bot_data)
    item = items.get(item_id)
    if not item:
        return False
    if chat_id is not None and int(item.get("chat_id", 0) or 0) != int(chat_id):
        return False
    if item.get("status") in TERMINAL_TRACKING_STATES:
        return False

    items.pop(item_id, None)
    get_tracking_in_progress_ids(application.bot_data).discard(item_id)
    persist_tracking_state_from_bot_data(application)
    logger.info("[TRACKING] Cancelled and removed item %s.", item_id)
    return True


def mark_tracking_waiting_fulfillment(
    application: Application,
    *,
    item_id: str,
    linked_message_id: int | None = None,
    pending_episode: dict[str, int] | None = None,
    pending_episode_title: str | None = None,
    now_utc: datetime | None = None,
) -> bool:
    item = get_tracking_item(application, item_id)
    if not item or item.get("status") in TERMINAL_TRACKING_STATES:
        return False

    now = now_utc or utc_now(application.bot_data.get(TRACKING_NOW_PROVIDER_KEY))
    item["status"] = "waiting_fulfillment"  # type: ignore[typeddict-item]
    item["next_check_at_utc"] = isoformat_utc(calculate_fulfillment_watchdog_check(now))
    item["last_checked_at_utc"] = isoformat_utc(now)
    if linked_message_id is not None:
        item["linked_download_message_id"] = int(linked_message_id)

    payload = _ensure_target_payload(item)
    if pending_episode is not None:
        payload["pending_episode"] = _coerce_episode_ref(pending_episode)
    if pending_episode_title is None:
        payload["pending_episode_title"] = None
    elif isinstance(pending_episode_title, str):
        payload["pending_episode_title"] = pending_episode_title.strip() or None
    _ensure_retry(item)["last_error"] = None

    if get_tracking_target_kind(item) == "movie":
        _sync_movie_compatibility_fields(item)
    else:
        _sync_tv_compatibility_fields(item)
    persist_tracking_state_from_bot_data(application)
    logger.info("[TRACKING] Item %s is now waiting for fulfillment.", item_id)
    return True


def mark_tracking_hourly_retry(
    application: Application,
    *,
    item_id: str,
    error_message: str | None = None,
    now_utc: datetime | None = None,
) -> bool:
    item = get_tracking_item(application, item_id)
    if not item or item.get("status") in TERMINAL_TRACKING_STATES:
        return False

    now = now_utc or utc_now(application.bot_data.get(TRACKING_NOW_PROVIDER_KEY))
    item["status"] = "searching"  # type: ignore[typeddict-item]
    item["next_check_at_utc"] = isoformat_utc(calculate_next_hourly_check(now))
    item["last_checked_at_utc"] = isoformat_utc(now)
    retry = _ensure_retry(item)
    retry["consecutive_failures"] = int(retry.get("consecutive_failures") or 0) + 1
    retry["last_error"] = error_message

    if get_tracking_target_kind(item) == "movie":
        _sync_movie_compatibility_fields(item)
    else:
        _sync_tv_compatibility_fields(item)
    persist_tracking_state_from_bot_data(application)
    logger.info("[TRACKING] Item %s scheduled for next hourly check.", item_id)
    return True


def mark_tracking_weekly_metadata_retry(
    application: Application,
    *,
    item_id: str,
    error_message: str | None = None,
    now_utc: datetime | None = None,
) -> bool:
    item = get_tracking_item(application, item_id)
    if not item or item.get("status") in TERMINAL_TRACKING_STATES:
        return False

    now = now_utc or utc_now(application.bot_data.get(TRACKING_NOW_PROVIDER_KEY))
    item["status"] = "awaiting_metadata"  # type: ignore[typeddict-item]
    item["next_check_at_utc"] = isoformat_utc(calculate_next_weekly_metadata_check(now))
    item["last_checked_at_utc"] = isoformat_utc(now)
    retry = _ensure_retry(item)
    retry["consecutive_failures"] = int(retry.get("consecutive_failures") or 0) + 1
    retry["last_error"] = error_message

    if get_tracking_target_kind(item) == "movie":
        payload = _ensure_target_payload(item)
        payload["release_date_status"] = "unknown"
        payload["availability_date"] = None
        payload["availability_source"] = None
        _sync_movie_compatibility_fields(item)
    else:
        _sync_tv_compatibility_fields(item)
    persist_tracking_state_from_bot_data(application)
    logger.info("[TRACKING] Item %s remains in metadata-only mode.", item_id)
    return True


def set_tracking_release_window(
    application: Application,
    *,
    item_id: str,
    availability_date: date,
    availability_source: str | None,
    now_utc: datetime | None = None,
) -> bool:
    item = get_tracking_item(application, item_id)
    if not item or item.get("status") in TERMINAL_TRACKING_STATES:
        return False

    now = now_utc or utc_now(application.bot_data.get(TRACKING_NOW_PROVIDER_KEY))
    first_check = calculate_release_day_first_check_utc(
        availability_date,
        local_timezone=get_tracking_timezone(application.bot_data),
        now_utc=now,
    )
    if first_check <= now:
        status = "searching"
        next_check = now
    else:
        status = "awaiting_window"
        next_check = first_check

    payload = _ensure_target_payload(item)
    payload["release_date_status"] = "confirmed"
    payload["availability_date"] = availability_date.isoformat()
    payload["availability_source"] = _coerce_availability_source(availability_source)

    item["status"] = status  # type: ignore[typeddict-item]
    item["next_check_at_utc"] = isoformat_utc(next_check)
    item["last_checked_at_utc"] = isoformat_utc(now)
    _ensure_retry(item)["last_error"] = None
    _sync_movie_compatibility_fields(item)

    persist_tracking_state_from_bot_data(application)
    logger.info(
        "[TRACKING] Item %s release window updated to %s (%s).",
        item_id,
        status,
        item["next_check_at_utc"],
    )
    return True


def mark_tracking_fulfilled(
    application: Application,
    *,
    item_id: str,
    now_utc: datetime | None = None,
) -> bool:
    item = get_tracking_item(application, item_id)
    if not item or item.get("status") in TERMINAL_TRACKING_STATES:
        return False

    now = now_utc or utc_now(application.bot_data.get(TRACKING_NOW_PROVIDER_KEY))
    item["status"] = "fulfilled"  # type: ignore[typeddict-item]
    item["fulfillment_state"] = "fulfilled"  # type: ignore[typeddict-item]
    item["fulfilled_at_utc"] = isoformat_utc(now)
    item["last_checked_at_utc"] = isoformat_utc(now)
    item["next_check_at_utc"] = None
    payload = _ensure_target_payload(item)
    payload["pending_episode"] = None
    payload["pending_episode_title"] = None
    payload["pending_episode_air_date"] = None
    _ensure_retry(item)["last_error"] = None

    if get_tracking_target_kind(item) == "movie":
        _sync_movie_compatibility_fields(item)
    else:
        _sync_tv_compatibility_fields(item)
    persist_tracking_state_from_bot_data(application)
    logger.info("[TRACKING] Item %s fulfilled.", item_id)
    return True


def _extract_episode_ref_from_parsed_info(
    parsed_info: dict[str, Any] | None,
) -> TrackingEpisodeRef | None:
    if not isinstance(parsed_info, dict):
        return None
    return _coerce_episode_ref(parsed_info)


def mark_tracking_fulfillment_success(
    application: Application,
    *,
    item_id: str,
    parsed_info: dict[str, Any] | None = None,
    now_utc: datetime | None = None,
) -> bool:
    """Applies post-processing success transitions for both movie and TV schedules."""
    item = get_tracking_item(application, item_id)
    if not item or item.get("status") in TERMINAL_TRACKING_STATES:
        return False

    if get_tracking_schedule_mode(item) != "ongoing_next_episode":
        return mark_tracking_fulfilled(application, item_id=item_id, now_utc=now_utc)

    now = now_utc or utc_now(application.bot_data.get(TRACKING_NOW_PROVIDER_KEY))
    payload = _ensure_target_payload(item)
    episode_ref = _coerce_episode_ref(
        payload.get("pending_episode")
    ) or _extract_episode_ref_from_parsed_info(parsed_info)
    if episode_ref is None:
        logger.warning(
            "[TRACKING] TV item %s fulfilled without episode metadata; scheduling hourly retry.",
            item_id,
        )
        return mark_tracking_hourly_retry(
            application,
            item_id=item_id,
            error_message="tracking_fulfillment_missing_episode_metadata",
            now_utc=now,
        )

    payload["episode_cursor"] = episode_ref
    payload["pending_episode"] = None
    payload["pending_episode_title"] = None
    payload["pending_episode_air_date"] = None

    item["status"] = "awaiting_metadata"  # type: ignore[typeddict-item]
    item["next_check_at_utc"] = isoformat_utc(now)
    item["last_checked_at_utc"] = isoformat_utc(now)
    item["linked_download_message_id"] = None
    item["fulfilled_at_utc"] = None
    item["fulfillment_state"] = "pending"  # type: ignore[typeddict-item]
    retry = _ensure_retry(item)
    retry["consecutive_failures"] = 0
    retry["last_error"] = None
    _sync_tv_compatibility_fields(item)

    persist_tracking_state_from_bot_data(application)
    logger.info(
        "[TRACKING] TV item %s advanced cursor to S%02dE%02d.",
        item_id,
        episode_ref["season"],
        episode_ref["episode"],
    )
    return True
