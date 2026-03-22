from __future__ import annotations

import json
import os
from typing import Any

from telegram_bot.config import logger
from telegram_bot.domain.types import TrackingItem, TrackingStateFile

TRACKING_STATE_VERSION = 1


def _normalize_tracking_item(item_id: str, raw_item: Any) -> TrackingItem | None:
    """Best-effort coercion for persisted tracking items."""
    if not isinstance(raw_item, dict):
        return None

    normalized: TrackingItem = {
        "id": str(raw_item.get("id") or item_id),
        "chat_id": int(raw_item.get("chat_id", 0) or 0),
        "target_kind": "movie",
        "target_identity": str(raw_item.get("target_identity") or ""),
        "title": str(raw_item.get("title") or raw_item.get("canonical_title") or ""),
        "canonical_title": str(raw_item.get("canonical_title") or raw_item.get("title") or ""),
        "release_date_status": "confirmed"
        if raw_item.get("release_date_status") == "confirmed"
        else "unknown",
        "status": str(raw_item.get("status") or "pending_date"),  # type: ignore[typeddict-item]
        "created_at_utc": str(raw_item.get("created_at_utc") or ""),
        "fulfillment_state": "fulfilled"
        if raw_item.get("fulfillment_state") == "fulfilled"
        else "pending",
    }

    if not normalized["id"] or not normalized["title"] or not normalized["canonical_title"]:
        return None
    if not normalized["created_at_utc"]:
        return None

    raw_year = raw_item.get("year")
    if isinstance(raw_year, int):
        normalized["year"] = raw_year
    elif isinstance(raw_year, str) and raw_year.isdigit():
        normalized["year"] = int(raw_year)

    for key in (
        "availability_date",
        "availability_source",
        "next_check_at_utc",
        "last_checked_at_utc",
        "fulfilled_at_utc",
    ):
        value = raw_item.get(key)
        if value is None:
            normalized[key] = None  # type: ignore[literal-required]
        elif isinstance(value, str):
            normalized[key] = value  # type: ignore[literal-required]

    linked_message_id = raw_item.get("linked_download_message_id")
    if isinstance(linked_message_id, int):
        normalized["linked_download_message_id"] = linked_message_id
    elif linked_message_id is None:
        normalized["linked_download_message_id"] = None

    if not normalized.get("target_identity"):
        year = normalized.get("year")
        if isinstance(year, int):
            normalized["target_identity"] = (
                f"movie:{normalized['canonical_title'].casefold()}:{int(year)}"
            )
        else:
            normalized["target_identity"] = f"movie:{normalized['canonical_title'].casefold()}"

    return normalized


def load_tracking_state(file_path: str) -> dict[str, TrackingItem]:
    """Loads durable tracking items from disk."""
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

    items: dict[str, TrackingItem] = {}
    for item_id, raw_item in raw_items.items():
        normalized = _normalize_tracking_item(str(item_id), raw_item)
        if normalized is None:
            continue
        items[normalized["id"]] = normalized

    logger.info("Loaded %d tracking item(s) from '%s'.", len(items), file_path)
    return items


def save_tracking_state(file_path: str, items: dict[str, TrackingItem]) -> None:
    """Persists tracking state to disk."""
    serializable_items: dict[str, TrackingItem] = {}
    for item_id, item in items.items():
        if not isinstance(item, dict):
            continue
        serializable_items[str(item_id)] = item

    payload: TrackingStateFile = {
        "version": TRACKING_STATE_VERSION,
        "items": serializable_items,
    }

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        logger.info("Saved %d tracking item(s) to '%s'.", len(serializable_items), file_path)
    except OSError as exc:
        logger.error("Could not save tracking state '%s': %s", file_path, exc)
