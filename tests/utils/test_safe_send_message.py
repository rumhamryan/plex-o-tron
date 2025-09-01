import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from telegram.error import TimedOut, RetryAfter, NetworkError

from telegram_bot.utils import safe_send_message


@pytest.mark.asyncio
async def test_safe_send_message_success(mocker):
    # Arrange
    message = SimpleNamespace(message_id=1)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=message))

    # Act
    result = await safe_send_message(bot, chat_id=123, text="hi")

    # Assert
    bot.send_message.assert_awaited_once_with(chat_id=123, text="hi")
    assert result is message


@pytest.mark.asyncio
async def test_safe_send_message_retries_on_timeout_then_succeeds(mocker):
    # Arrange
    message = SimpleNamespace(message_id=1)
    send = AsyncMock(side_effect=[TimedOut(), message])
    bot = SimpleNamespace(send_message=send)
    sleep_mock = mocker.patch("asyncio.sleep", new=AsyncMock())

    # Act
    result = await safe_send_message(bot, chat_id=1, text="x", base_delay=0)

    # Assert
    assert send.await_count == 2
    sleep_mock.assert_awaited()
    assert result is message


@pytest.mark.asyncio
async def test_safe_send_message_respects_retry_after(mocker):
    # Arrange
    message = SimpleNamespace(message_id=1)
    send = AsyncMock(side_effect=[RetryAfter(0.5), message])
    bot = SimpleNamespace(send_message=send)
    sleep_mock = mocker.patch("asyncio.sleep", new=AsyncMock())

    # Act
    result = await safe_send_message(bot, chat_id=1, text="x", base_delay=0)

    # Assert
    assert send.await_count == 2
    # First sleep call should use retry_after from exception (approx 0.5)
    assert sleep_mock.await_args.args[0] >= 0.5
    assert result is message


@pytest.mark.asyncio
async def test_safe_send_message_retries_on_network_error_then_raises(mocker):
    # Arrange: exceed attempts
    send = AsyncMock(
        side_effect=[NetworkError("n"), NetworkError("n"), NetworkError("n")]
    )
    bot = SimpleNamespace(send_message=send)
    mocker.patch("asyncio.sleep", new=AsyncMock())

    # Act / Assert
    with pytest.raises(NetworkError):
        await safe_send_message(bot, chat_id=1, text="x", max_attempts=3, base_delay=0)
