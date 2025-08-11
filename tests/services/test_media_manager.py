from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

import pytest
from telegram_bot.services.media_manager import (
    generate_plex_filename,
    parse_resolution_from_name,
)


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
