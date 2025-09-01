import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from telegram.error import BadRequest
from telegram_bot.utils import safe_edit_message


@pytest.mark.asyncio
async def test_safe_edit_message_falls_back_to_send(mocker, make_message):
    # Arrange: a Message with a bot attached
    msg = make_message("orig", message_id=10)
    # Force edit to fail with recoverable BadRequest
    mocker.patch.object(
        type(msg),
        "edit_text",
        new=AsyncMock(side_effect=BadRequest("Message to edit not found")),
    )
    # Patch safe_send_message used by utils.safe_edit_message
    safe_send = mocker.patch("telegram_bot.utils.safe_send_message", new=AsyncMock())

    # Act
    await safe_edit_message(msg, text="replacement", parse_mode=None)

    # Assert: falls back to send
    safe_send.assert_awaited_once()
    kwargs = safe_send.await_args.kwargs
    assert kwargs["chat_id"] == msg.chat_id
    assert kwargs["text"] == "replacement"
