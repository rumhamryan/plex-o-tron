import asyncio
from unittest.mock import AsyncMock, Mock, ANY
import pytest
from telegram_bot.services.download_manager import (
    download_task_wrapper,
    _requeue_download,
)


@pytest.mark.asyncio
async def test_download_task_wrapper_metadata_timeout(mocker):
    handle = Mock()
    handle.is_valid.return_value = True
    ses = Mock()
    download_data = {
        "source_dict": {
            "value": "magnet:?x",
            "parsed_info": {},
            "type": "magnet",
            "clean_name": "Test Download",
        },
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
        side_effect=TimeoutError("metadata_timeout"),
    )
    mocker.patch(
        "telegram_bot.services.download_manager._finalize_download",
        AsyncMock(),
    )
    requeue_mock = mocker.patch(
        "telegram_bot.services.download_manager._requeue_download",
        AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.services.download_manager.process_queue_for_user",
        AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.services.download_manager.ProgressReporter",
        Mock(),
    )

    await download_task_wrapper(download_data, application)

    # Check requeue flags
    assert download_data["requeued"] is True
    assert download_data["metadata_timeout_occurred"] is True

    # Check cleanup
    ses.remove_torrent.assert_called_once_with(handle, ANY)

    # Check requeue call
    requeue_mock.assert_awaited_once_with(download_data, application)


@pytest.mark.asyncio
async def test_requeue_download_timeout_delay(mocker):
    """Test that a timeout requeue on an empty queue triggers a delay and unpauses."""
    download_data = {
        "chat_id": 123,
        "metadata_timeout_occurred": True,
        "is_paused": True,  # Should be flipped to False
    }
    application = Mock()
    application.bot_data = {
        "active_downloads": {"123": download_data},
        "download_queues": {},
    }

    sleep_mock = mocker.patch("asyncio.sleep", AsyncMock())
    process_mock = mocker.patch(
        "telegram_bot.services.download_manager.process_queue_for_user",
        AsyncMock(),
    )
    mocker.patch("telegram_bot.services.download_manager.save_state")

    await _requeue_download(download_data, application)

    # Should be unpaused
    assert download_data["is_paused"] is False

    # Should sleep because queue length became 1 (only this item)
    sleep_mock.assert_awaited_once_with(60)

    # Should trigger processing
    process_mock.assert_awaited_once_with(123, application)


@pytest.mark.asyncio
async def test_requeue_download_timeout_no_delay_if_queue_not_empty(mocker):
    """Test that if other items are in queue, we don't delay."""
    download_data = {
        "chat_id": 123,
        "metadata_timeout_occurred": True,
    }
    existing_item = {"some": "item"}
    application = Mock()
    application.bot_data = {
        "active_downloads": {"123": download_data},
        "download_queues": {"123": [existing_item]},
    }

    sleep_mock = mocker.patch("asyncio.sleep", AsyncMock())
    process_mock = mocker.patch(
        "telegram_bot.services.download_manager.process_queue_for_user",
        AsyncMock(),
    )
    mocker.patch("telegram_bot.services.download_manager.save_state")

    await _requeue_download(download_data, application)

    # Queue length will be 2
    assert len(application.bot_data["download_queues"]["123"]) == 2

    # Should NOT sleep
    sleep_mock.assert_not_awaited()

    process_mock.assert_awaited_once_with(123, application)


@pytest.mark.asyncio
async def test_requeue_download_normal(mocker):
    """Test normal requeue (paused, no delay)."""
    download_data = {
        "chat_id": 123,
        "is_paused": False,  # Should become True
    }
    application = Mock()
    application.bot_data = {
        "active_downloads": {"123": download_data},
        "download_queues": {},
    }

    sleep_mock = mocker.patch("asyncio.sleep", AsyncMock())
    process_mock = mocker.patch(
        "telegram_bot.services.download_manager.process_queue_for_user",
        AsyncMock(),
    )
    mocker.patch("telegram_bot.services.download_manager.save_state")

    await _requeue_download(download_data, application)

    assert download_data["is_paused"] is True
    sleep_mock.assert_not_awaited()
    process_mock.assert_awaited_once_with(123, application)
