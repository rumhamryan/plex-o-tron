import sys
import os
from pathlib import Path
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
    mocker.patch("telegram_bot.services.media_manager._cleanup_source_directory")

    result = await handle_successful_download(
        ti,
        parsed,
        "/downloads",
        save_paths,
        {"url": "u", "token": "t"},
    )

    makedirs_mock.assert_called_once_with("/final", exist_ok=True)

    # FIX: Construct the expected paths using os.path.join
    expected_source_path = os.path.join("/downloads", "Movie.mkv")
    expected_dest_path = os.path.join("/final", "Sample (2023).mkv")

    move_mock.assert_called_once_with(expected_source_path, expected_dest_path)

    scan_mock.assert_called_once()
    assert "Success" in result
