from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from telegram_bot.services.tracking import manager as tracking_manager
from telegram_bot.services.tracking import scheduler as tracking_scheduler


def _build_application(
    mocker,
    *,
    movie_resolutions: dict[str, int | float] | None = None,
    tv_resolutions: dict[str, int | float] | None = None,
):
    if movie_resolutions is None:
        movie_resolutions = {"2160p": 5, "4k": 5, "1080p": 3}
    if tv_resolutions is None:
        tv_resolutions = {"1080p": 4, "720p": 2}

    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=91)))
    app = SimpleNamespace(
        bot=bot,
        bot_data={
            "SAVE_PATHS": {"default": "/tmp"},
            "SEARCH_CONFIG": {
                "preferences": {
                    "movies": {"resolutions": movie_resolutions},
                    "tv": {"resolutions": tv_resolutions},
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
    mocker.patch(
        "telegram_bot.services.tracking.targets.tv_ongoing.persist_tracking_state_from_bot_data",
        return_value=None,
    )
    return app


def _create_movie_item(
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


def _create_tv_item(
    app,
    *,
    now_utc: datetime,
    title: str = "Future Show",
    tmdb_series_id: int = 1234,
) -> str:
    created = tracking_manager.create_tv_tracking_item(
        app,
        chat_id=456,
        canonical_title=title,
        tmdb_series_id=tmdb_series_id,
        now_utc=now_utc,
    )
    return created["id"]


@pytest.mark.asyncio
async def test_tracking_scheduler_movie_release_day_time_gate_matrix(mocker):
    app = _build_application(mocker)
    item_id = _create_movie_item(
        app,
        now_utc=datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc),
        availability_date=date(2026, 6, 1),
    )

    orchestrate_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.orchestrate_searches",
        AsyncMock(return_value=[]),
    )

    # Before noon local: item is not due.
    processed_before_noon = await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 11, 59, tzinfo=timezone.utc),
    )
    assert processed_before_noon == 0
    assert orchestrate_mock.await_count == 0

    # At noon: first search runs; no top-tier result means hourly retry.
    processed_at_noon = await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )
    assert processed_at_noon == 1
    assert orchestrate_mock.await_count == 1
    item = tracking_manager.get_tracking_item(app, item_id)
    assert item is not None
    assert item["status"] == "searching"
    assert item["next_check_at_utc"] == "2026-06-01T13:00:00Z"


@pytest.mark.asyncio
async def test_tracking_scheduler_movie_metadata_unknown_remains_weekly(mocker):
    app = _build_application(mocker)
    item_id = _create_movie_item(
        app,
        now_utc=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        availability_date=None,
    )
    item = tracking_manager.get_tracking_item(app, item_id)
    assert item is not None
    item["next_check_at_utc"] = "2026-06-01T09:00:00Z"

    resolve_mock = mocker.patch(
        "telegram_bot.services.tracking.targets.movie.movie_release_dates.resolve_movie_tracking_target",
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

    await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    resolve_mock.assert_awaited_once()
    current = tracking_manager.get_tracking_item(app, item_id)
    assert current is not None
    assert current["status"] == "awaiting_metadata"
    assert current["next_check_at_utc"] == "2026-06-08T10:00:00Z"
    assert current["release_date_status"] == "unknown"


@pytest.mark.asyncio
async def test_tracking_scheduler_movie_metadata_resolves_future_release_window(mocker):
    app = _build_application(mocker)
    item_id = _create_movie_item(
        app,
        now_utc=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        availability_date=None,
    )
    item = tracking_manager.get_tracking_item(app, item_id)
    assert item is not None
    item["next_check_at_utc"] = "2026-06-01T09:00:00Z"

    resolve_mock = mocker.patch(
        "telegram_bot.services.tracking.targets.movie.movie_release_dates.resolve_movie_tracking_target",
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

    await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )

    resolve_mock.assert_awaited_once()
    current = tracking_manager.get_tracking_item(app, item_id)
    assert current is not None
    assert current["status"] == "awaiting_window"
    assert current["next_check_at_utc"] == "2026-06-10T12:00:00Z"
    assert current["availability_date"] == "2026-06-10"
    assert current["availability_source"] == "physical"
    assert current["release_date_status"] == "confirmed"


@pytest.mark.asyncio
async def test_tracking_scheduler_movie_selects_best_top_tier_candidate(mocker):
    app = _build_application(mocker)
    item_id = _create_movie_item(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        availability_date=date(2026, 6, 1),
    )
    item = tracking_manager.get_tracking_item(app, item_id)
    assert item is not None
    item["status"] = "searching"
    item["next_check_at_utc"] = "2026-06-01T12:00:00Z"

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
    assert current["next_check_at_utc"] == "2026-06-01T18:00:00Z"
    assert current["linked_download_message_id"] == 91


@pytest.mark.asyncio
async def test_tracking_scheduler_waiting_fulfillment_watchdog_retries_search(mocker):
    app = _build_application(mocker)
    item_id = _create_movie_item(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        availability_date=date(2026, 6, 1),
    )
    item = tracking_manager.get_tracking_item(app, item_id)
    assert item is not None
    item["status"] = "waiting_fulfillment"
    item["next_check_at_utc"] = "2026-06-01T12:00:00Z"

    await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )

    current = tracking_manager.get_tracking_item(app, item_id)
    assert current is not None
    assert current["status"] == "searching"
    assert current["next_check_at_utc"] == "2026-06-01T13:00:00Z"


@pytest.mark.asyncio
async def test_tracking_scheduler_tv_search_queue_success_stores_pending_episode(mocker):
    app = _build_application(mocker)
    item_id = _create_tv_item(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )

    resolve_mock = mocker.patch(
        "telegram_bot.services.tracking.targets.tv_ongoing.tv_next_episode.resolve_next_ongoing_episode",
        AsyncMock(
            return_value={
                "canonical_title": "Future Show",
                "tmdb_series_id": 1234,
                "state": "search_now",
                "next_episode": {
                    "season": 1,
                    "episode": 2,
                    "title": "Pilot Part II",
                    "air_date": date(2026, 5, 31),
                },
                "next_air_date": date(2026, 5, 31),
            }
        ),
    )
    mocker.patch(
        "telegram_bot.services.tracking.targets.tv_ongoing.plex_service.get_existing_episodes_for_season",
        AsyncMock(return_value=set()),
    )
    mocker.patch(
        "telegram_bot.services.tracking.scheduler.orchestrate_searches",
        AsyncMock(
            return_value=[
                {
                    "title": "Future Show S01E02 1080p WEB",
                    "page_url": "magnet:?xt=urn:btih:tv1080",
                    "info_url": "https://example.invalid/tv",
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

    resolve_mock.assert_awaited_once()
    queue_mock.assert_awaited_once()
    current = tracking_manager.get_tracking_item(app, item_id)
    assert current is not None
    assert current["status"] == "waiting_fulfillment"
    assert current["target_payload"]["pending_episode"] == {"season": 1, "episode": 2}
    assert current["target_payload"]["pending_episode_title"] == "Pilot Part II"


@pytest.mark.asyncio
async def test_tracking_scheduler_tv_queue_failure_keeps_pending_episode_and_retries_hourly(mocker):
    app = _build_application(mocker)
    item_id = _create_tv_item(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )

    mocker.patch(
        "telegram_bot.services.tracking.targets.tv_ongoing.tv_next_episode.resolve_next_ongoing_episode",
        AsyncMock(
            return_value={
                "canonical_title": "Future Show",
                "tmdb_series_id": 1234,
                "state": "search_now",
                "next_episode": {
                    "season": 1,
                    "episode": 3,
                    "title": "Third Episode",
                    "air_date": date(2026, 5, 31),
                },
                "next_air_date": date(2026, 5, 31),
            }
        ),
    )
    mocker.patch(
        "telegram_bot.services.tracking.targets.tv_ongoing.plex_service.get_existing_episodes_for_season",
        AsyncMock(return_value=set()),
    )
    mocker.patch(
        "telegram_bot.services.tracking.scheduler.orchestrate_searches",
        AsyncMock(
            return_value=[
                {
                    "title": "Future Show S01E03 1080p WEB",
                    "page_url": "magnet:?xt=urn:btih:tv1080",
                    "info_url": "https://example.invalid/tv",
                    "score": 200,
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
    assert current["status"] == "searching"
    assert current["next_check_at_utc"] == "2026-06-01T13:00:00Z"
    assert current["target_payload"]["pending_episode"] == {"season": 1, "episode": 3}


@pytest.mark.asyncio
async def test_tracking_scheduler_tv_fulfillment_advances_cursor_and_returns_to_metadata(mocker):
    app = _build_application(mocker)
    item_id = _create_tv_item(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )

    item = tracking_manager.get_tracking_item(app, item_id)
    assert item is not None
    item["target_payload"]["pending_episode"] = {"season": 1, "episode": 4}
    item["status"] = "waiting_fulfillment"

    updated = tracking_manager.mark_tracking_fulfillment_success(
        app,
        item_id=item_id,
        parsed_info={"type": "tv", "season": 1, "episode": 4},
        now_utc=datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc),
    )
    assert updated is True

    current = tracking_manager.get_tracking_item(app, item_id)
    assert current is not None
    assert current["status"] == "awaiting_metadata"
    assert current["target_payload"]["episode_cursor"] == {"season": 1, "episode": 4}
    assert current["target_payload"]["pending_episode"] is None
