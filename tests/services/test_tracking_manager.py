from datetime import date, datetime, timezone
import json
from types import SimpleNamespace

from telegram_bot.services.tracking import manager as tracking_manager
from telegram_bot.services.tracking.persistence import save_tracking_state


def _build_application():
    return SimpleNamespace(bot_data={"tracking_timezone": timezone.utc})


def test_cancel_tracking_item_removes_item_and_in_progress(mocker):
    application = _build_application()
    persist_mock = mocker.patch(
        "telegram_bot.services.tracking.manager.persist_tracking_state_from_bot_data",
        return_value=None,
    )

    created = tracking_manager.create_movie_tracking_item(
        application,
        chat_id=456,
        canonical_title="Future Movie",
        year=2030,
        availability_date=date(2030, 1, 2),
        availability_source="streaming",
        now_utc=datetime(2026, 3, 23, 0, 0, tzinfo=timezone.utc),
    )
    item_id = created["id"]
    tracking_manager.get_tracking_in_progress_ids(application.bot_data).add(item_id)

    cancelled = tracking_manager.cancel_tracking_item(
        application,
        item_id=item_id,
        chat_id=456,
    )

    assert cancelled is True
    assert tracking_manager.get_tracking_item(application, item_id) is None
    assert item_id not in tracking_manager.get_tracking_in_progress_ids(application.bot_data)
    assert persist_mock.call_count == 2


def test_cancel_tracking_item_respects_chat_id_guard(mocker):
    application = _build_application()
    mocker.patch(
        "telegram_bot.services.tracking.manager.persist_tracking_state_from_bot_data",
        return_value=None,
    )

    created = tracking_manager.create_movie_tracking_item(
        application,
        chat_id=456,
        canonical_title="Future Movie",
        year=2030,
        availability_date=date(2030, 1, 2),
        availability_source="streaming",
        now_utc=datetime(2026, 3, 23, 0, 0, tzinfo=timezone.utc),
    )
    item_id = created["id"]

    cancelled = tracking_manager.cancel_tracking_item(
        application,
        item_id=item_id,
        chat_id=999,
    )

    assert cancelled is False
    assert tracking_manager.get_tracking_item(application, item_id) is not None


def test_cancel_tracking_item_removes_persisted_entry(mocker, tmp_path):
    application = _build_application()
    state_file = tmp_path / "tracking_state.json"

    def persist_to_tmp(app):
        save_tracking_state(str(state_file), tracking_manager.get_tracking_items(app.bot_data))

    mocker.patch(
        "telegram_bot.services.tracking.manager.persist_tracking_state_from_bot_data",
        side_effect=persist_to_tmp,
    )

    created = tracking_manager.create_movie_tracking_item(
        application,
        chat_id=456,
        canonical_title="Future Movie",
        year=2030,
        availability_date=date(2030, 1, 2),
        availability_source="streaming",
        now_utc=datetime(2026, 3, 23, 0, 0, tzinfo=timezone.utc),
    )
    item_id = created["id"]

    with state_file.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    assert item_id in payload["items"]

    cancelled = tracking_manager.cancel_tracking_item(
        application,
        item_id=item_id,
        chat_id=456,
    )
    assert cancelled is True

    with state_file.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    assert item_id not in payload["items"]
