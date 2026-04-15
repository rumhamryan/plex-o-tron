from unittest.mock import AsyncMock

import pytest

from telegram_bot.services.media_manager.validation import validate_and_enrich_torrent


class _SingleLargeVideoFiles:
    def num_files(self):
        return 1

    def file_path(self, index):
        return "Big.Movie.2025.2160p.mkv"

    def file_size(self, index):
        return 60 * 1024 * 1024 * 1024


class _HugeValidTorrent:
    def files(self):
        return _SingleLargeVideoFiles()

    def total_size(self):
        return 60 * 1024 * 1024 * 1024

    def name(self):
        return "Big.Movie.2025.2160p.WEB-DL.x265"


@pytest.mark.asyncio
async def test_validate_and_enrich_torrent_allows_large_valid_torrent(mocker, make_message):
    progress = make_message()
    torrent = _HugeValidTorrent()

    parse_mock = mocker.patch(
        "telegram_bot.services.media_manager.validation.parse_torrent_name",
        return_value={"type": "movie", "title": "Big Movie", "year": "2025"},
    )
    safe_mock = mocker.patch(
        "telegram_bot.services.media_manager.validation.safe_edit_message",
        AsyncMock(),
    )

    error, parsed = await validate_and_enrich_torrent(torrent, progress)

    assert error is None
    assert parsed == {"type": "movie", "title": "Big Movie", "year": "2025"}
    parse_mock.assert_called_once_with("Big.Movie.2025.2160p.WEB-DL.x265")
    safe_mock.assert_not_awaited()
