import sys
from pathlib import Path
import pytest
from unittest.mock import Mock
from telegram_bot.services.search_logic import (
    _parse_codec,
    _parse_size_to_gb,
    score_torrent_result,
    orchestrate_searches,
)

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Some.Movie.x265", "x265"),
        ("Another HEVC release", "x265"),
        ("Film X264 edition", "x264"),
        ("No codec here", None),
    ],
)
def test_parse_codec(title, expected):
    assert _parse_codec(title) == expected


@pytest.mark.parametrize(
    "size_str, expected",
    [
        ("1.5 GB", 1.5),
        ("500 MB", 500 / 1024),
        ("1024 KB", 1024 / (1024 * 1024)),
        ("invalid", 0.0),
    ],
)
def test_parse_size_to_gb(size_str, expected):
    assert _parse_size_to_gb(size_str) == pytest.approx(expected)


def test_score_torrent_result():
    prefs = {
        "codecs": {"x265": 10},
        "resolutions": {"1080p": 5},
        "uploaders": {"trusted": 20},
    }
    score = score_torrent_result("Great Movie 1080p x265", "trusted", prefs, seeders=7)
    assert score == 42  # 10 + 5 + 20 + 7

    no_match = score_torrent_result("Another 720p x264", "unknown", prefs, seeders=3)
    assert no_match == 3


@pytest.mark.asyncio
async def test_orchestrate_searches_uses_generic_scraper(mocker):
    context = Mock()
    context.bot_data = {
        "SEARCH_CONFIG": {
            "websites": {
                "movies": [
                    {
                        "name": "Example",
                        "search_url": "https://example.com/?q={query}",
                        "enabled": True,
                    }
                ]
            },
            "preferences": {"movies": {}},
        }
    }

    async def fake_generic(
        query, media_type, search_url, *, preferences=None, source_name=None
    ):
        return [
            {
                "title": "Sample",
                "page_url": "url",
                "score": 1,
                "source": source_name,
                "uploader": "uploader",
                "size_gb": 0,
                "codec": None,
                "seeders": 0,
                "leechers": 0,
                "year": None,
            }
        ]

    mocker.patch(
        "telegram_bot.services.scraping_service.scrape_generic_page",
        side_effect=fake_generic,
    )

    results = await orchestrate_searches("query", "movie", context)
    assert results[0]["source"] == "Example"
