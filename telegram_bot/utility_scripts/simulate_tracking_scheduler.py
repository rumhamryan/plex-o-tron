from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from telegram_bot.services.tracking import manager as tracking_manager
from telegram_bot.services.tracking import scheduler as tracking_scheduler

UTC = timezone.utc
TOP_TIER_RESOLUTIONS: dict[str, int | float] = {"2160p": 5, "4k": 5, "1080p": 3}


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    item_id: str
    processed: int
    before: dict[str, Any]
    after: dict[str, Any]


def _build_application(
    *,
    resolutions: dict[str, int | float] | None = None,
) -> Any:
    if resolutions is None:
        resolutions = dict(TOP_TIER_RESOLUTIONS)

    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=700)))
    return SimpleNamespace(
        bot=bot,
        bot_data={
            "SAVE_PATHS": {"default": "/tmp"},
            "SEARCH_CONFIG": {"preferences": {"movies": {"resolutions": resolutions}}},
            "tracking_timezone": UTC,
        },
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _create_item(
    application: Any,
    *,
    now_utc: datetime,
    availability_date: date | None,
    title: str = "Future Release",
    year: int | None = 2026,
) -> str:
    created = tracking_manager.create_movie_tracking_item(
        application,
        chat_id=456,
        canonical_title=title,
        year=year,
        availability_date=availability_date,
        availability_source="streaming" if availability_date else None,
        now_utc=now_utc,
    )
    return str(created["id"])


def _snapshot(application: Any, item_id: str) -> dict[str, Any]:
    item = tracking_manager.get_tracking_item(application, item_id)
    if item is None:
        raise RuntimeError(f"Tracking item {item_id} not found.")
    return {
        "status": item.get("status"),
        "release_date_status": item.get("release_date_status"),
        "availability_date": item.get("availability_date"),
        "availability_source": item.get("availability_source"),
        "next_check_at_utc": item.get("next_check_at_utc"),
        "linked_download_message_id": item.get("linked_download_message_id"),
        "fulfillment_state": item.get("fulfillment_state"),
    }


def _print_result(result: ScenarioResult) -> None:
    print(f"\n[PASS] {result.name}")
    print(f"  item_id:   {result.item_id}")
    print(f"  processed: {result.processed}")
    print(f"  before:    {result.before}")
    print(f"  after:     {result.after}")


async def _scenario_pending_date_unknown_metadata_only() -> ScenarioResult:
    app = _build_application()
    item_id = _create_item(
        app,
        now_utc=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        availability_date=None,
    )
    item = tracking_manager.get_tracking_item(app, item_id)
    if item is None:
        raise RuntimeError("Missing tracking item.")
    item["next_check_at_utc"] = "2026-06-01T09:00:00Z"

    before = _snapshot(app, item_id)
    resolve_mock = AsyncMock(
        return_value={
            "title": "Future Release",
            "canonical_title": "Future Release",
            "year": 2026,
            "is_released": False,
            "release_date_status": "unknown",
            "availability_date": None,
            "availability_source": None,
        }
    )
    orchestrate_mock = AsyncMock(return_value=[])

    with (
        patch(
            "telegram_bot.services.tracking.scheduler.movie_release_dates.resolve_movie_tracking_target",
            resolve_mock,
        ),
        patch("telegram_bot.services.tracking.scheduler.orchestrate_searches", orchestrate_mock),
    ):
        processed = await tracking_scheduler.run_tracking_scheduler_tick(
            app,
            now_utc=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
        )

    after = _snapshot(app, item_id)
    assert processed == 1
    assert resolve_mock.await_count == 1
    assert orchestrate_mock.await_count == 0
    assert after["status"] == "pending_date"
    assert after["release_date_status"] == "unknown"
    assert after["next_check_at_utc"] == "2026-06-08T10:00:00Z"
    return ScenarioResult("pending_date_unknown_metadata_only", item_id, processed, before, after)


async def _scenario_pending_date_resolves_future_window() -> ScenarioResult:
    app = _build_application()
    item_id = _create_item(
        app,
        now_utc=datetime(2026, 6, 1, 9, 0, tzinfo=UTC),
        availability_date=None,
    )
    item = tracking_manager.get_tracking_item(app, item_id)
    if item is None:
        raise RuntimeError("Missing tracking item.")
    item["next_check_at_utc"] = "2026-06-01T09:00:00Z"

    before = _snapshot(app, item_id)
    resolve_mock = AsyncMock(
        return_value={
            "title": "Future Release",
            "canonical_title": "Future Release",
            "year": 2026,
            "is_released": False,
            "release_date_status": "confirmed",
            "availability_date": date(2026, 6, 10),
            "availability_source": "physical",
        }
    )
    orchestrate_mock = AsyncMock(return_value=[])

    with (
        patch(
            "telegram_bot.services.tracking.scheduler.movie_release_dates.resolve_movie_tracking_target",
            resolve_mock,
        ),
        patch("telegram_bot.services.tracking.scheduler.orchestrate_searches", orchestrate_mock),
    ):
        processed = await tracking_scheduler.run_tracking_scheduler_tick(
            app,
            now_utc=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
        )

    after = _snapshot(app, item_id)
    assert processed == 1
    assert resolve_mock.await_count == 1
    assert orchestrate_mock.await_count == 0
    assert after["status"] == "waiting_release_window"
    assert after["availability_date"] == "2026-06-10"
    assert after["availability_source"] == "physical"
    assert after["next_check_at_utc"] == "2026-06-10T12:00:00Z"
    return ScenarioResult("pending_date_resolves_future_window", item_id, processed, before, after)


async def _scenario_release_day_noon_gate() -> ScenarioResult:
    app = _build_application()
    item_id = _create_item(
        app,
        now_utc=datetime(2026, 6, 1, 11, 0, tzinfo=UTC),
        availability_date=date(2026, 6, 1),
    )
    before = _snapshot(app, item_id)

    orchestrate_mock = AsyncMock(return_value=[])

    with patch("telegram_bot.services.tracking.scheduler.orchestrate_searches", orchestrate_mock):
        processed_before_noon = await tracking_scheduler.run_tracking_scheduler_tick(
            app,
            now_utc=datetime(2026, 6, 1, 11, 59, tzinfo=UTC),
        )
        mid = _snapshot(app, item_id)
        processed_at_noon = await tracking_scheduler.run_tracking_scheduler_tick(
            app,
            now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )

    after = _snapshot(app, item_id)
    assert processed_before_noon == 0
    assert processed_at_noon == 1
    assert orchestrate_mock.await_count == 1
    assert mid["status"] == "waiting_release_window"
    assert mid["next_check_at_utc"] == "2026-06-01T12:00:00Z"
    assert after["status"] == "watching_release"
    assert after["next_check_at_utc"] == "2026-06-01T13:00:00Z"
    return ScenarioResult("release_day_noon_gate", item_id, processed_at_noon, before, after)


async def _scenario_no_top_tier_hourly_retry() -> ScenarioResult:
    app = _build_application()
    item_id = _create_item(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        availability_date=date(2026, 6, 1),
    )
    before = _snapshot(app, item_id)

    orchestrate_mock = AsyncMock(
        return_value=[
            {
                "title": "Future Release 2026 1080p WEB",
                "page_url": "magnet:?xt=urn:btih:1080",
                "info_url": "https://example.invalid/1080",
                "score": 200,
            }
        ]
    )
    queue_mock = AsyncMock(return_value=(True, 1))

    with (
        patch("telegram_bot.services.tracking.scheduler.orchestrate_searches", orchestrate_mock),
        patch("telegram_bot.services.tracking.scheduler.queue_download_source", queue_mock),
    ):
        processed = await tracking_scheduler.run_tracking_scheduler_tick(
            app,
            now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )

    after = _snapshot(app, item_id)
    assert processed == 1
    assert orchestrate_mock.await_count == 1
    assert queue_mock.await_count == 0
    assert after["status"] == "watching_release"
    assert after["next_check_at_utc"] == "2026-06-01T13:00:00Z"
    return ScenarioResult("no_top_tier_hourly_retry", item_id, processed, before, after)


async def _scenario_top_tier_queue_success() -> ScenarioResult:
    app = _build_application()
    item_id = _create_item(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        availability_date=date(2026, 6, 1),
    )
    before = _snapshot(app, item_id)

    orchestrate_mock = AsyncMock(
        return_value=[
            {
                "title": "Future Release 2026 1080p WEB",
                "page_url": "magnet:?xt=urn:btih:1080",
                "info_url": "https://example.invalid/1080",
                "score": 300,
            },
            {
                "title": "Future Release 2026 2160p WEB",
                "page_url": "magnet:?xt=urn:btih:2160",
                "info_url": "https://example.invalid/2160",
                "score": 99,
            },
        ]
    )
    queue_mock = AsyncMock(return_value=(True, 1))

    with (
        patch("telegram_bot.services.tracking.scheduler.orchestrate_searches", orchestrate_mock),
        patch("telegram_bot.services.tracking.scheduler.queue_download_source", queue_mock),
    ):
        processed = await tracking_scheduler.run_tracking_scheduler_tick(
            app,
            now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )

    after = _snapshot(app, item_id)
    assert processed == 1
    assert orchestrate_mock.await_count == 1
    assert queue_mock.await_count == 1
    await_args = queue_mock.await_args
    assert await_args is not None
    queued_source = await_args.kwargs["source_dict"]
    assert queued_source["value"] == "magnet:?xt=urn:btih:2160"
    assert after["status"] == "waiting_fulfillment"
    assert after["next_check_at_utc"] is None
    assert after["linked_download_message_id"] == 700
    return ScenarioResult("top_tier_queue_success", item_id, processed, before, after)


async def _scenario_queue_failure_hourly_retry() -> ScenarioResult:
    app = _build_application()
    item_id = _create_item(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        availability_date=date(2026, 6, 1),
    )
    before = _snapshot(app, item_id)

    orchestrate_mock = AsyncMock(
        return_value=[
            {
                "title": "Future Release 2026 2160p WEB",
                "page_url": "magnet:?xt=urn:btih:2160",
                "info_url": "https://example.invalid/2160",
                "score": 99,
            }
        ]
    )
    queue_mock = AsyncMock(side_effect=RuntimeError("intentional queue failure"))

    with (
        patch("telegram_bot.services.tracking.scheduler.orchestrate_searches", orchestrate_mock),
        patch("telegram_bot.services.tracking.scheduler.queue_download_source", queue_mock),
    ):
        processed = await tracking_scheduler.run_tracking_scheduler_tick(
            app,
            now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )

    after = _snapshot(app, item_id)
    assert processed == 1
    assert orchestrate_mock.await_count == 1
    assert queue_mock.await_count == 1
    assert after["status"] == "watching_release"
    assert after["next_check_at_utc"] == "2026-06-01T13:00:00Z"
    return ScenarioResult("queue_failure_hourly_retry", item_id, processed, before, after)


async def _scenario_terminal_state_skip() -> ScenarioResult:
    app = _build_application()
    item_id = _create_item(
        app,
        now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        availability_date=date(2026, 6, 1),
    )
    item = tracking_manager.get_tracking_item(app, item_id)
    if item is None:
        raise RuntimeError("Missing tracking item.")
    item["status"] = "waiting_fulfillment"
    item["next_check_at_utc"] = _iso(datetime(2026, 6, 1, 10, 0, tzinfo=UTC))
    before = _snapshot(app, item_id)

    orchestrate_mock = AsyncMock(return_value=[])
    with patch("telegram_bot.services.tracking.scheduler.orchestrate_searches", orchestrate_mock):
        processed = await tracking_scheduler.run_tracking_scheduler_tick(
            app,
            now_utc=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )

    after = _snapshot(app, item_id)
    assert processed == 0
    assert orchestrate_mock.await_count == 0
    assert after["status"] == "waiting_fulfillment"
    return ScenarioResult("terminal_state_skip", item_id, processed, before, after)


SCENARIOS = {
    "pending_date_unknown_metadata_only": _scenario_pending_date_unknown_metadata_only,
    "pending_date_resolves_future_window": _scenario_pending_date_resolves_future_window,
    "release_day_noon_gate": _scenario_release_day_noon_gate,
    "no_top_tier_hourly_retry": _scenario_no_top_tier_hourly_retry,
    "top_tier_queue_success": _scenario_top_tier_queue_success,
    "queue_failure_hourly_retry": _scenario_queue_failure_hourly_retry,
    "terminal_state_skip": _scenario_terminal_state_skip,
}


async def _run_selected_scenarios(selected: list[str], *, pause_between: bool) -> int:
    executed = 0
    with (
        patch(
            "telegram_bot.services.tracking.manager.persist_tracking_state_from_bot_data",
            return_value=None,
        ),
        patch(
            "telegram_bot.services.tracking.scheduler.persist_tracking_state_from_bot_data",
            return_value=None,
        ),
    ):
        for index, name in enumerate(selected):
            runner = SCENARIOS[name]
            result = await runner()
            _print_result(result)
            executed += 1

            has_remaining = index < len(selected) - 1
            if pause_between and has_remaining:
                if not _await_continue_or_stop(result.name):
                    break

    return executed


def _await_continue_or_stop(last_scenario: str) -> bool:
    if not sys.stdin.isatty():
        print(f"\n[INFO] Skipping pause after '{last_scenario}' because stdin is not interactive.")
        return True

    while True:
        choice = input(
            f"\nReview complete for '{last_scenario}'. Press Enter to continue, or 'q' to stop: "
        ).strip()
        if not choice:
            return True
        if choice.lower() in {"q", "quit", "exit"}:
            print("Stopping simulation run at user request.")
            return False
        print("Invalid choice. Press Enter to continue or type 'q' to stop.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run deterministic scheduler simulations to validate tracking state transitions "
            "without hitting external metadata/search services."
        )
    )
    parser.add_argument(
        "--scenario",
        choices=["all", *SCENARIOS.keys()],
        default="all",
        help="Which simulation scenario to run.",
    )
    parser.add_argument(
        "--no-pause",
        action="store_true",
        help="Run selected scenarios without waiting for confirmation between them.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    selected = list(SCENARIOS.keys()) if args.scenario == "all" else [str(args.scenario)]
    pause_between = len(selected) > 1 and not args.no_pause
    executed = asyncio.run(_run_selected_scenarios(selected, pause_between=pause_between))
    print(f"\nCompleted {executed} scenario(s) successfully.")


if __name__ == "__main__":
    main()
