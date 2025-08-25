import sys
from pathlib import Path
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, ANY

import httpx
import pytest
from telegram_bot.services.download_manager import (
    ProgressReporter,
    download_task_wrapper,
    add_download_to_queue,
    add_season_to_queue,
    process_queue_for_user,
    handle_pause_request,
    handle_resume_request,
    download_with_progress,
)

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))


@pytest.mark.asyncio
async def test_progress_reporter_movie(mocker):
    status = SimpleNamespace(
        progress=0.5,
        download_rate=1024 * 1024,
        state=SimpleNamespace(name="downloading"),
        num_peers=5,
    )
    application = Mock()
    download_data = {"lock": asyncio.Lock(), "is_paused": False}
    reporter = ProgressReporter(
        application,
        chat_id=1,
        message_id=2,
        parsed_info={"type": "movie"},
        clean_name="Sample Movie",
        download_data=download_data,
    )
    safe_mock = mocker.patch(
        "telegram_bot.services.download_manager.safe_edit_message",
        AsyncMock(),
    )

    await reporter.report(status)

    safe_mock.assert_awaited_once()
    _, kwargs = safe_mock.call_args
    assert "⬇️ *Downloading:*" in kwargs["text"]
    assert "Sample Movie" in kwargs["text"]


@pytest.mark.asyncio
async def test_progress_reporter_tv_paused(mocker):
    status = SimpleNamespace(
        progress=0.25,
        download_rate=512 * 1024,
        state=SimpleNamespace(name="downloading"),
        num_peers=3,
    )
    application = Mock()
    download_data = {"lock": asyncio.Lock(), "is_paused": True}
    reporter = ProgressReporter(
        application,
        chat_id=1,
        message_id=2,
        parsed_info={
            "type": "tv",
            "title": "Show",
            "season": 1,
            "episode": 2,
            "episode_title": "Pilot",
        },
        clean_name="ignored",
        download_data=download_data,
    )
    safe_mock = mocker.patch(
        "telegram_bot.services.download_manager.safe_edit_message",
        AsyncMock(),
    )

    await reporter.report(status)

    safe_mock.assert_awaited_once()
    _, kwargs = safe_mock.call_args
    assert "⏸️ *Paused:*" in kwargs["text"]
    assert "S01E02" in kwargs["text"]


@pytest.mark.asyncio
async def test_download_task_wrapper_success(mocker):
    download_data = {
        "source_dict": {"value": "magnet:?x", "parsed_info": {}, "type": "magnet"},
        "chat_id": 123,
        "message_id": 1,
        "save_path": "/tmp",
        "lock": asyncio.Lock(),
    }
    application = Mock()
    mocker.patch(
        "telegram_bot.services.download_manager.download_with_progress",
        AsyncMock(return_value=(True, "ti")),
    )
    handle_success = mocker.patch(
        "telegram_bot.services.download_manager.handle_successful_download",
        AsyncMock(return_value="msg"),
    )
    mocker.patch(
        "telegram_bot.services.download_manager._finalize_download",
        AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.services.download_manager.process_queue_for_user",
        AsyncMock(),
    )

    await download_task_wrapper(download_data, application)

    handle_success.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_task_wrapper_cancellation_cleanup(mocker):
    handle = Mock()
    handle.is_valid.return_value = True
    ses = Mock()
    download_data = {
        "source_dict": {"value": "magnet:?x", "parsed_info": {}, "type": "magnet"},
        "chat_id": 1,
        "message_id": 2,
        "save_path": "/tmp",
        "lock": asyncio.Lock(),
        "handle": handle,
    }
    application = Mock()
    application.bot_data = {"TORRENT_SESSION": ses}

    mocker.patch(
        "telegram_bot.services.download_manager.download_with_progress",
        AsyncMock(side_effect=asyncio.CancelledError()),
    )
    mocker.patch(
        "telegram_bot.services.download_manager._finalize_download",
        AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.services.download_manager.process_queue_for_user",
        AsyncMock(),
    )

    await download_task_wrapper(download_data, application)

    ses.remove_torrent.assert_called_once_with(handle, ANY)


@pytest.mark.asyncio
async def test_download_task_wrapper_failure_message(mocker):
    download_data = {
        "source_dict": {"value": "magnet:?x", "parsed_info": {}, "type": "magnet"},
        "chat_id": 1,
        "message_id": 2,
        "save_path": "/tmp",
        "lock": asyncio.Lock(),
    }
    application = Mock()
    application.bot_data = {}

    mocker.patch(
        "telegram_bot.services.download_manager.download_with_progress",
        AsyncMock(return_value=(False, None)),
    )
    finalize_mock = mocker.patch(
        "telegram_bot.services.download_manager._finalize_download",
        AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.services.download_manager.process_queue_for_user",
        AsyncMock(),
    )

    await download_task_wrapper(download_data, application)

    finalize_mock.assert_awaited_once()
    args, _ = finalize_mock.call_args
    assert "Download Failed" in args[3]


@pytest.mark.asyncio
async def test_download_with_progress_http_status_error(mocker):
    """Ensures HTTP errors during .torrent retrieval are gracefully handled."""
    session = Mock()
    bot_data = {"TORRENT_SESSION": session}
    download_data: dict[str, str] = {}

    request = httpx.Request("GET", "http://example.com/bad.torrent")
    response = httpx.Response(404, request=request)
    http_error = httpx.HTTPStatusError("not found", request=request, response=response)
    response.raise_for_status = Mock(side_effect=http_error)

    client = AsyncMock()
    client.get.return_value = response
    async_client = AsyncMock()
    async_client.__aenter__.return_value = client
    mocker.patch("httpx.AsyncClient", return_value=async_client)

    success, info = await download_with_progress(
        source="http://example.com/bad.torrent",
        save_path="/tmp",
        status_callback=AsyncMock(),
        bot_data=bot_data,
        download_data=download_data,
    )

    assert success is False
    assert info is None
    session.add_torrent.assert_not_called()


@pytest.mark.asyncio
async def test_add_download_to_queue(
    mocker, make_update, make_callback_query, make_message, context
):
    message = make_message(message_id=10)
    callback = make_callback_query(data="confirm", message=message)
    update = make_update(callback_query=callback)
    context.user_data["pending_torrent"] = {
        "value": "magnet",
        "original_message_id": 10,
        "type": "magnet",
    }
    context.bot_data["active_downloads"] = {}
    context.bot_data["download_queues"] = {}
    context.bot_data["SAVE_PATHS"] = {"default": "/tmp"}
    context.application = SimpleNamespace(bot=context.bot, bot_data=context.bot_data)

    mocker.patch("telegram_bot.services.download_manager.save_state")
    process_mock = mocker.patch(
        "telegram_bot.services.download_manager.process_queue_for_user",
        AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.services.download_manager.safe_edit_message",
        AsyncMock(),
    )

    await add_download_to_queue(update, context)

    q = context.bot_data["download_queues"][str(message.chat.id)]
    assert len(q) == 1
    process_mock.assert_awaited_once_with(message.chat.id, context.application)


@pytest.mark.asyncio
async def test_add_season_to_queue(
    mocker, make_update, make_callback_query, make_message, context
):
    message = make_message(message_id=20)
    callback = make_callback_query(data="confirm_season_download", message=message)
    update = make_update(callback_query=callback)
    context.user_data["pending_season_download"] = [
        {"link": "magnet1", "parsed_info": {"episode": 1}},
        {"link": "magnet2", "parsed_info": {"episode": 2}},
    ]
    context.bot_data["active_downloads"] = {}
    context.bot_data["download_queues"] = {}
    context.bot_data["SAVE_PATHS"] = {"default": "/tmp"}
    context.application = SimpleNamespace(bot=context.bot, bot_data=context.bot_data)

    mocker.patch("telegram_bot.services.download_manager.save_state")
    process_mock = mocker.patch(
        "telegram_bot.services.download_manager.process_queue_for_user",
        AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.services.download_manager.safe_edit_message",
        AsyncMock(),
    )

    await add_season_to_queue(update, context)

    q = context.bot_data["download_queues"][str(message.chat.id)]
    assert len(q) == 2
    assert q[0]["source_dict"]["parsed_info"]["episode"] == 1
    process_mock.assert_awaited_once_with(message.chat.id, context.application)


@pytest.mark.asyncio
async def test_process_queue_for_user_active(mocker):
    chat_id = 111
    application = Mock()
    application.bot_data = {
        "active_downloads": {str(chat_id): {}},
        "download_queues": {},
    }
    start_mock = mocker.patch(
        "telegram_bot.services.download_manager._start_download_task",
        AsyncMock(),
    )

    await process_queue_for_user(chat_id, application)

    start_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_queue_for_user_start(mocker):
    chat_id = 222
    download_item = {"chat_id": chat_id}
    application = Mock()
    application.bot_data = {
        "active_downloads": {},
        "download_queues": {str(chat_id): [download_item]},
    }
    start_mock = mocker.patch(
        "telegram_bot.services.download_manager._start_download_task",
        AsyncMock(),
    )

    await process_queue_for_user(chat_id, application)

    start_mock.assert_awaited_once_with(download_item, application)
    assert str(chat_id) not in application.bot_data["download_queues"]


@pytest.mark.asyncio
async def test_pause_and_resume_requests(
    mocker, make_update, make_callback_query, make_message, context
):
    message = make_message()
    callback = make_callback_query(data="pause_download", message=message)
    update = make_update(callback_query=callback)
    download_data = {"lock": asyncio.Lock(), "is_paused": False}
    context.bot_data["active_downloads"] = {str(message.chat.id): download_data}

    await handle_pause_request(update, context)
    assert download_data["is_paused"] is True

    callback_resume = make_callback_query(data="resume_download", message=message)
    update_resume = make_update(callback_query=callback_resume)
    await handle_resume_request(update_resume, context)
    assert download_data["is_paused"] is False
