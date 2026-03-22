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

from . import movie_release_dates, selection
from .manager import (
    TERMINAL_TRACKING_STATES,
    TRACKING_LOOP_TASK_KEY,
    TRACKING_NOW_PROVIDER_KEY,
    calculate_release_day_first_check_utc,
    get_tracking_in_progress_ids,
    get_tracking_item,
    get_tracking_items,
    get_tracking_timezone,
    isoformat_utc,
    mark_tracking_hourly_retry,
    mark_tracking_waiting_fulfillment,
    mark_tracking_weekly_metadata_retry,
    parse_utc_iso,
    persist_tracking_state_from_bot_data,
    set_tracking_release_window,
    utc_now,
)

TRACKING_SCHEDULER_INTERVAL_SECONDS = 60


def _parse_iso_date(value: str | None) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _is_due(item: TrackingItem, now_utc: datetime) -> bool:
    status = str(item.get("status") or "")
    if status in TERMINAL_TRACKING_STATES or status == "waiting_fulfillment":
        return False
    next_check = parse_utc_iso(item.get("next_check_at_utc"))
    if next_check is None:
        return status == "watching_release"
    return next_check <= now_utc


def _build_search_context(application: Application) -> Any:
    return SimpleNamespace(
        bot_data=application.bot_data, application=application, bot=application.bot
    )


def _build_source_dict(item: TrackingItem, candidate: dict[str, Any]) -> SourceDict | None:
    page_url = candidate.get("page_url")
    if not isinstance(page_url, str) or not page_url:
        return None

    canonical_title = str(item.get("canonical_title") or item.get("title") or "Movie")
    parsed_info: dict[str, Any] = {"type": "movie", "title": canonical_title}
    year = item.get("year")
    if isinstance(year, int):
        parsed_info["year"] = year
        clean_name = f"{canonical_title} ({year})"
    else:
        clean_name = canonical_title

    return {
        "value": page_url,
        "type": "magnet" if page_url.startswith("magnet:") else "url",
        "parsed_info": parsed_info,
        "info_url": candidate.get("info_url"),
        "clean_name": clean_name,
        "tracking_item_id": str(item.get("id")),
    }


async def _queue_candidate_for_tracking_item(
    application: Application,
    *,
    item: TrackingItem,
    candidate: dict[str, Any],
    now_utc: datetime,
) -> None:
    source_dict = _build_source_dict(item, candidate)
    if source_dict is None:
        mark_tracking_hourly_retry(
            application,
            item_id=str(item.get("id")),
            now_utc=now_utc,
        )
        return

    chat_id = int(item.get("chat_id", 0) or 0)
    if chat_id <= 0:
        logger.warning("[TRACKING] Item %s has invalid chat id; skipping.", item.get("id"))
        mark_tracking_hourly_retry(
            application,
            item_id=str(item.get("id")),
            now_utc=now_utc,
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
            "[TRACKING] Could not send queue status message for item %s: %s", item["id"], exc
        )

    try:
        await queue_download_source(
            application,
            chat_id=chat_id,
            source_dict=source_dict,
            message_id=message_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[TRACKING] Failed to queue item %s: %s", item["id"], exc)
        mark_tracking_hourly_retry(
            application,
            item_id=str(item["id"]),
            now_utc=now_utc,
        )
        return

    mark_tracking_waiting_fulfillment(
        application,
        item_id=str(item["id"]),
        linked_message_id=message_id if message_id > 0 else None,
        now_utc=now_utc,
    )


async def _attempt_release_search(
    application: Application, *, item: TrackingItem, now_utc: datetime
) -> None:
    query = str(item.get("canonical_title") or item.get("title") or "").strip()
    if not query:
        mark_tracking_hourly_retry(application, item_id=str(item["id"]), now_utc=now_utc)
        return

    search_kwargs: dict[str, Any] = {}
    if isinstance(item.get("year"), int):
        search_kwargs["year"] = str(item["year"])

    context = _build_search_context(application)
    results = await orchestrate_searches(query, "movie", context, **search_kwargs)
    selected = selection.select_best_auto_download_candidate(
        results,
        search_config=application.bot_data.get("SEARCH_CONFIG", {}),
    )
    if selected is None:
        mark_tracking_hourly_retry(
            application,
            item_id=str(item["id"]),
            now_utc=now_utc,
        )
        return

    await _queue_candidate_for_tracking_item(
        application,
        item=item,
        candidate=selected,
        now_utc=now_utc,
    )


async def _process_pending_date_item(
    application: Application, *, item: TrackingItem, now_utc: datetime
) -> None:
    local_today = now_utc.astimezone(get_tracking_timezone(application.bot_data)).date()
    resolved = await movie_release_dates.resolve_movie_tracking_target(
        str(item.get("canonical_title") or item.get("title") or ""),
        year=item.get("year"),
        today=local_today,
    )

    availability_date = resolved.get("availability_date")
    if availability_date is None:
        mark_tracking_weekly_metadata_retry(
            application,
            item_id=str(item["id"]),
            now_utc=now_utc,
        )
        return

    set_tracking_release_window(
        application,
        item_id=str(item["id"]),
        availability_date=availability_date,
        availability_source=resolved.get("availability_source"),
        now_utc=now_utc,
    )

    refreshed = get_tracking_item(application, str(item["id"]))
    if refreshed and refreshed.get("status") == "watching_release":
        await _attempt_release_search(application, item=refreshed, now_utc=now_utc)


async def _process_waiting_release_window_item(
    application: Application, *, item: TrackingItem, now_utc: datetime
) -> None:
    release_day = _parse_iso_date(item.get("availability_date"))
    if release_day is None:
        mark_tracking_weekly_metadata_retry(
            application,
            item_id=str(item["id"]),
            now_utc=now_utc,
        )
        return

    first_check = calculate_release_day_first_check_utc(
        release_day,
        local_timezone=get_tracking_timezone(application.bot_data),
        now_utc=now_utc,
    )
    if first_check > now_utc:
        item["next_check_at_utc"] = isoformat_utc(first_check)
        item["last_checked_at_utc"] = isoformat_utc(now_utc)
        return

    item["status"] = "watching_release"  # type: ignore[typeddict-item]
    item["next_check_at_utc"] = isoformat_utc(now_utc)
    item["last_checked_at_utc"] = isoformat_utc(now_utc)
    await _attempt_release_search(application, item=item, now_utc=now_utc)


async def _process_due_item(
    application: Application, item: TrackingItem, *, now_utc: datetime
) -> None:
    status = str(item.get("status") or "")
    item["last_checked_at_utc"] = isoformat_utc(now_utc)

    if status == "pending_date":
        await _process_pending_date_item(application, item=item, now_utc=now_utc)
        return

    if status == "waiting_release_window":
        await _process_waiting_release_window_item(application, item=item, now_utc=now_utc)
        return

    if status == "watching_release":
        await _attempt_release_search(application, item=item, now_utc=now_utc)


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
            await _process_due_item(application, item, now_utc=now)
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
