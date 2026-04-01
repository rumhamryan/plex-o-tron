from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, cast

from telegram.ext import Application

from telegram_bot.config import logger
from telegram_bot.domain.types import BatchCollectionMeta, BatchMeta, SourceDict, TrackingItem
from telegram_bot.services.download_manager.bot_data_access import get_or_create_download_batches
from telegram_bot.services.download_manager.queue import queue_download_source
from telegram_bot.services.search_logic import orchestrate_searches
from telegram_bot.utils import sanitize_collection_name

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
from . import tv_next_episode

TRACKING_SCHEDULER_INTERVAL_SECONDS = 60


def _coerce_non_negative_int(value: Any, *, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _collect_tracking_ids_with_pending_downloads(bot_data: dict[str, Any]) -> set[str]:
    active_downloads = bot_data.get("active_downloads")
    download_queues = bot_data.get("download_queues")
    pending_ids: set[str] = set()

    def _capture_tracking_id(download_data: Any) -> None:
        if not isinstance(download_data, dict):
            return
        source_dict = download_data.get("source_dict")
        if not isinstance(source_dict, dict):
            return
        tracking_item_id = source_dict.get("tracking_item_id")
        if not isinstance(tracking_item_id, str):
            return
        normalized = tracking_item_id.strip()
        if normalized:
            pending_ids.add(normalized)

    if isinstance(active_downloads, dict):
        for raw_download_data in active_downloads.values():
            _capture_tracking_id(raw_download_data)

    if isinstance(download_queues, dict):
        for queued_items in download_queues.values():
            if not isinstance(queued_items, list):
                continue
            for queued_download_data in queued_items:
                _capture_tracking_id(queued_download_data)

    return pending_ids


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


def _coerce_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalize_tracking_collection_movies(raw_movies: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_movies, list):
        return []

    normalized_movies: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None]] = set()
    for raw_movie in raw_movies:
        if not isinstance(raw_movie, dict):
            continue
        raw_title = raw_movie.get("title")
        if not isinstance(raw_title, str):
            continue
        title = raw_title.strip()
        if not title:
            continue
        year = _coerce_positive_int(raw_movie.get("year"))
        dedupe_key = (title.casefold(), year)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        entry: dict[str, Any] = {"title": title}
        if isinstance(year, int):
            entry["year"] = year
        normalized_movies.append(entry)
    return normalized_movies


def _resolve_tracking_collection_meta(item: TrackingItem) -> BatchCollectionMeta | None:
    payload = item.get("target_payload")
    payload_dict = payload if isinstance(payload, dict) else {}
    raw_collection_name = payload_dict.get("collection_name")
    if not isinstance(raw_collection_name, str) or not raw_collection_name.strip():
        return None

    collection_name = sanitize_collection_name(raw_collection_name)
    raw_collection_fs_name = payload_dict.get("collection_fs_name")
    if isinstance(raw_collection_fs_name, str) and raw_collection_fs_name.strip():
        collection_fs_name = sanitize_collection_name(raw_collection_fs_name)
    else:
        collection_fs_name = collection_name

    canonical_title = str(
        payload_dict.get("canonical_title")
        or item.get("canonical_title")
        or item.get("display_title")
        or item.get("title")
        or "Unknown"
    ).strip()
    year = _coerce_positive_int(payload_dict.get("year", item.get("year")))
    movie_meta: dict[str, Any] = {"title": canonical_title or "Unknown"}
    if isinstance(year, int):
        movie_meta["year"] = year
    collection_movies = _normalize_tracking_collection_movies(payload_dict.get("collection_movies"))
    if not collection_movies:
        collection_movies = [movie_meta]
    else:
        current_movie_key = (
            str(movie_meta.get("title") or "").casefold(),
            _coerce_positive_int(movie_meta.get("year")),
        )
        existing_keys = {
            (str(movie.get("title") or "").casefold(), _coerce_positive_int(movie.get("year")))
            for movie in collection_movies
        }
        if current_movie_key not in existing_keys:
            collection_movies.append(movie_meta)

    return {
        "name": collection_name,
        "fs_name": collection_fs_name,
        "movies": collection_movies,
    }


def _attach_tracking_collection_batch(
    application: Application,
    *,
    item: TrackingItem,
    source_dict: SourceDict,
) -> None:
    collection_meta = _resolve_tracking_collection_meta(item)
    if collection_meta is None:
        return

    item_id = str(item.get("id") or "").strip()
    if not item_id:
        return
    batch_id = f"tracking-collection-{item_id}"
    source_dict["batch_id"] = batch_id
    parsed_info = source_dict.get("parsed_info")
    if isinstance(parsed_info, dict):
        parsed_info["collection_name"] = collection_meta["name"]

    batches: dict[str, BatchMeta] = get_or_create_download_batches(application.bot_data)
    batch = batches.get(batch_id)
    if not isinstance(batch, dict):
        batches[batch_id] = {
            "total": 1,
            "done": 0,
            "media_type": "movie",
            "scanned": False,
            "summaries": [],
            "collection": collection_meta,
        }
        return

    batch.setdefault("total", 1)
    batch.setdefault("done", 0)
    batch.setdefault("media_type", "movie")
    batch.setdefault("scanned", False)
    if not isinstance(batch.get("summaries"), list):
        batch["summaries"] = []
    if not isinstance(batch.get("collection"), dict):
        batch["collection"] = collection_meta


async def _resolve_tracking_tv_episode_title(
    *,
    item: TrackingItem,
    search_request: TrackingSearchRequest,
) -> str | None:
    """Resolves the canonical episode title for tracked TV downloads."""
    parsed_info = search_request.parsed_info if isinstance(search_request.parsed_info, dict) else {}
    season = _coerce_positive_int(parsed_info.get("season"))
    episode = _coerce_positive_int(parsed_info.get("episode"))
    if season is None or episode is None:
        return None

    payload = item.get("target_payload")
    payload_dict = payload if isinstance(payload, dict) else {}
    tmdb_series_id = _coerce_positive_int(payload_dict.get("tmdb_series_id"))
    if tmdb_series_id is None:
        return None

    return await tv_next_episode.fetch_episode_title_for_tmdb_episode(
        tmdb_series_id=tmdb_series_id,
        season=season,
        episode=episode,
    )


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
    tracking_ids_with_pending_downloads = _collect_tracking_ids_with_pending_downloads(
        cast(dict[str, Any], application.bot_data)
    )

    nudged = 0
    for item in items.values():
        item_id = str(item.get("id") or "").strip()
        status = str(item.get("status") or "")
        if status in TERMINAL_TRACKING_STATES:
            continue

        if status in {"searching", "waiting_fulfillment"}:
            if item_id in tracking_ids_with_pending_downloads:
                continue
            item["next_check_at_utc"] = isoformat_utc(now)
            if status == "waiting_fulfillment":
                # The queued download reference was lost across restarts; resume search immediately.
                item["status"] = "searching"  # type: ignore[typeddict-item]
                item["linked_download_message_id"] = None
            nudged += 1
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
    effective_search_request = search_request
    if getattr(adapter, "target_kind", "") == "tv":
        resolved_episode_title = await _resolve_tracking_tv_episode_title(
            item=item,
            search_request=search_request,
        )
        if (
            isinstance(resolved_episode_title, str)
            and resolved_episode_title.strip()
            and resolved_episode_title != search_request.pending_episode_title
        ):
            effective_search_request = replace(
                search_request,
                pending_episode_title=resolved_episode_title,
            )

    source_dict = _build_source_dict(
        item=item,
        candidate=candidate,
        search_request=effective_search_request,
    )
    if source_dict is None:
        adapter.on_queue_failure(
            application,
            item=item,
            now_utc=now_utc,
            error_message="tracking_candidate_missing_page_url",
        )
        return

    _attach_tracking_collection_batch(application, item=item, source_dict=source_dict)

    parsed_info = source_dict.get("parsed_info")
    if isinstance(parsed_info, dict) and parsed_info.get("type") == "tv":
        season = _coerce_positive_int(parsed_info.get("season")) or 0
        episode = _coerce_positive_int(parsed_info.get("episode")) or 0
        episode_title = effective_search_request.pending_episode_title
        if isinstance(episode_title, str) and episode_title.strip():
            parsed_info["episode_title"] = episode_title.strip()
        else:
            parsed_info.pop("episode_title", None)
        logger.info(
            "[TRACKING] Queueing tracked TV candidate for item %s: %s S%02dE%02d%s",
            item_id or "(unknown)",
            str(parsed_info.get("title") or "TV Show"),
            season,
            episode,
            f" - {parsed_info['episode_title']}"
            if isinstance(parsed_info.get("episode_title"), str)
            and parsed_info.get("episode_title")
            else "",
        )

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
        search_request=effective_search_request,
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
    search_kwargs = dict(search_request.search_kwargs)
    results = await orchestrate_searches(
        search_request.query,
        search_request.media_type,
        context,
        **search_kwargs,
    )

    # TPB API occasionally reports temporarily stale/low swarm counts for new TV
    # episodes. If the first pass yields nothing, retry once with relaxed swarm
    # gating so tracking can still attempt a candidate instead of idling for an hour.
    current_status = str(item.get("status") or "")
    if (
        not results
        and getattr(adapter, "target_kind", "") == "tv"
        and current_status == "searching"
        and _coerce_non_negative_int(search_kwargs.get("min_seeders"), default=20) > 0
    ):
        item_id = str(item.get("id") or "")
        logger.info(
            "[TRACKING] No TV candidates for item %s at min_seeders=%d; retrying with min_seeders=0.",
            item_id or "(unknown)",
            _coerce_non_negative_int(search_kwargs.get("min_seeders"), default=20),
        )
        relaxed_search_kwargs = dict(search_kwargs)
        relaxed_search_kwargs["min_seeders"] = 0
        results = await orchestrate_searches(
            search_request.query,
            search_request.media_type,
            context,
            **relaxed_search_kwargs,
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
