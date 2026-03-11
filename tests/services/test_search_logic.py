import pytest
from telegram_bot.services.search_logic import _parse_size_to_gb
from telegram_bot.utils import parse_codec, score_torrent_result


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Some.Movie.x265", "x265"),
        ("Another HEVC release", "x265"),
        ("Film X264 edition", "x264"),
        ("AV1 Showcase 1080p", "av1"),
        ("H.265 encode", "x265"),
        ("H 265 Hybrid", "x265"),
        ("H264 remux", "x264"),
        ("H 264-FLUX", "x264"),
        ("Rick and Morty S08E01 1080p WEB H264-LAZYCUNTS [eztv]", "x264"),
        (
            "Rick and Morty S08E01 Summer of All Fears 720p MAX WEB-DL DDP5 1 H 264-FLUX [eztv]",
            "x264",
        ),
        ("No codec here", None),
    ],
)
def testparse_codec(title, expected):
    assert parse_codec(title) == expected


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
    # seeders=25, leechers=0 -> availability = 10 * (1 - exp(-25/25)) = 6.32 -> round to 6
    # 10 (x265) + 5 (1080p) + 20 (trusted) + 6 (health) = 41
    score = score_torrent_result("Great Movie 1080p x265", "trusted", prefs, seeders=25)
    assert score == 41

    # seeders=3, leechers=0 -> 1 rounding to 1.
    no_match = score_torrent_result("Another 720p x264", "unknown", prefs, seeders=3)
    assert no_match == 1
