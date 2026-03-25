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
async def test_tracking_startup_reconciliation_nudges_release_window_and_tv_metadata_items(mocker):
    app = _build_application(mocker)
    movie_release_id = _create_movie_item(
        app,
        now_utc=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        availability_date=date(2026, 6, 1),
    )
    movie_release = tracking_manager.get_tracking_item(app, movie_release_id)
    assert movie_release is not None
    movie_release["status"] = "awaiting_window"
    movie_release["next_check_at_utc"] = "2026-06-08T12:00:00Z"

    movie_unknown_id = _create_movie_item(
        app,
        now_utc=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        availability_date=None,
        title="Unknown Release",
    )

    tv_window_id = _create_tv_item(
        app,
        now_utc=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        title="Window Show",
        tmdb_series_id=9876,
    )
    tv_window = tracking_manager.get_tracking_item(app, tv_window_id)
    assert tv_window is not None
    tv_window["status"] = "awaiting_window"
    tv_window["next_check_at_utc"] = "2026-06-08T12:00:00Z"
    tv_window["target_payload"]["pending_episode_air_date"] = "2026-06-01"

    tv_metadata_id = _create_tv_item(
        app,
        now_utc=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        title="Metadata Show",
        tmdb_series_id=5555,
    )
    tv_metadata = tracking_manager.get_tracking_item(app, tv_metadata_id)
    assert tv_metadata is not None
    tv_metadata["status"] = "awaiting_metadata"
    tv_metadata["next_check_at_utc"] = "2026-06-08T12:00:00Z"
    tv_metadata["target_payload"]["pending_episode_air_date"] = None

    nudged = tracking_scheduler.reconcile_tracking_items_on_startup(
        app,
        now_utc=datetime(2026, 6, 1, 12, 5, tzinfo=timezone.utc),
    )

    assert nudged == 3
    assert tracking_manager.get_tracking_item(app, movie_release_id)["next_check_at_utc"] == (
        "2026-06-01T12:05:00Z"
    )
    assert tracking_manager.get_tracking_item(app, tv_window_id)["next_check_at_utc"] == (
        "2026-06-01T12:05:00Z"
    )
    assert tracking_manager.get_tracking_item(app, tv_metadata_id)["next_check_at_utc"] == (
        "2026-06-01T12:05:00Z"
    )
    assert tracking_manager.get_tracking_item(app, movie_unknown_id)["next_check_at_utc"] == (
        "2026-06-08T09:00:00Z"
    )


@pytest.mark.asyncio
async def test_tracking_startup_reconciliation_recovers_orphaned_search_states(mocker):
    app = _build_application(mocker)
    orphan_id = _create_tv_item(
        app,
        now_utc=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        title="Orphan Show",
        tmdb_series_id=3001,
    )
    orphan_item = tracking_manager.get_tracking_item(app, orphan_id)
    assert orphan_item is not None
    orphan_item["status"] = "waiting_fulfillment"
    orphan_item["next_check_at_utc"] = "2026-06-08T12:00:00Z"
    orphan_item["linked_download_message_id"] = 888

    searching_id = _create_tv_item(
        app,
        now_utc=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        title="Searching Show",
        tmdb_series_id=3002,
    )
    searching_item = tracking_manager.get_tracking_item(app, searching_id)
    assert searching_item is not None
    searching_item["status"] = "searching"
    searching_item["next_check_at_utc"] = "2026-06-08T12:00:00Z"

    protected_id = _create_tv_item(
        app,
        now_utc=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        title="Protected Show",
        tmdb_series_id=3003,
    )
    protected_item = tracking_manager.get_tracking_item(app, protected_id)
    assert protected_item is not None
    protected_item["status"] = "waiting_fulfillment"
    protected_item["next_check_at_utc"] = "2026-06-08T12:00:00Z"
    app.bot_data["active_downloads"] = {
        "456": {
            "source_dict": {"tracking_item_id": protected_id},
            "chat_id": 456,
        }
    }

    nudged = tracking_scheduler.reconcile_tracking_items_on_startup(
        app,
        now_utc=datetime(2026, 6, 1, 12, 5, tzinfo=timezone.utc),
    )

    assert nudged == 2

    orphan_current = tracking_manager.get_tracking_item(app, orphan_id)
    assert orphan_current is not None
    assert orphan_current["status"] == "searching"
    assert orphan_current["linked_download_message_id"] is None
    assert orphan_current["next_check_at_utc"] == "2026-06-01T12:05:00Z"

    searching_current = tracking_manager.get_tracking_item(app, searching_id)
    assert searching_current is not None
    assert searching_current["status"] == "searching"
    assert searching_current["next_check_at_utc"] == "2026-06-01T12:05:00Z"

    protected_current = tracking_manager.get_tracking_item(app, protected_id)
    assert protected_current is not None
    assert protected_current["status"] == "waiting_fulfillment"
    assert protected_current["next_check_at_utc"] == "2026-06-08T12:00:00Z"


@pytest.mark.asyncio
async def test_tracking_scheduler_runtime_does_not_force_catchup_for_drifted_next_check(mocker):
    app = _build_application(mocker)
    item_id = _create_movie_item(
        app,
        now_utc=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        availability_date=date(2026, 6, 1),
    )
    item = tracking_manager.get_tracking_item(app, item_id)
    assert item is not None
    item["status"] = "awaiting_window"
    item["next_check_at_utc"] = "2026-06-08T12:00:00Z"

    orchestrate_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.orchestrate_searches",
        AsyncMock(return_value=[]),
    )
    processed = await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )

    assert processed == 0
    orchestrate_mock.assert_not_awaited()


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


@pytest.mark.asyncio
async def test_tracking_scheduler_tv_transient_metadata_failure_preserves_window_and_searches(
    mocker,
):
    app = _build_application(mocker)
    item_id = _create_tv_item(
        app,
        now_utc=datetime(2026, 6, 1, 11, 0, tzinfo=timezone.utc),
    )
    item = tracking_manager.get_tracking_item(app, item_id)
    assert item is not None
    item["status"] = "awaiting_window"
    item["next_check_at_utc"] = "2026-06-01T12:00:00Z"
    item["target_payload"]["pending_episode"] = {"season": 1, "episode": 5}
    item["target_payload"]["pending_episode_title"] = "Episode Five"
    item["target_payload"]["pending_episode_air_date"] = "2026-06-01"

    resolve_mock = mocker.patch(
        "telegram_bot.services.tracking.targets.tv_ongoing.tv_next_episode.resolve_next_ongoing_episode",
        AsyncMock(
            return_value={
                "canonical_title": "Future Show",
                "tmdb_series_id": 1234,
                "state": "awaiting_metadata",
                "next_episode": None,
                "next_air_date": None,
                "metadata_refresh_failed": True,
            }
        ),
    )
    orchestrate_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.orchestrate_searches",
        AsyncMock(
            return_value=[
                {
                    "title": "Future Show S01E05 1080p WEB",
                    "page_url": "magnet:?xt=urn:btih:tv105",
                    "info_url": "https://example.invalid/tv105",
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
    orchestrate_mock.assert_awaited_once()
    queue_mock.assert_awaited_once()
    current = tracking_manager.get_tracking_item(app, item_id)
    assert current is not None
    assert current["status"] == "waiting_fulfillment"
    assert current["target_payload"]["pending_episode"] == {"season": 1, "episode": 5}
    assert current["target_payload"]["pending_episode_title"] == "Episode Five"
    assert current["target_payload"]["pending_episode_air_date"] == "2026-06-01"


@pytest.mark.asyncio
async def test_tracking_scheduler_tv_transient_metadata_failure_without_window_retries_hourly(
    mocker,
):
    app = _build_application(mocker)
    item_id = _create_tv_item(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )
    item = tracking_manager.get_tracking_item(app, item_id)
    assert item is not None
    item["status"] = "awaiting_metadata"
    item["next_check_at_utc"] = "2026-06-01T12:00:00Z"
    item["target_payload"]["pending_episode"] = None
    item["target_payload"]["pending_episode_title"] = None
    item["target_payload"]["pending_episode_air_date"] = None

    resolve_mock = mocker.patch(
        "telegram_bot.services.tracking.targets.tv_ongoing.tv_next_episode.resolve_next_ongoing_episode",
        AsyncMock(
            return_value={
                "canonical_title": "Future Show",
                "tmdb_series_id": 1234,
                "state": "awaiting_metadata",
                "next_episode": None,
                "next_air_date": None,
                "metadata_refresh_failed": True,
            }
        ),
    )
    orchestrate_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.orchestrate_searches",
        AsyncMock(return_value=[]),
    )

    await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )

    resolve_mock.assert_awaited_once()
    orchestrate_mock.assert_not_awaited()
    current = tracking_manager.get_tracking_item(app, item_id)
    assert current is not None
    assert current["status"] == "awaiting_metadata"
    assert current["next_check_at_utc"] == "2026-06-01T13:00:00Z"


@pytest.mark.asyncio
async def test_tracking_scheduler_tv_relaxes_seeders_when_initial_search_is_empty(mocker):
    app = _build_application(mocker)
    item_id = _create_tv_item(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )
    item = tracking_manager.get_tracking_item(app, item_id)
    assert item is not None
    item["status"] = "searching"
    item["next_check_at_utc"] = "2026-06-01T12:00:00Z"
    item["target_payload"]["pending_episode"] = {"season": 2, "episode": 1}
    item["target_payload"]["pending_episode_title"] = "Episode One"
    item["target_payload"]["pending_episode_air_date"] = "2026-06-01"

    orchestrate_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.orchestrate_searches",
        AsyncMock(
            side_effect=[
                [],
                [
                    {
                        "title": "Future Show S02E01 1080p WEB",
                        "page_url": "magnet:?xt=urn:btih:tv201",
                        "info_url": "https://example.invalid/tv201",
                        "score": 9,
                        "seeders": 4,
                    }
                ],
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

    assert orchestrate_mock.await_count == 2
    first_call = orchestrate_mock.await_args_list[0]
    second_call = orchestrate_mock.await_args_list[1]
    assert "min_seeders" not in first_call.kwargs
    assert second_call.kwargs.get("min_seeders") == 0
    queue_mock.assert_awaited_once()
    current = tracking_manager.get_tracking_item(app, item_id)
    assert current is not None
    assert current["status"] == "waiting_fulfillment"


@pytest.mark.asyncio
async def test_tracking_scheduler_tv_queue_uses_tmdb_episode_title_override(mocker):
    app = _build_application(mocker)
    item_id = _create_tv_item(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )
    item = tracking_manager.get_tracking_item(app, item_id)
    assert item is not None
    item["status"] = "searching"
    item["next_check_at_utc"] = "2026-06-01T12:00:00Z"
    item["target_payload"]["pending_episode"] = {"season": 2, "episode": 1}
    item["target_payload"]["pending_episode_title"] = "Old Title"
    item["target_payload"]["pending_episode_air_date"] = "2026-06-01"

    mocker.patch(
        "telegram_bot.services.tracking.scheduler.orchestrate_searches",
        AsyncMock(
            return_value=[
                {
                    "title": "Future Show S02E01 1080p WEB",
                    "page_url": "magnet:?xt=urn:btih:tv201",
                    "info_url": "https://example.invalid/tv201",
                    "score": 9,
                }
            ]
        ),
    )
    title_lookup_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.tv_next_episode.fetch_episode_title_for_tmdb_episode",
        AsyncMock(return_value="The Northern Star"),
    )
    queue_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.queue_download_source",
        AsyncMock(return_value=(True, 1)),
    )

    await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )

    title_lookup_mock.assert_awaited_once_with(tmdb_series_id=1234, season=2, episode=1)
    queue_mock.assert_awaited_once()
    source_dict = queue_mock.await_args.kwargs["source_dict"]
    assert source_dict["parsed_info"]["episode_title"] == "The Northern Star"

    current = tracking_manager.get_tracking_item(app, item_id)
    assert current is not None
    assert current["status"] == "waiting_fulfillment"
    assert current["target_payload"]["pending_episode_title"] == "The Northern Star"


@pytest.mark.asyncio
async def test_tracking_scheduler_tv_queue_keeps_existing_title_when_lookup_fails(mocker):
    app = _build_application(mocker)
    item_id = _create_tv_item(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )
    item = tracking_manager.get_tracking_item(app, item_id)
    assert item is not None
    item["status"] = "searching"
    item["next_check_at_utc"] = "2026-06-01T12:00:00Z"
    item["target_payload"]["pending_episode"] = {"season": 2, "episode": 1}
    item["target_payload"]["pending_episode_title"] = "Old Title"
    item["target_payload"]["pending_episode_air_date"] = "2026-06-01"

    mocker.patch(
        "telegram_bot.services.tracking.scheduler.orchestrate_searches",
        AsyncMock(
            return_value=[
                {
                    "title": "Future Show S02E01 1080p WEB",
                    "page_url": "magnet:?xt=urn:btih:tv201",
                    "info_url": "https://example.invalid/tv201",
                    "score": 9,
                }
            ]
        ),
    )
    mocker.patch(
        "telegram_bot.services.tracking.scheduler.tv_next_episode.fetch_episode_title_for_tmdb_episode",
        AsyncMock(return_value=None),
    )
    queue_mock = mocker.patch(
        "telegram_bot.services.tracking.scheduler.queue_download_source",
        AsyncMock(return_value=(True, 1)),
    )

    await tracking_scheduler.run_tracking_scheduler_tick(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
    )

    queue_mock.assert_awaited_once()
    source_dict = queue_mock.await_args.kwargs["source_dict"]
    assert source_dict["parsed_info"]["episode_title"] == "Old Title"

    current = tracking_manager.get_tracking_item(app, item_id)
    assert current is not None
    assert current["status"] == "waiting_fulfillment"
    assert current["target_payload"]["pending_episode_title"] == "Old Title"
