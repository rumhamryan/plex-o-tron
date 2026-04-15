import sys
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from telegram import InlineKeyboardMarkup
from telegram_bot.services.torrent_service import fetch_metadata_from_magnet, process_user_input
from telegram_bot.services.torrent_service.input_handlers import (
    _fetch_and_parse_magnet_details,
    _handle_webpage_url,
)

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

# -------- process_user_input routing ---------


@pytest.mark.asyncio
async def test_process_user_input_magnet_routing(mocker, context, make_message):
    progress = make_message()
    magnet = "magnet:?xt=urn:btih:abc"
    fetch_mock = mocker.patch(
        "telegram_bot.services.torrent_service.input_handlers.fetch_metadata_from_magnet",
        AsyncMock(return_value=None),
    )

    await process_user_input(magnet, context, progress)

    fetch_mock.assert_awaited_once_with(magnet, progress, context)


@pytest.mark.asyncio
async def test_process_user_input_torrent_url_routing(mocker, context, make_message):
    progress = make_message()
    url = "https://example.com/file.torrent"
    handle_mock = mocker.patch(
        "telegram_bot.services.torrent_service.input_handlers._handle_torrent_url",
        AsyncMock(return_value=None),
    )

    await process_user_input(url, context, progress)

    handle_mock.assert_awaited_once_with(url, context, progress)


@pytest.mark.asyncio
async def test_process_user_input_webpage_routing(mocker, context, make_message):
    progress = make_message()
    url = "https://example.com/page"
    handle_mock = mocker.patch(
        "telegram_bot.services.torrent_service.input_handlers._handle_webpage_url",
        AsyncMock(return_value=None),
    )

    await process_user_input(url, context, progress)

    handle_mock.assert_awaited_once_with(url, context, progress)


# -------- _handle_webpage_url ---------


@pytest.mark.asyncio
async def test_handle_webpage_url_no_links(mocker, context, make_message):
    url = "https://example.com"
    progress = make_message()
    mocker.patch(
        "telegram_bot.services.torrent_service.input_handlers.find_magnet_link_on_page",
        AsyncMock(return_value=[]),
    )
    safe_mock = mocker.patch(
        "telegram_bot.services.torrent_service.input_handlers.safe_edit_message",
        AsyncMock(),
    )

    result = await _handle_webpage_url(url, context, progress)

    assert result is None
    # Last call should contain the error message
    args, kwargs = safe_mock.call_args
    assert "Error" in kwargs["text"]


@pytest.mark.asyncio
async def test_handle_webpage_url_multiple_links(mocker, context, make_message):
    url = "https://example.com"
    progress = make_message()
    context.user_data["link_prompt_message_id"] = 55
    magnet_links = ["magnet1", "magnet2"]
    mocker.patch(
        "telegram_bot.services.torrent_service.input_handlers.find_magnet_link_on_page",
        AsyncMock(return_value=magnet_links),
    )
    parsed_choices = [
        {
            "index": 0,
            "resolution": "1080p",
            "file_type": "MP4",
            "size": "1 GB",
            "name": "Name1",
            "magnet_link": "magnet1",
            "bencoded_metadata": b"1",
        },
        {
            "index": 1,
            "resolution": "720p",
            "file_type": "MKV",
            "size": "700 MB",
            "name": "Name2",
            "magnet_link": "magnet2",
            "bencoded_metadata": b"2",
        },
    ]
    mocker.patch(
        "telegram_bot.services.torrent_service.input_handlers._fetch_and_parse_magnet_details",
        AsyncMock(return_value=parsed_choices),
    )
    safe_mock = mocker.patch(
        "telegram_bot.services.torrent_service.input_handlers.safe_edit_message",
        AsyncMock(),
    )

    result = await _handle_webpage_url(url, context, progress)

    assert result is None
    assert context.user_data["temp_magnet_choices_details"] == parsed_choices
    args, kwargs = safe_mock.call_args
    assert "Please select one" in kwargs["text"]
    markup = kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert len(markup.inline_keyboard) == len(parsed_choices) + 1  # + Cancel button
    context.bot.delete_message.assert_awaited_once_with(chat_id=progress.chat_id, message_id=55)
    assert "link_prompt_message_id" not in context.user_data


@pytest.mark.asyncio
async def test_fetch_and_parse_magnet_details_escapes_status_message(mocker, context, make_message):
    progress = make_message()
    context.bot_data["TORRENT_SESSION"] = object()
    safe_mock = mocker.patch(
        "telegram_bot.services.torrent_service.input_handlers.safe_edit_message",
        AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.services.torrent_service.input_handlers._blocking_fetch_metadata",
        return_value=None,
    )

    result = await _fetch_and_parse_magnet_details(["magnet1", "magnet2"], context, progress)

    assert result == []
    safe_mock.assert_awaited_once()
    kwargs = safe_mock.await_args.kwargs
    assert kwargs["parse_mode"] == "MarkdownV2"
    assert kwargs["text"] == "Found 2 links\\. Fetching details\\.\\.\\."


@pytest.mark.asyncio
async def test_fetch_and_parse_magnet_details_keeps_large_torrent(mocker, context, make_message):
    class _Files:
        def num_files(self):
            return 1

        def file_path(self, index):
            return "Big.Movie.2025.2160p.mkv"

        def file_size(self, index):
            return 60 * 1024 * 1024 * 1024

    class _TorrentInfo:
        def name(self):
            return "Big.Movie.2025.2160p.WEB-DL.x265"

        def total_size(self):
            return 60 * 1024 * 1024 * 1024

        def files(self):
            return _Files()

    progress = make_message()
    context.bot_data["TORRENT_SESSION"] = object()
    mocker.patch(
        "telegram_bot.services.torrent_service.input_handlers.safe_edit_message",
        AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.services.torrent_service.input_handlers._blocking_fetch_metadata",
        return_value=b"metadata",
    )
    mocker.patch(
        "telegram_bot.services.torrent_service.input_handlers.lt.torrent_info",
        return_value=_TorrentInfo(),
    )

    result = await _fetch_and_parse_magnet_details(["magnet1"], context, progress)

    assert len(result) == 1
    assert result[0]["name"] == "Big.Movie.2025.2160p.WEB-DL.x265"
    assert result[0]["magnet_link"] == "magnet1"


# -------- fetch_metadata_from_magnet ---------


@pytest.mark.asyncio
async def test_fetch_metadata_from_magnet_timeout(mocker, context, make_message):
    progress = make_message()
    context.bot_data["TORRENT_SESSION"] = object()
    mocker.patch(
        "telegram_bot.services.torrent_service.metadata_fetch._update_fetch_timer",
        AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.services.torrent_service.metadata_fetch._blocking_fetch_metadata",
        return_value=None,
    )
    safe_mock = mocker.patch(
        "telegram_bot.services.torrent_service.metadata_fetch.safe_edit_message",
        AsyncMock(),
    )

    result = await fetch_metadata_from_magnet("magnet:?xt=urn:btih:abc", progress, context)

    assert result is None
    args, kwargs = safe_mock.call_args
    assert "Timed out" in kwargs["text"]


@pytest.mark.asyncio
async def test_fetch_metadata_from_magnet_success(mocker, context, make_message):
    progress = make_message()
    context.bot_data["TORRENT_SESSION"] = object()
    mocker.patch(
        "telegram_bot.services.torrent_service.metadata_fetch._update_fetch_timer",
        AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.services.torrent_service.metadata_fetch._blocking_fetch_metadata",
        return_value=b"data",
    )
    ti_obj = object()
    mocker.patch(
        "telegram_bot.services.torrent_service.metadata_fetch.lt.torrent_info",
        return_value=ti_obj,
    )

    result = await fetch_metadata_from_magnet("magnet:?xt=urn:btih:abc", progress, context)

    assert result is ti_obj
