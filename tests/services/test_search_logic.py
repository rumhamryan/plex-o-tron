import pytest
from telegram_bot.services.search_logic import (
    _parse_size_to_gib,
    find_episode_file,
    find_media_by_name,
    find_season_directory,
)
from telegram_bot.utils import (
    compute_av_match_metadata,
    parse_audio_channels,
    parse_audio_formats,
    parse_codec,
    parse_video_formats,
    score_torrent_result,
)


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
def test_parse_size_to_gib(size_str, expected):
    assert _parse_size_to_gib(size_str) == pytest.approx(expected)


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


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Movie 2160p DoVi HDR10", {"dolby_vision"}),
        ("Movie HDR10+ WEB-DL", {"hdr10_plus"}),
        ("Movie HDR10 WEB-DL", {"hdr10"}),
        ("Movie HDR WEB-DL", {"hdr"}),
        ("Movie HDRip x264", set()),
        ("Movie DV HEVC 2160p", {"dolby_vision"}),
        ("Movie HLG Broadcast", {"hlg"}),
        ("Movie SDR WEB", {"sdr"}),
    ],
)
def test_parse_video_formats(title: str, expected: set[str]):
    assert parse_video_formats(title) == expected


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Movie Dolby Atmos TrueHD 7.1", {"atmos", "truehd"}),
        ("Movie DDP5.1 WEB-DL", {"ddp"}),
        ("Movie E-AC-3 WEB-DL", {"ddp"}),
        ("Movie AC3 5.1", {"dd"}),
        ("Movie DTS-HD MA 5.1", {"dts_hd_ma"}),
        ("Movie DTS-HD 5.1", {"dts_hd"}),
        ("Movie DTS 5.1", {"dts"}),
        ("Movie AAC 2.0", {"aac"}),
        ("Movie FLAC OPUS", {"flac", "opus"}),
    ],
)
def test_parse_audio_formats(title: str, expected: set[str]):
    assert parse_audio_formats(title) == expected


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Movie Atmos 7.1", {"7.1"}),
        ("Movie DDP5 1", {"5.1"}),
        ("Movie AAC Stereo", {"2.0"}),
        ("Movie Mono Track", {"1.0"}),
    ],
)
def test_parse_audio_channels(title: str, expected: set[str]):
    assert parse_audio_channels(title) == expected


def test_compute_av_match_metadata_obeys_specificity_and_sets_flags():
    prefs = {
        "video_formats": {"dolby_vision": 8, "hdr10_plus": 6, "hdr10": 4, "hdr": 2},
        "audio_formats": {"atmos": 8, "truehd": 6, "ddp": 4},
        "audio_channels": {"7.1": 3, "5.1": 2},
    }
    metadata = compute_av_match_metadata(
        "Movie 2160p DoVi HDR10 Dolby Atmos TrueHD 7.1",
        prefs,
    )
    assert metadata["matched_video_formats"] == ["dolby_vision"]
    assert metadata["matched_audio_formats"] == ["atmos", "truehd"]
    assert metadata["matched_audio_channels"] == ["7.1"]
    assert metadata["is_gold_av"] is True
    assert metadata["is_silver_av"] is False
    assert metadata["has_video_match"] is True
    assert metadata["has_audio_match"] is True


def test_score_torrent_result_includes_av_format_preferences():
    prefs = {
        "video_formats": {"dolby_vision": 8},
        "audio_formats": {"atmos": 8},
        "audio_channels": {"7.1": 3},
    }
    score = score_torrent_result(
        "Movie.2160p.DoVi.Atmos.7.1",
        "Uploader",
        prefs,
        seeders=0,
        leechers=0,
    )
    assert score == 19


def test_score_torrent_result_uses_highest_video_and_audio_format_only():
    prefs = {
        "video_formats": {"dolby_vision": 8, "hdr10_plus": 6},
        "audio_formats": {"atmos": 8, "ddp": 4},
        "audio_channels": {"5.1": 2},
    }
    score = score_torrent_result(
        "Movie 2160p DoVi HDR10+ DDP5.1 Atmos",
        "Uploader",
        prefs,
        seeders=0,
        leechers=0,
    )
    # Video: max(DoVi=8, HDR10+=6) => 8
    # Audio: max(Atmos=8, DDP=4) => 8
    # Channels: 5.1 => 2
    assert score == 18


@pytest.mark.asyncio
async def test_find_media_by_name_ignores_trashinfo_files(tmp_path):
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    valid_file = movies_root / "Him (2023).mkv"
    valid_file.write_text("video")

    trash_info_dir = movies_root / ".Trash-1000" / "info"
    trash_info_dir.mkdir(parents=True)
    (trash_info_dir / "Him (2023).mkv.trashinfo").write_text("trash entry")

    result = await find_media_by_name(
        "movie",
        "Him",
        {"movies": str(movies_root)},
        "file",
    )

    assert result == str(valid_file)


@pytest.mark.asyncio
async def test_find_media_by_name_ignores_directories_inside_trash_roots(tmp_path):
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    valid_directory = movies_root / "Him Collection"
    valid_directory.mkdir()

    trash_files_dir = movies_root / ".Trash-1000" / "files" / "Him Collection"
    trash_files_dir.mkdir(parents=True)

    result = await find_media_by_name(
        "movie",
        "Him",
        {"movies": str(movies_root)},
        "directory",
    )

    assert result == str(valid_directory)


@pytest.mark.asyncio
async def test_find_season_directory_returns_none_for_ignored_trash_show_path(tmp_path):
    trash_show_path = tmp_path / ".Trash-1000" / "files" / "Example Show"
    (trash_show_path / "Season 01").mkdir(parents=True)

    result = await find_season_directory(str(trash_show_path), 1)

    assert result is None


@pytest.mark.asyncio
async def test_find_episode_file_ignores_trashinfo_matches(tmp_path):
    season_path = tmp_path / "Example Show" / "Season 01"
    season_path.mkdir(parents=True)
    valid_file = season_path / "Example Show S01E02.mkv"
    valid_file.write_text("episode")
    (season_path / "Example Show S01E01.mkv.trashinfo").write_text("trash entry")

    result = await find_episode_file(str(season_path), 1, 1)

    assert result is None


@pytest.mark.asyncio
async def test_find_episode_file_returns_valid_media_when_trashinfo_also_exists(tmp_path):
    season_path = tmp_path / "Example Show" / "Season 01"
    season_path.mkdir(parents=True)
    valid_file = season_path / "Example Show S01E01.mkv"
    valid_file.write_text("episode")
    (season_path / "Example Show S01E01.mp4.trashinfo").write_text("trash entry")

    result = await find_episode_file(str(season_path), 1, 1)

    assert result == str(valid_file)
