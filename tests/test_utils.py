import sys
from pathlib import Path
import pytest
from telegram_bot.utils import (
    format_bytes,
    extract_first_int,
    parse_torrent_name,
    get_site_name_from_url,
)

sys.path.append(str(Path(__file__).resolve().parent.parent))


# Use pytest's "parametrize" to test many cases with one function
@pytest.mark.parametrize(
    "size_bytes, expected_str",
    [
        (0, "0B"),
        (1023, "1023.0 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1048576, "1.0 MB"),
        (1610612736, "1.5 GB"),
    ],
)
def test_format_bytes(size_bytes, expected_str):
    """Verify that format_bytes converts byte sizes to correct human-readable strings."""
    assert format_bytes(size_bytes) == expected_str


@pytest.mark.parametrize(
    "text, expected_int",
    [
        ("S01E05", 1),
        ("Season 12", 12),
        ("No numbers here", None),
        ("", None),
        ("Episode 5 is the best", 5),
    ],
)
def test_extract_first_int(text, expected_int):
    """Verify that extract_first_int correctly pulls the first integer."""
    assert extract_first_int(text) == expected_int


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Movie.Title.2023", {"type": "movie", "title": "Movie Title", "year": "2023"}),
        (
            "Show.Name.S01E02.1080p",
            {"type": "tv", "title": "Show Name", "season": 1, "episode": 2},
        ),
        (
            "Show Name 1x02 [1080p]",
            {"type": "tv", "title": "Show Name", "season": 1, "episode": 2},
        ),
        (
            "Another_Show-S01E02_[x265]",
            {"type": "tv", "title": "Another Show", "season": 1, "episode": 2},
        ),
        (
            "Unknown.File[x265]",
            {"type": "unknown", "title": "Unknown File"},
        ),
    ],
)
def test_parse_torrent_name(name, expected):
    """Verify that torrent names are parsed into the correct metadata."""
    assert parse_torrent_name(name) == expected


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://yts.mx/some/path", "YTS"),
        ("https://1337x.to/torrent/123", "1337X"),
        ("", "Unknown"),
    ],
)
def test_get_site_name_from_url(url, expected):
    """Ensure site names are extracted cleanly from URLs."""
    assert get_site_name_from_url(url) == expected
