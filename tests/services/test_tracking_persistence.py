from __future__ import annotations

import json

from telegram_bot.services.tracking.persistence import load_tracking_state, save_tracking_state


def test_load_tracking_state_empty_file_returns_empty(tmp_path):
    state_file = tmp_path / "tracking_state.json"
    state_file.write_text("", encoding="utf-8")

    loaded = load_tracking_state(str(state_file))
    assert loaded == {}


def test_load_tracking_state_migrates_v1_movie_item_to_v2(tmp_path):
    state_file = tmp_path / "tracking_state.json"
    payload = {
        "version": 1,
        "items": {
            "trk_legacy": {
                "id": "trk_legacy",
                "chat_id": 456,
                "target_kind": "movie",
                "target_identity": "movie:future movie:2030",
                "title": "Future Movie",
                "canonical_title": "Future Movie",
                "year": 2030,
                "release_date_status": "confirmed",
                "availability_date": "2030-01-02",
                "availability_source": "streaming",
                "status": "watching_release",
                "next_check_at_utc": "2026-03-23T12:00:00Z",
                "last_checked_at_utc": None,
                "created_at_utc": "2026-03-23T00:00:00Z",
                "fulfilled_at_utc": None,
                "fulfillment_state": "pending",
                "linked_download_message_id": None,
            }
        },
    }
    state_file.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_tracking_state(str(state_file))
    assert list(loaded) == ["trk_legacy"]
    item = loaded["trk_legacy"]
    assert item["target_kind"] == "movie"
    assert item["schedule_mode"] == "future_release"
    assert item["status"] == "searching"
    assert item["display_title"] == "Future Movie"
    assert item["target_payload"]["canonical_title"] == "Future Movie"
    assert item["target_payload"]["year"] == 2030
    assert item["target_payload"]["release_date_status"] == "confirmed"
    assert item["target_payload"]["availability_date"] == "2030-01-02"
    assert item["target_payload"]["availability_source"] == "streaming"
    assert item["retry"]["consecutive_failures"] == 0
    assert item["retry"]["last_error"] is None


def test_load_tracking_state_skips_malformed_items(tmp_path):
    state_file = tmp_path / "tracking_state.json"
    payload = {
        "version": 1,
        "items": {
            "valid": {
                "id": "valid",
                "chat_id": 456,
                "title": "Future Movie",
                "canonical_title": "Future Movie",
                "created_at_utc": "2026-03-23T00:00:00Z",
            },
            "bad_non_dict": "oops",
            "bad_missing_created": {
                "id": "bad_missing_created",
                "chat_id": 456,
                "title": "No Timestamp",
                "canonical_title": "No Timestamp",
            },
        },
    }
    state_file.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_tracking_state(str(state_file))
    assert list(loaded) == ["valid"]


def test_save_and_load_tracking_state_v2_roundtrip(tmp_path):
    state_file = tmp_path / "tracking_state.json"
    items = {
        "trk_movie": {
            "id": "trk_movie",
            "chat_id": 456,
            "target_kind": "movie",
            "schedule_mode": "future_release",
            "target_identity": "movie:future movie:2030",
            "display_title": "Future Movie",
            "status": "awaiting_window",
            "next_check_at_utc": "2030-01-02T12:00:00Z",
            "last_checked_at_utc": "2026-03-23T00:00:00Z",
            "created_at_utc": "2026-03-23T00:00:00Z",
            "fulfilled_at_utc": None,
            "linked_download_message_id": None,
            "target_payload": {
                "canonical_title": "Future Movie",
                "year": 2030,
                "release_date_status": "confirmed",
                "availability_date": "2030-01-02",
                "availability_source": "streaming",
            },
            "retry": {"consecutive_failures": 1, "last_error": "network"},
        },
        "trk_tv": {
            "id": "trk_tv",
            "chat_id": 456,
            "target_kind": "tv",
            "schedule_mode": "ongoing_next_episode",
            "target_identity": "tv:tmdb:1234",
            "display_title": "Future Show",
            "status": "searching",
            "next_check_at_utc": "2026-03-23T01:00:00Z",
            "last_checked_at_utc": "2026-03-23T00:00:00Z",
            "created_at_utc": "2026-03-23T00:00:00Z",
            "fulfilled_at_utc": None,
            "linked_download_message_id": None,
            "target_payload": {
                "canonical_title": "Future Show",
                "tmdb_series_id": 1234,
                "episode_cursor": {"season": 1, "episode": 3},
                "pending_episode": {"season": 1, "episode": 4},
                "pending_episode_title": "Fourth Episode",
            },
            "retry": {"consecutive_failures": 0, "last_error": None},
        },
    }

    save_tracking_state(str(state_file), items)
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    assert raw["version"] == 2

    loaded = load_tracking_state(str(state_file))
    assert set(loaded) == {"trk_movie", "trk_tv"}
    assert loaded["trk_movie"]["target_payload"]["year"] == 2030
    assert loaded["trk_tv"]["target_payload"]["tmdb_series_id"] == 1234
    assert loaded["trk_tv"]["target_payload"]["pending_episode"] == {"season": 1, "episode": 4}


def test_save_tracking_state_creates_v1_backup_once_before_first_v2_write(tmp_path):
    state_file = tmp_path / "tracking_state.json"
    backup_file = tmp_path / "tracking_state.v1.bak"

    v1_payload = {
        "version": 1,
        "items": {
            "trk_legacy": {
                "id": "trk_legacy",
                "chat_id": 456,
                "title": "Future Movie",
                "canonical_title": "Future Movie",
                "created_at_utc": "2026-03-23T00:00:00Z",
            }
        },
    }
    state_file.write_text(json.dumps(v1_payload), encoding="utf-8")

    loaded = load_tracking_state(str(state_file))
    assert backup_file.exists() is False

    save_tracking_state(str(state_file), loaded)
    assert backup_file.exists() is True
    backup_payload = json.loads(backup_file.read_text(encoding="utf-8"))
    assert backup_payload["version"] == 1

    # A second save should keep the same backup file and write v2 state to the primary file.
    save_tracking_state(str(state_file), loaded)
    primary_payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert primary_payload["version"] == 2
