from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

from telegram.ext import Application

from telegram_bot.config import logger
from telegram_bot.domain.types import SourceDict, TrackingItem
from telegram_bot.services.download_manager.queue import queue_download_source
from telegram_bot.services.search_logic import orchestrate_searches

from .manager import (
    TERMINAL_TRACKING_STATES,
    TRACKING_LOOP_TASK_KEY,
    TRACKING_NOW_PROVIDER_KEY,
    calculate_release_day_first_check_utc,
    get_tracking_in_progress_ids,
    get_tracking_item,
    get_tracking_items,
    get_tracking_target_kind,
    get_tracking_timezone,
    isoformat_utc,
    parse_utc_iso,
    persist_tracking_state_from_bot_data,
    utc_now,
)
from .targets import get_tracking_adapter_for_item
from .targets.base import TrackingSearchRequest, TrackingTargetAdapter

TRACKING_SCHEDULER_INTERVAL_SECONDS = 60


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


def _resolve_known_release_day(item: TrackingItem) -> date | None:
    payload = item.get("target_payload")
    payload_dict = payload if isinstance(payload, dict) else {}
    if get_tracking_target_kind(item) == "tv":
        return _coerce_iso_date(payload_dict.get("pending_episode_air_date"))
    return _coerce_iso_date(payload_dict.get("availability_date", item.get("availability_date")))


def _release_window_is_open(
    item: TrackingItem,
    *,
    now_utc: datetime,
    local_timezone: Any,
) -> bool:
    status = str(item.get("status") or "")
    if status not in {"awaiting_metadata", "awaiting_window"}:
        return False
    release_day = _resolve_known_release_day(item)
    if release_day is None:
        return False
    first_check = calculate_release_day_first_check_utc(
        release_day,
        local_timezone=local_timezone,
        now_utc=now_utc,
    )
    return first_check <= now_utc


def _is_due(
    item: TrackingItem,
    now_utc: datetime,
) -> bool:
    status = str(item.get("status") or "")
    if status in TERMINAL_TRACKING_STATES:
        return False

    next_check = parse_utc_iso(item.get("next_check_at_utc"))
    if next_check is None:
        return status in {"awaiting_metadata", "awaiting_window", "searching"}
    return next_check <= now_utc


def reconcile_tracking_items_on_startup(
    application: Application,
    *,
    now_utc: datetime | None = None,
) -> int:
    """
    Startup-only reconciliation that nudges overdue release windows to run immediately.

    This does not alter normal runtime cadence; it only performs a one-time recovery
    when the process boots or restarts.
    """
    now_provider = application.bot_data.get(TRACKING_NOW_PROVIDER_KEY)
    now = now_utc or utc_now(now_provider)
    local_timezone = get_tracking_timezone(application.bot_data)
    items = get_tracking_items(application.bot_data)

    nudged = 0
    for item in items.values():
        status = str(item.get("status") or "")
        if status in TERMINAL_TRACKING_STATES:
            continue
        if status not in {"awaiting_metadata", "awaiting_window"}:
            continue

        target_kind = get_tracking_target_kind(item)
        release_window_open = _release_window_is_open(
            item,
            now_utc=now,
            local_timezone=local_timezone,
        )
        # For TV metadata-only schedules, run one immediate boot-time refresh to recover
        # from any stale weekly deferment caused before the latest transient-failure fix.
        tv_metadata_boot_recheck = target_kind == "tv" and status == "awaiting_metadata"
        if not (release_window_open or tv_metadata_boot_recheck):
            continue

        item["next_check_at_utc"] = isoformat_utc(now)
        nudged += 1

    if nudged > 0:
        persist_tracking_state_from_bot_data(application)
    logger.info("[TRACKING] Startup reconciliation nudged %d item(s).", nudged)
    return nudged


def _build_search_context(application: Application) -> Any:
    return SimpleNamespace(
        bot_data=application.bot_data,
        application=application,
        bot=application.bot,
    )


def _build_source_dict(
    *,
    item: TrackingItem,
    candidate: dict[str, Any],
    search_request: TrackingSearchRequest,
) -> SourceDict | None:
    page_url = candidate.get("page_url")
    if not isinstance(page_url, str) or not page_url:
        return None
    parsed_info = dict(search_request.parsed_info)
    return {
        "value": page_url,
        "type": "magnet" if page_url.startswith("magnet:") else "url",
        "parsed_info": parsed_info,
        "info_url": candidate.get("info_url"),
        "clean_name": search_request.clean_name,
        "tracking_item_id": str(item.get("id")),
    }


async def _queue_candidate_for_tracking_item(
    application: Application,
    *,
    item: TrackingItem,
    candidate: dict[str, Any],
    search_request: TrackingSearchRequest,
    adapter: TrackingTargetAdapter,
    now_utc: datetime,
) -> None:
    item_id = str(item.get("id"))
    source_dict = _build_source_dict(item=item, candidate=candidate, search_request=search_request)
    if source_dict is None:
        adapter.on_queue_failure(
            application,
            item=item,
            now_utc=now_utc,
            error_message="tracking_candidate_missing_page_url",
        )
        return

    chat_id = int(item.get("chat_id", 0) or 0)
    if chat_id <= 0:
        logger.warning("[TRACKING] Item %s has invalid chat id; skipping.", item_id)
        adapter.on_queue_failure(
            application,
            item=item,
            now_utc=now_utc,
            error_message="tracking_item_invalid_chat_id",
        )
        return

    message_id = int(item.get("linked_download_message_id") or 0)
    try:
        status_message = await application.bot.send_message(
            chat_id=chat_id,
            text="🎯 Scheduled tracking found a top-tier candidate. Queueing download...",
        )
        message_id = status_message.message_id
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[TRACKING] Could not send queue status message for item %s: %s",
            item_id,
            exc,
        )

    try:
        await queue_download_source(
            application,
            chat_id=chat_id,
            source_dict=source_dict,
            message_id=message_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[TRACKING] Failed to queue item %s: %s", item_id, exc)
        adapter.on_queue_failure(
            application,
            item=item,
            now_utc=now_utc,
            error_message="tracking_queue_failure",
        )
        return

    adapter.on_queue_success(
        application,
        item=item,
        now_utc=now_utc,
        linked_message_id=message_id if message_id > 0 else None,
        selected_candidate=candidate,
        search_request=search_request,
    )


async def _attempt_release_search(
    application: Application,
    *,
    item: TrackingItem,
    adapter: TrackingTargetAdapter,
    now_utc: datetime,
) -> None:
    search_request = adapter.build_search_request(item=item)
    if search_request is None:
        adapter.on_queue_failure(
            application,
            item=item,
            now_utc=now_utc,
            error_message="tracking_missing_search_request",
        )
        return

    context = _build_search_context(application)
    results = await orchestrate_searches(
        search_request.query,
        search_request.media_type,
        context,
        **search_request.search_kwargs,
    )
    selected = adapter.select_candidate(
        results,
        search_config=application.bot_data.get("SEARCH_CONFIG", {}),
    )
    if selected is None:
        adapter.on_queue_failure(
            application,
            item=item,
            now_utc=now_utc,
            error_message="tracking_no_top_tier_candidate",
        )
        return

    await _queue_candidate_for_tracking_item(
        application,
        item=item,
        candidate=selected,
        search_request=search_request,
        adapter=adapter,
        now_utc=now_utc,
    )


async def _process_waiting_fulfillment_item(
    application: Application,
    *,
    item: TrackingItem,
    adapter: TrackingTargetAdapter,
    now_utc: datetime,
) -> None:
    """Watchdog fallback: if fulfillment confirmation stalls, resume searching."""
    adapter.on_queue_failure(
        application,
        item=item,
        now_utc=now_utc,
        error_message="tracking_fulfillment_timeout_watchdog",
    )


async def _process_due_item(
    application: Application,
    item: TrackingItem,
    *,
    now_utc: datetime,
) -> None:
    item_id = str(item.get("id") or "")
    adapter = get_tracking_adapter_for_item(item)
    if adapter is None:
        logger.warning(
            "[TRACKING] Item %s has unsupported target kind '%s'.",
            item_id or "(unknown)",
            get_tracking_target_kind(item),
        )
        return

    status = str(item.get("status") or "")
    if status in {"awaiting_metadata", "awaiting_window"}:
        refreshed = await adapter.refresh_target_metadata(
            application,
            item=item,
            now_utc=now_utc,
        )
        current_item = refreshed or get_tracking_item(application, item_id) or item
        current_status = str(current_item.get("status") or "")
        if current_status == "searching" and _is_due(current_item, now_utc):
            await _attempt_release_search(
                application,
                item=current_item,
                adapter=adapter,
                now_utc=now_utc,
            )
        return

    if status == "searching":
        await _attempt_release_search(
            application,
            item=item,
            adapter=adapter,
            now_utc=now_utc,
        )
        return

    if status == "waiting_fulfillment":
        await _process_waiting_fulfillment_item(
            application,
            item=item,
            adapter=adapter,
            now_utc=now_utc,
        )


async def run_tracking_scheduler_tick(
    application: Application,
    *,
    now_utc: datetime | None = None,
) -> int:
    """Runs one scheduler pass over due items."""
    now_provider = application.bot_data.get(TRACKING_NOW_PROVIDER_KEY)
    now = now_utc or utc_now(now_provider)
    items = get_tracking_items(application.bot_data)
    in_progress = get_tracking_in_progress_ids(application.bot_data)

    due_ids = [item_id for item_id, item in items.items() if _is_due(item, now)]
    processed = 0
    for item_id in due_ids:
        if item_id in in_progress:
            continue
        item = items.get(item_id)
        if not item:
            continue

        in_progress.add(item_id)
        try:
            await _process_due_item(
                application,
                item,
                now_utc=now,
            )
            persist_tracking_state_from_bot_data(application)
            processed += 1
        except Exception:  # noqa: BLE001
            logger.exception("[TRACKING] Scheduler failed processing item %s", item_id)
        finally:
            in_progress.discard(item_id)
    return processed


async def _tracking_scheduler_loop(application: Application) -> None:
    logger.info("[TRACKING] Scheduler loop started.")
    while True:
        await run_tracking_scheduler_tick(application)
        await asyncio.sleep(TRACKING_SCHEDULER_INTERVAL_SECONDS)


def start_tracking_scheduler(application: Application) -> None:
    existing_task = application.bot_data.get(TRACKING_LOOP_TASK_KEY)
    if isinstance(existing_task, asyncio.Task) and not existing_task.done():
        return
    loop = asyncio.get_running_loop()
    task = loop.create_task(_tracking_scheduler_loop(application))
    application.bot_data[TRACKING_LOOP_TASK_KEY] = task


async def stop_tracking_scheduler(application: Application) -> None:
    task = application.bot_data.get(TRACKING_LOOP_TASK_KEY)
    if not isinstance(task, asyncio.Task):
        return
    if task.done():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    application.bot_data[TRACKING_LOOP_TASK_KEY] = None
