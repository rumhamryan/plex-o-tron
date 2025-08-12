import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, ANY

import pytest

from telegram_bot.services.download_manager import (
    ProgressReporter,
    download_task_wrapper,
    add_download_to_queue,
    process_queue_for_user,
    handle_pause_request,
    handle_resume_request,
)



@pytest.mark.asyncio
async def test_progress_reporter_movie(mocker):
    status = SimpleNamespace(
        progress=0.5,
        download_rate=1024 * 1024,
        state=SimpleNamespace(name="downloading"),
        num_peers=5,
    )
    application = SimpleNamespace(bot=SimpleNamespace())
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
    application = SimpleNamespace(bot=SimpleNamespace())
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
    application = SimpleNamespace(bot=SimpleNamespace(), bot_data={})
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
    application = SimpleNamespace(bot=SimpleNamespace(), bot_data={"TORRENT_SESSION": ses})
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
    application = SimpleNamespace(bot=SimpleNamespace(), bot_data={})
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
async def test_add_download_to_queue(mocker, make_update, make_callback_query, make_message, context):
    message = make_message(message_id=10)
    callback = make_callback_query(data="confirm", message=message)
    update = make_update(callback_query=callback)
    context.user_data["pending_torrent"] = {"value": "magnet", "original_message_id": 10, "type": "magnet"}
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
async def test_process_queue_for_user_active(mocker):
    chat_id = 111
    application = SimpleNamespace(bot_data={
        "active_downloads": {str(chat_id): {}},
        "download_queues": {str(chat_id): [{}]},
    })
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
    application = SimpleNamespace(bot_data={
        "active_downloads": {},
        "download_queues": {str(chat_id): [download_item]},
    })
    start_mock = mocker.patch(
        "telegram_bot.services.download_manager._start_download_task",
        AsyncMock(),
    )

    await process_queue_for_user(chat_id, application)

    start_mock.assert_awaited_once_with(download_item, application)
    assert str(chat_id) not in application.bot_data["download_queues"]


@pytest.mark.asyncio
async def test_pause_and_resume_requests(mocker, make_update, make_callback_query, make_message, context):
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
