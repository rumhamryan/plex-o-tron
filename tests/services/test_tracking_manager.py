from datetime import date, datetime, timezone
import json
from types import SimpleNamespace

from telegram_bot.services.tracking import manager as tracking_manager
from telegram_bot.services.tracking.persistence import save_tracking_state


def _build_application(*, allowed_user_ids: list[int] | None = None):
    bot_data: dict[str, object] = {"tracking_timezone": timezone.utc}
    if allowed_user_ids is not None:
        bot_data["ALLOWED_USER_IDS"] = allowed_user_ids
    return SimpleNamespace(bot_data=bot_data)


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


def test_create_tv_tracking_item_reuses_existing_active_identity(mocker):
    application = _build_application()
    persist_mock = mocker.patch(
        "telegram_bot.services.tracking.manager.persist_tracking_state_from_bot_data",
        return_value=None,
    )

    first = tracking_manager.create_tv_tracking_item(
        application,
        chat_id=456,
        canonical_title="Rick and Morty",
        tmdb_series_id=60625,
        now_utc=datetime(2026, 3, 23, 0, 0, tzinfo=timezone.utc),
    )
    second = tracking_manager.create_tv_tracking_item(
        application,
        chat_id=456,
        canonical_title="Rick and Morty",
        tmdb_series_id=60625,
        now_utc=datetime(2026, 3, 23, 0, 5, tzinfo=timezone.utc),
    )

    assert second["id"] == first["id"]
    assert len(tracking_manager.get_tracking_items(application.bot_data)) == 1
    assert persist_mock.call_count == 1


def test_create_movie_tracking_item_stores_collection_metadata(mocker):
    application = _build_application()
    mocker.patch(
        "telegram_bot.services.tracking.manager.persist_tracking_state_from_bot_data",
        return_value=None,
    )

    created = tracking_manager.create_movie_tracking_item(
        application,
        chat_id=456,
        canonical_title="Avatar 3",
        year=2027,
        availability_date=date(2027, 12, 18),
        availability_source="streaming",
        collection_name="Avatar",
        collection_fs_name="Avatar",
        collection_movies=[
            {"title": "Avatar", "year": 2009},
            {"title": "Avatar: The Way of Water", "year": 2022},
        ],
        now_utc=datetime(2026, 3, 23, 0, 0, tzinfo=timezone.utc),
    )

    payload = created["target_payload"]
    assert payload.get("collection_name") == "Avatar"
    assert payload.get("collection_fs_name") == "Avatar"
    assert payload.get("collection_movies") == [
        {"title": "Avatar", "year": 2009},
        {"title": "Avatar: The Way of Water", "year": 2022},
    ]


def test_load_tracking_state_prunes_unauthorized_and_duplicate_items(tmp_path):
    state_file = tmp_path / "tracking_state.json"
    payload = {
        "version": 2,
        "items": {
            "trk_older": {
                "id": "trk_older",
                "chat_id": 777,
                "target_kind": "tv",
                "schedule_mode": "ongoing_next_episode",
                "target_identity": "tv:tmdb:60625",
                "display_title": "Rick and Morty",
                "status": "searching",
                "next_check_at_utc": "2026-03-23T04:00:00Z",
                "last_checked_at_utc": "2026-03-23T03:59:00Z",
                "created_at_utc": "2026-03-23T03:00:00Z",
                "target_payload": {
                    "canonical_title": "Rick and Morty",
                    "tmdb_series_id": 60625,
                    "episode_cursor": {"season": 2, "episode": 1},
                    "pending_episode": {"season": 2, "episode": 2},
                },
                "retry": {"consecutive_failures": 0, "last_error": None},
            },
            "trk_newer_duplicate": {
                "id": "trk_newer_duplicate",
                "chat_id": 777,
                "target_kind": "tv",
                "schedule_mode": "ongoing_next_episode",
                "target_identity": "tv:tmdb:60625",
                "display_title": "Rick and Morty",
                "status": "awaiting_metadata",
                "next_check_at_utc": "2026-03-23T04:00:00Z",
                "last_checked_at_utc": "2026-03-23T04:00:00Z",
                "created_at_utc": "2026-03-23T03:10:00Z",
                "target_payload": {
                    "canonical_title": "Rick and Morty",
                    "tmdb_series_id": 60625,
                    "episode_cursor": {"season": 2, "episode": 1},
                    "pending_episode": None,
                },
                "retry": {"consecutive_failures": 0, "last_error": None},
            },
            "trk_unauthorized": {
                "id": "trk_unauthorized",
                "chat_id": 456,
                "target_kind": "tv",
                "schedule_mode": "ongoing_next_episode",
                "target_identity": "tv:tmdb:1234",
                "display_title": "Future Show",
                "status": "searching",
                "next_check_at_utc": "2026-03-23T04:00:00Z",
                "last_checked_at_utc": "2026-03-23T03:59:00Z",
                "created_at_utc": "2026-03-23T03:20:00Z",
                "target_payload": {
                    "canonical_title": "Future Show",
                    "tmdb_series_id": 1234,
                    "episode_cursor": None,
                    "pending_episode": {"season": 1, "episode": 3},
                },
                "retry": {"consecutive_failures": 0, "last_error": None},
            },
        },
    }
    state_file.write_text(json.dumps(payload), encoding="utf-8")

    application = _build_application(allowed_user_ids=[777])
    loaded = tracking_manager.load_tracking_state_into_bot_data(
        application,
        file_path=str(state_file),
    )

    assert set(loaded.keys()) == {"trk_older"}
    assert loaded["trk_older"]["target_identity"] == "tv:tmdb:60625"

    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert set(persisted["items"].keys()) == {"trk_older"}
