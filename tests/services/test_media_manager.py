import sys
import os
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from telegram_bot.services.media_manager import (
    generate_plex_filename,
    parse_resolution_from_name,
    handle_successful_download,
    _get_final_destination_path,
    _trigger_plex_scan,
)
from telegram_bot.ui.messages import format_media_summary

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))


def test_generate_plex_filename_movie():
    parsed = {"type": "movie", "title": "Inception", "year": "2010"}
    assert generate_plex_filename(parsed, ".mkv") == "Inception (2010).mkv"


def test_generate_plex_filename_tv_with_episode_title():
    parsed = {
        "type": "tv",
        "title": "My Show",
        "season": 1,
        "episode": 2,
        "episode_title": "Pilot",
    }
    assert generate_plex_filename(parsed, ".mp4") == "s01e02 - Pilot.mp4"


def test_generate_plex_filename_illegal_chars():
    parsed = {"type": "movie", "title": "Bad:Title*?", "year": "2020"}
    assert generate_plex_filename(parsed, ".mp4") == "BadTitle (2020).mp4"


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Movie.2160p.BluRay", "4K"),
        ("Video.1080p.x265", "1080p"),
        ("Series.720p.HDTV", "720p"),
        ("Old.Movie.DVDRip", "SD"),
        ("Sample", "N/A"),
    ],
)
def test_parse_resolution_from_name(name, expected):
    assert parse_resolution_from_name(name) == expected


def test_format_media_summary_with_icons():
    result = format_media_summary(
        prefix="🗑️ *Removed*",
        title="Movie (2024)",
        size_label="2.5 GB",
        destination_label="/movies/Movie (2024)",
        title_icon="🎬",
        size_icon="📦",
        destination_icon="📁",
    )
    lines = result.split("\n")
    assert lines[0] == "🗑️ *Removed*"
    assert lines[1] == "🎬 Movie \\(2024\\)"
    assert lines[2] == "📦 Size: 2\\.5 GB"
    assert lines[3] == "📁 Destination: `/movies/Movie \\(2024\\)`"


def test_format_media_summary_without_optional_fields():
    result = format_media_summary(
        prefix="✅ Done",
        title="Plain Title",
    )
    assert result == "✅ Done\nPlain Title"


class DummyFiles:
    def num_files(self):
        return 1

    def file_path(self, index):
        return "Movie.mkv"


class DummyTorrent:
    def files(self):
        return DummyFiles()


@pytest.mark.asyncio
async def test_handle_successful_download(mocker):
    ti = DummyTorrent()
    parsed = {"type": "movie", "title": "Sample", "year": "2023"}
    save_paths = {"movies": "/movies", "default": "/default"}

    mocker.patch(
        "telegram_bot.services.media_manager._get_final_destination_path",
        return_value="/final",
    )
    makedirs_mock = mocker.patch("telegram_bot.services.media_manager.os.makedirs")
    move_mock = mocker.patch("telegram_bot.services.media_manager.shutil.move")
    scan_mock = mocker.patch(
        "telegram_bot.services.media_manager._trigger_plex_scan",
        return_value="scan",
    )
    size_mock = mocker.patch(
        "telegram_bot.services.media_manager._get_path_size_bytes",
        return_value=1024,
    )

    result = await handle_successful_download(
        ti,
        parsed,
        "/downloads",
        save_paths,
        {"url": "u", "token": "t"},
    )

    makedirs_mock.assert_called_once_with("/final", exist_ok=True)

    expected_source_path = os.path.join("/downloads", "Movie.mkv")
    expected_dest_path = os.path.join("/final", "Sample (2023).mkv")

    move_mock.assert_called_once_with(expected_source_path, expected_dest_path)

    scan_mock.assert_called_once()
    size_mock.assert_called_once_with(expected_dest_path)
    assert "Successfully Added to Plex" in result
    assert "📦 Size: 1\\.0 KB" in result
    assert "📁 Destination: `/final/Sample \\(2023\\)\\.mkv`" in result


class SeasonFiles:
    def num_files(self):
        return 2

    def file_path(self, index):
        return ["Show.S01E01.mkv", "Show.S01E02.mkv"][index]


class SeasonTorrent:
    def files(self):
        return SeasonFiles()


@pytest.mark.asyncio
async def test_handle_successful_download_season_pack(mocker):
    ti = SeasonTorrent()
    parsed = {
        "type": "tv",
        "title": "Show",
        "season": 1,
        "is_season_pack": True,
    }
    save_paths = {"tv_shows": "/tv", "default": "/default"}

    mocker.patch(
        "telegram_bot.services.media_manager._get_final_destination_path",
        return_value="/final",
    )
    makedirs_mock = mocker.patch("telegram_bot.services.media_manager.os.makedirs")
    move_mock = mocker.patch("telegram_bot.services.media_manager.shutil.move")
    fetch_mock = mocker.patch(
        "telegram_bot.services.media_manager.fetch_episode_title_from_wikipedia",
        AsyncMock(side_effect=[("Ep1", None), ("Ep2", None)]),
    )
    scan_mock = mocker.patch(
        "telegram_bot.services.media_manager._trigger_plex_scan",
        return_value="",
    )
    size_mock = mocker.patch(
        "telegram_bot.services.media_manager._get_path_size_bytes",
        side_effect=[1024, 2048],
    )

    result = await handle_successful_download(
        ti,
        parsed,
        "/downloads",
        save_paths,
        {"url": "u", "token": "t"},
    )

    assert makedirs_mock.call_count == 2
    expected1_src = os.path.join("/downloads", "Show.S01E01.mkv")
    expected1_dest = os.path.join("/final", "s01e01 - Ep1.mkv")
    expected2_src = os.path.join("/downloads", "Show.S01E02.mkv")
    expected2_dest = os.path.join("/final", "s01e02 - Ep2.mkv")
    move_mock.assert_has_calls(
        [
            mocker.call(expected1_src, expected1_dest),
            mocker.call(expected2_src, expected2_dest),
        ],
        any_order=True,
    )
    assert fetch_mock.await_count == 2
    # One scan after all files have been moved
    assert scan_mock.call_count == 1
    assert size_mock.call_count == 2
    assert "Successfully Added to Plex" in result
    assert "📦 Size: 3\\.0 KB" in result
    assert "Processed and moved 2 episodes from the season pack." in result


def test_get_final_destination_path_collection():
    parsed = {
        "type": "movie",
        "collection": {
            "name": "Saga",
            "fs_name": "Saga",
        },
    }
    save_paths = {"movies": "/library/movies", "default": "/library"}
    result = _get_final_destination_path(parsed, save_paths)
    # Deferring directory creation: expect root movies path
    assert result == "/library/movies"


def test_get_final_destination_path_with_collection_name():
    parsed = {
        "type": "movie",
        "collection_name": "Saga Collection",
    }
    save_paths = {"movies": "/library/movies", "default": "/library"}
    result = _get_final_destination_path(parsed, save_paths)
    expected = os.path.join("/library/movies", "Saga Collection")
    assert result == expected


@pytest.mark.asyncio
async def test_trigger_plex_scan_skips_placeholder_token():
    result = await _trigger_plex_scan(
        "movie",
        {"url": "http://plex", "token": "PLEX_TOKEN"},
    )
    assert result == ""
