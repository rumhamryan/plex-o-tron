from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock

import pytest

from telegram_bot.services.tracking import manager as tracking_manager
from telegram_bot.services.tracking import scheduler as tracking_scheduler


def _build_application(
    mocker,
    *,
    resolutions: dict[str, int | float] | None = None,
):
    if resolutions is None:
        resolutions = {"2160p": 5, "4k": 5, "1080p": 3}

    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=91)))
    app = SimpleNamespace(
        bot=bot,
        bot_data={
            "SAVE_PATHS": {"default": "/tmp"},
            "SEARCH_CONFIG": {
                "preferences": {
                    "movies": {
                        "resolutions": resolutions,
                    }
                }
            },
            "tracking_timezone": timezone.utc,
        },
    )

    mocker.patch(
        "telegram_bot.services.tracking.manager.persist_tracking_state_from_bot_data",
        return_value=None,
    )
    mocker.patch(
        "telegram_bot.services.tracking.scheduler.persist_tracking_state_from_bot_data",
        return_value=None,
    )
    return app


def _create_item(
    app,
    *,
    now_utc: datetime,
    availability_date: date | None,
    title: str = "Future Release",
    year: int | None = 2026,
) -> str:
    created = tracking_manager.create_movie_tracking_item(
        app,
        chat_id=456,
        canonical_title=title,
        year=year,
        availability_date=availability_date,
        availability_source="streaming" if availability_date else None,
        now_utc=now_utc,
    )
    return created["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tick_time", "expected_search_calls", "expected_status", "expected_next"),
    [
        (
            datetime(2026, 6, 1, 11, 59, tzinfo=timezone.utc),
            0,
            "waiting_release_window",
            "2026-06-01T12:00:00Z",
        ),
        (
            datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
            1,
            "watching_release",
            "2026-06-01T13:00:00Z",
        ),
    ],
)
async def test_tracking_scheduler_release_day_time_gate_matrix(
    mocker,
    tick_time: datetime,
    expected_search_calls: int,
    expected_status: str,
    expected_next: str,
):
    app = _build_application(mocker)
    item_id = _create_item(
        app,
        now_utc=datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc),
        availability_date=date(2026, 6, 1),
    )

    orchestrate_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.orchestrate_searches",
        AsyncMock(return_value=[]),
    )
    queue_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.queue_download_source",
        AsyncMock(return_value=(True, 1)),
    )

    await tracking_scheduler.run_tracking_scheduler_tick(app, now_utc=tick_time)

    assert orchestrate_mock.await_count == expected_search_calls
    assert queue_mock.await_count == 0
    item = tracking_manager.get_tracking_item(app, item_id)
    assert item is not None
    assert item["status"] == expected_status
    assert item["next_check_at_utc"] == expected_next


@pytest.mark.asyncio
async def test_tracking_scheduler_pending_date_unknown_remains_metadata_only(mocker):
    app = _build_application(mocker)
    item_id = _create_item(
        app,
        now_utc=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        availability_date=None,
    )
    item = tracking_manager.get_tracking_item(app, item_id)
    assert item is not None
    item["next_check_at_utc"] = "2026-06-01T09:00:00Z"

    resolve_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.movie_release_dates.resolve_movie_tracking_target",
        AsyncMock(
            return_value={
                "title": "Future Release",
                "canonical_title": "Future Release",
                "year": 2026,
                "is_released": False,
                "release_date_status": "unknown",
                "availability_date": None,
                "availability_source": None,
            }
        ),
    )
    orchestrate_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.orchestrate_searches",
        AsyncMock(return_value=[]),
    )

    await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    resolve_mock.assert_awaited_once()
    assert orchestrate_mock.await_count == 0
    current = tracking_manager.get_tracking_item(app, item_id)
    assert current is not None
    assert current["status"] == "pending_date"
    assert current["next_check_at_utc"] == "2026-06-08T10:00:00Z"
    assert current["release_date_status"] == "unknown"


@pytest.mark.asyncio
async def test_tracking_scheduler_pending_date_resolve_to_future_release_window(mocker):
    app = _build_application(mocker)
    item_id = _create_item(
        app,
        now_utc=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        availability_date=None,
    )
    item = tracking_manager.get_tracking_item(app, item_id)
    assert item is not None
    item["next_check_at_utc"] = "2026-06-01T09:00:00Z"

    resolve_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.movie_release_dates.resolve_movie_tracking_target",
        AsyncMock(
            return_value={
                "title": "Future Release",
                "canonical_title": "Future Release",
                "year": 2026,
                "is_released": False,
                "release_date_status": "confirmed",
                "availability_date": date(2026, 6, 10),
                "availability_source": "physical",
            }
        ),
    )
    orchestrate_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.orchestrate_searches",
        AsyncMock(return_value=[]),
    )

    await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    resolve_mock.assert_awaited_once()
    assert orchestrate_mock.await_count == 0
    current = tracking_manager.get_tracking_item(app, item_id)
    assert current is not None
    assert current["status"] == "waiting_release_window"
    assert current["next_check_at_utc"] == "2026-06-10T12:00:00Z"
    assert current["availability_date"] == "2026-06-10"
    assert current["availability_source"] == "physical"
    assert current["release_date_status"] == "confirmed"


@pytest.mark.asyncio
async def test_tracking_scheduler_pending_date_resolve_to_release_day_after_noon_searches_immediately(
    mocker,
):
    app = _build_application(mocker)
    item_id = _create_item(
        app,
        now_utc=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        availability_date=None,
    )
    item = tracking_manager.get_tracking_item(app, item_id)
    assert item is not None
    item["next_check_at_utc"] = "2026-06-01T09:00:00Z"

    resolve_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.movie_release_dates.resolve_movie_tracking_target",
        AsyncMock(
            return_value={
                "title": "Future Release",
                "canonical_title": "Future Release",
                "year": 2026,
                "is_released": True,
                "release_date_status": "confirmed",
                "availability_date": date(2026, 6, 1),
                "availability_source": "streaming",
            }
        ),
    )
    orchestrate_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.orchestrate_searches",
        AsyncMock(
            return_value=[
                {
                    "title": "Future Release 2026 1080p WEB",
                    "page_url": "magnet:?xt=urn:btih:1080",
                    "info_url": "https://example.invalid/1080",
                    "score": 200,
                }
            ]
        ),
    )
    queue_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.queue_download_source",
        AsyncMock(return_value=(True, 1)),
    )

    await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 13, 5, tzinfo=timezone.utc),
    )

    resolve_mock.assert_awaited_once()
    orchestrate_mock.assert_awaited_once()
    assert queue_mock.await_count == 0

    current = tracking_manager.get_tracking_item(app, item_id)
    assert current is not None
    assert current["status"] == "watching_release"
    assert current["availability_date"] == "2026-06-01"
    assert current["availability_source"] == "streaming"
    assert current["next_check_at_utc"] == "2026-06-01T14:00:00Z"


@pytest.mark.asyncio
async def test_tracking_scheduler_selects_best_candidate_within_top_tier(mocker):
    app = _build_application(mocker)
    item_id = _create_item(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        availability_date=date(2026, 6, 1),
    )

    orchestrate_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.orchestrate_searches",
        AsyncMock(
            return_value=[
                {
                    "title": "Future Release 2026 1080p WEB",
                    "page_url": "magnet:?xt=urn:btih:1080",
                    "info_url": "https://example.invalid/1080",
                    "score": 200,
                },
                {
                    "title": "Future Release 2026 4K WEB",
                    "page_url": "magnet:?xt=urn:btih:4k",
                    "info_url": "https://example.invalid/4k",
                    "score": 97,
                },
                {
                    "title": "Future Release 2026 2160p WEB",
                    "page_url": "magnet:?xt=urn:btih:2160",
                    "info_url": "https://example.invalid/2160",
                    "score": 99,
                },
            ]
        ),
    )
    queue_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.queue_download_source",
        AsyncMock(return_value=(True, 1)),
    )

    await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )

    orchestrate_mock.assert_awaited_once()
    queue_mock.assert_awaited_once()
    queued_source = queue_mock.await_args.kwargs["source_dict"]
    assert queued_source["value"] == "magnet:?xt=urn:btih:2160"

    current = tracking_manager.get_tracking_item(app, item_id)
    assert current is not None
    assert current["status"] == "waiting_fulfillment"
    assert current["next_check_at_utc"] is None


@pytest.mark.asyncio
async def test_tracking_scheduler_no_top_tier_candidate_schedules_hourly_retry(mocker):
    app = _build_application(mocker)
    item_id = _create_item(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        availability_date=date(2026, 6, 1),
    )

    orchestrate_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.orchestrate_searches",
        AsyncMock(
            return_value=[
                {
                    "title": "Future Release 2026 1080p WEB",
                    "page_url": "magnet:?xt=urn:btih:1080",
                    "info_url": "https://example.invalid/1080",
                    "score": 200,
                }
            ]
        ),
    )
    queue_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.queue_download_source",
        AsyncMock(return_value=(True, 1)),
    )

    await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )

    orchestrate_mock.assert_awaited_once()
    assert queue_mock.await_count == 0
    current = tracking_manager.get_tracking_item(app, item_id)
    assert current is not None
    assert current["status"] == "watching_release"
    assert current["next_check_at_utc"] == "2026-06-01T13:00:00Z"


@pytest.mark.asyncio
async def test_tracking_scheduler_queue_failure_returns_item_to_hourly_retry(mocker):
    app = _build_application(mocker)
    item_id = _create_item(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        availability_date=date(2026, 6, 1),
    )

    mocker.patch(
        "telegram_bot.services.tracking.scheduler.orchestrate_searches",
        AsyncMock(
            return_value=[
                {
                    "title": "Future Release 2026 2160p WEB",
                    "page_url": "magnet:?xt=urn:btih:2160",
                    "info_url": "https://example.invalid/2160",
                    "score": 99,
                }
            ]
        ),
    )
    queue_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.queue_download_source",
        AsyncMock(side_effect=RuntimeError("queue failure")),
    )

    await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )

    queue_mock.assert_awaited_once()
    current = tracking_manager.get_tracking_item(app, item_id)
    assert current is not None
    assert current["status"] == "watching_release"
    assert current["next_check_at_utc"] == "2026-06-01T13:00:00Z"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["waiting_fulfillment", "fulfilled", "cancelled"])
async def test_tracking_scheduler_terminal_and_waiting_states_are_not_processed(
    mocker,
    status: Literal["waiting_fulfillment", "fulfilled", "cancelled"],
):
    app = _build_application(mocker)
    item_id = _create_item(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        availability_date=date(2026, 6, 1),
    )
    item = tracking_manager.get_tracking_item(app, item_id)
    assert item is not None
    item["status"] = status
    item["next_check_at_utc"] = "2026-06-01T10:00:00Z"

    orchestrate_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.orchestrate_searches",
        AsyncMock(return_value=[]),
    )

    processed = await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )

    assert processed == 0
    assert orchestrate_mock.await_count == 0


@pytest.mark.asyncio
async def test_tracking_scheduler_skips_items_already_in_progress(mocker):
    app = _build_application(mocker)
    item_id = _create_item(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        availability_date=date(2026, 6, 1),
    )
    tracking_manager.get_tracking_in_progress_ids(app.bot_data).add(item_id)

    orchestrate_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.orchestrate_searches",
        AsyncMock(return_value=[]),
    )

    processed = await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )

    assert processed == 0
    assert orchestrate_mock.await_count == 0


@pytest.mark.asyncio
async def test_tracking_scheduler_release_date_lifecycle(mocker):
    app = _build_application(mocker)

    release_day = date(2026, 6, 1)
    created = tracking_manager.create_movie_tracking_item(
        app,
        chat_id=456,
        canonical_title="Future Release",
        year=2026,
        availability_date=release_day,
        availability_source="streaming",
        now_utc=datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc),
    )
    item_id = created["id"]
    assert created["status"] == "waiting_release_window"
    assert created["next_check_at_utc"] == "2026-06-01T12:00:00Z"

    orchestration_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.orchestrate_searches",
        AsyncMock(return_value=[]),
    )
    queue_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.queue_download_source",
        AsyncMock(return_value=(True, 1)),
    )

    # Before noon local: no torrent-site search.
    await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 11, 30, tzinfo=timezone.utc),
    )
    assert orchestration_mock.await_count == 0
    assert queue_mock.await_count == 0

    # Release-day noon: first search happens, but only lower-tier result => no queue.
    orchestration_mock.return_value = [
        {
            "title": "Future Release 2026 1080p WEB",
            "page_url": "magnet:?xt=urn:btih:1080",
            "info_url": "https://example.invalid/1080",
            "score": 95,
        }
    ]
    await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )
    assert orchestration_mock.await_count == 1
    assert queue_mock.await_count == 0

    current = tracking_manager.get_tracking_item(app, item_id)
    assert current is not None
    assert current["status"] == "watching_release"
    assert current["next_check_at_utc"] == "2026-06-01T13:00:00Z"

    # Next hourly check with an eligible top-tier result queues the download.
    orchestration_mock.return_value = [
        {
            "title": "Future Release 2026 2160p WEB",
            "page_url": "magnet:?xt=urn:btih:2160",
            "info_url": "https://example.invalid/2160",
            "score": 99,
        }
    ]
    await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 13, 0, tzinfo=timezone.utc),
    )
    assert orchestration_mock.await_count == 2
    queue_mock.assert_awaited_once()

    waiting = tracking_manager.get_tracking_item(app, item_id)
    assert waiting is not None
    assert waiting["status"] == "waiting_fulfillment"
    assert waiting["next_check_at_utc"] is None

    # Fulfillment transition occurs only after successful post-processing.
    fulfilled = tracking_manager.mark_tracking_fulfilled(
        app,
        item_id=item_id,
        now_utc=datetime(2026, 6, 1, 13, 10, tzinfo=timezone.utc),
    )
    assert fulfilled is True
    final_item = tracking_manager.get_tracking_item(app, item_id)
    assert final_item is not None
    assert final_item["status"] == "fulfilled"
    assert final_item["fulfillment_state"] == "fulfilled"
