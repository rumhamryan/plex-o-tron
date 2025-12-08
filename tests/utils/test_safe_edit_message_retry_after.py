import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from telegram.error import RetryAfter
from telegram_bot.utils import safe_edit_message


@pytest.mark.asyncio
async def test_safe_edit_message_retries_on_small_retry_after(mocker, make_message):
    # Arrange: message with bot, edit fails once with small RetryAfter then succeeds
    msg = make_message("orig", message_id=42)
    edit_mock = AsyncMock(side_effect=[RetryAfter(1), None])
    mocker.patch.object(type(msg), "edit_text", new=edit_mock)
    sleep_mock = mocker.patch("asyncio.sleep", new=AsyncMock())

    # Act
    await safe_edit_message(msg, text="hello", parse_mode=None, base_delay=0)

    # Assert: two attempts, slept for retry_after
    assert edit_mock.await_count == 2
    assert sleep_mock.await_args.args[0] >= 0.2


@pytest.mark.asyncio
async def test_safe_edit_message_suppresses_on_large_retry_after(mocker, make_message):
    # Arrange: first call hits a large RetryAfter; subsequent call within window is suppressed
    msg = make_message("orig", message_id=77)
    edit_mock = AsyncMock(side_effect=[RetryAfter(60)])
    mocker.patch.object(type(msg), "edit_text", new=edit_mock)

    # Fix monotonic so suppression window stays active across calls
    monotonic_mock = mocker.patch(
        "telegram_bot.utils.time.monotonic", return_value=1000.0
    )

    # Act: first call should set suppression and return without raising
    await safe_edit_message(msg, text="hello", parse_mode=None, max_retry_after=10.0)

    # Act again: second call while suppression is still active should no-op
    await safe_edit_message(msg, text="hello2", parse_mode=None, max_retry_after=10.0)

    # Assert: edit attempted only once; monotonic used
    assert edit_mock.await_count == 1
    monotonic_mock.assert_called()
