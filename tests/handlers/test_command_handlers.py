import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from telegram import Update, Message

# Ensure root path for imports
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from telegram_bot.handlers.command_handlers import search_command, plex_status_command


@pytest.mark.asyncio
async def test_search_command_starts_workflow(mocker, make_message, context):
    update = Update(update_id=1, message=make_message("/search"))

    mocker.patch(
        "telegram_bot.handlers.command_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    reply_mock = mocker.patch.object(Message, "reply_text", AsyncMock())

    await search_command(update, context)

    reply_mock.assert_awaited_once()
    assert context.user_data.get("active_workflow") == "search"


@pytest.mark.asyncio
async def test_plex_status_command_calls_service(mocker, make_message, context):
    status_msg = make_message()
    context.bot.send_message.return_value = status_msg

    update = Update(update_id=1, message=make_message("/status"))

    mocker.patch(
        "telegram_bot.handlers.command_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    service_mock = mocker.patch(
        "telegram_bot.handlers.command_handlers.get_plex_server_status",
        AsyncMock(return_value="All good"),
    )

    await plex_status_command(update, context)

    service_mock.assert_awaited_once_with(context)
