import sys
import os
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from telegram_bot.services.media_manager import (
    generate_plex_filename,
    parse_resolution_from_name,
    handle_successful_download,
)

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
    assert "Success" in result


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
    assert scan_mock.call_count == 2
    assert "Processed and moved 2 episodes" in result
