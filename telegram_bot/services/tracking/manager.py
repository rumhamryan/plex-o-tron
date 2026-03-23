from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import uuid4

from telegram.ext import Application

from telegram_bot.config import TRACKING_STATE_FILE, logger
from telegram_bot.domain.types import TrackingItem

from .persistence import load_tracking_state, save_tracking_state

TRACKING_ITEMS_KEY = "tracking_items"
TRACKING_IN_PROGRESS_KEY = "tracking_in_progress_ids"
TRACKING_LOOP_TASK_KEY = "tracking_loop_task"
TRACKING_TIMEZONE_KEY = "tracking_timezone"
TRACKING_NOW_PROVIDER_KEY = "tracking_now_provider"
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


def load_tracking_state_into_bot_data(
    application: Application,
    *,
    file_path: str = TRACKING_STATE_FILE,
) -> dict[str, TrackingItem]:
    items = load_tracking_state(file_path)
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
    """First permitted torrent search time: local noon on release date."""
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


def create_movie_tracking_item(
    application: Application,
    *,
    chat_id: int,
    canonical_title: str,
    year: int | None,
    availability_date: date | None,
    availability_source: str | None,
    title: str | None = None,
    now_utc: datetime | None = None,
) -> TrackingItem:
    """Creates and persists a new movie tracking item."""
    items = get_tracking_items(application.bot_data)
    now = now_utc or utc_now(application.bot_data.get(TRACKING_NOW_PROVIDER_KEY))
    item_id = _next_tracking_item_id(items)
    local_timezone = get_tracking_timezone(application.bot_data)

    if isinstance(availability_date, date):
        first_check = calculate_release_day_first_check_utc(
            availability_date,
            local_timezone=local_timezone,
            now_utc=now,
        )
        status = "watching_release" if first_check <= now else "waiting_release_window"
        next_check = now if status == "watching_release" else first_check
        release_status = "confirmed"
        availability_iso = availability_date.isoformat()
    else:
        status = "pending_date"
        next_check = calculate_next_weekly_metadata_check(now)
        release_status = "unknown"
        availability_iso = None
        availability_source = None

    normalized_title = canonical_title.strip() or (title or "").strip() or "Unknown"
    identity = (
        f"movie:{normalized_title.casefold()}:{year}"
        if isinstance(year, int)
        else f"movie:{normalized_title.casefold()}"
    )

    item: TrackingItem = {
        "id": item_id,
        "chat_id": int(chat_id),
        "target_kind": "movie",
        "target_identity": identity,
        "title": title or normalized_title,
        "canonical_title": normalized_title,
        "year": year,
        "release_date_status": release_status,  # type: ignore[typeddict-item]
        "availability_date": availability_iso,
        "availability_source": availability_source,  # type: ignore[typeddict-item]
        "status": status,  # type: ignore[typeddict-item]
        "next_check_at_utc": isoformat_utc(next_check),
        "last_checked_at_utc": None,
        "created_at_utc": isoformat_utc(now),
        "fulfilled_at_utc": None,
        "fulfillment_state": "pending",
        "linked_download_message_id": None,
    }
    items[item_id] = item
    persist_tracking_state_from_bot_data(application)
    logger.info(
        "[TRACKING] Created item %s for '%s' (year=%s, status=%s).",
        item_id,
        normalized_title,
        year,
        status,
    )
    return item


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

    # Remove from memory + persistence so user-initiated cancel truly deletes
    # the schedule rather than leaving a cancelled record on disk.
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
    now_utc: datetime | None = None,
) -> bool:
    item = get_tracking_item(application, item_id)
    if not item or item.get("status") in TERMINAL_TRACKING_STATES:
        return False
    now = now_utc or utc_now(application.bot_data.get(TRACKING_NOW_PROVIDER_KEY))
    item["status"] = "waiting_fulfillment"  # type: ignore[typeddict-item]
    item["next_check_at_utc"] = None
    item["last_checked_at_utc"] = isoformat_utc(now)
    if linked_message_id is not None:
        item["linked_download_message_id"] = int(linked_message_id)
    persist_tracking_state_from_bot_data(application)
    logger.info("[TRACKING] Item %s is now waiting for fulfillment.", item_id)
    return True


def mark_tracking_hourly_retry(
    application: Application,
    *,
    item_id: str,
    now_utc: datetime | None = None,
) -> bool:
    item = get_tracking_item(application, item_id)
    if not item or item.get("status") in TERMINAL_TRACKING_STATES:
        return False
    now = now_utc or utc_now(application.bot_data.get(TRACKING_NOW_PROVIDER_KEY))
    item["status"] = "watching_release"  # type: ignore[typeddict-item]
    item["next_check_at_utc"] = isoformat_utc(calculate_next_hourly_check(now))
    item["last_checked_at_utc"] = isoformat_utc(now)
    persist_tracking_state_from_bot_data(application)
    logger.info("[TRACKING] Item %s scheduled for next hourly check.", item_id)
    return True


def mark_tracking_weekly_metadata_retry(
    application: Application,
    *,
    item_id: str,
    now_utc: datetime | None = None,
) -> bool:
    item = get_tracking_item(application, item_id)
    if not item or item.get("status") in TERMINAL_TRACKING_STATES:
        return False
    now = now_utc or utc_now(application.bot_data.get(TRACKING_NOW_PROVIDER_KEY))
    item["status"] = "pending_date"  # type: ignore[typeddict-item]
    item["release_date_status"] = "unknown"  # type: ignore[typeddict-item]
    item["availability_date"] = None
    item["availability_source"] = None
    item["next_check_at_utc"] = isoformat_utc(calculate_next_weekly_metadata_check(now))
    item["last_checked_at_utc"] = isoformat_utc(now)
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
        status = "watching_release"
        next_check = now
    else:
        status = "waiting_release_window"
        next_check = first_check

    item["release_date_status"] = "confirmed"  # type: ignore[typeddict-item]
    item["availability_date"] = availability_date.isoformat()
    item["availability_source"] = availability_source  # type: ignore[typeddict-item]
    item["status"] = status  # type: ignore[typeddict-item]
    item["next_check_at_utc"] = isoformat_utc(next_check)
    item["last_checked_at_utc"] = isoformat_utc(now)
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
    persist_tracking_state_from_bot_data(application)
    logger.info("[TRACKING] Item %s fulfilled.", item_id)
    return True
