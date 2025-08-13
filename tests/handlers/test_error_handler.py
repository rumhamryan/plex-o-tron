# tests/handlers/test_error_handler.py
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock  # <-- Import Mock
import pytest
from telegram import Message
from telegram_bot.handlers.error_handler import global_error_handler

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))


@pytest.mark.asyncio
async def test_global_error_handler_logs_and_notifies(
    mocker, make_message, make_update
):
    message = make_message()
    update = make_update(message=message)

    # Create a mock that has the specific attributes the function needs
    context = Mock()
    context.error = Exception("boom")
    context.chat_data = {}
    context.user_data = {}

    reply_mock = mocker.patch.object(Message, "reply_text", AsyncMock())
    logger_mock = mocker.patch("telegram_bot.handlers.error_handler.logger.error")

    await global_error_handler(update, context)

    assert logger_mock.called
    reply_mock.assert_awaited_once()
    args, kwargs = reply_mock.call_args
    assert "An unexpected error occurred" in kwargs["text"]
