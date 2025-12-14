# tests/handlers/test_error_handler.py
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import Message
from telegram.error import NetworkError

from telegram_bot.handlers import error_handler
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
    # New behavior: ensure we force plain text to avoid parse errors
    assert kwargs.get("parse_mode", None) is None


@pytest.mark.asyncio
async def test_global_error_handler_throttles_network_errors(mocker):
    error_handler._LAST_TRANSIENT_LOG.clear()
    warn_mock = mocker.patch("telegram_bot.handlers.error_handler.logger.warning")
    error_mock = mocker.patch("telegram_bot.handlers.error_handler.logger.error")

    context = Mock()
    context.error = NetworkError("httpx.ReadError")
    context.chat_data = {}
    context.user_data = {}

    await global_error_handler(update=None, context=context)
    warn_mock.assert_called_once()
    error_mock.assert_not_called()

    warn_mock.reset_mock()
    # Second call should be suppressed due to throttling
    await global_error_handler(update=None, context=context)
    warn_mock.assert_not_called()
