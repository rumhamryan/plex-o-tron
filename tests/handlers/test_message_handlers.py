import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

import pytest
from telegram import Update, Message


from telegram_bot.handlers.message_handlers import (
    handle_link_message,
    handle_search_message,
)


@pytest.mark.asyncio
async def test_handle_link_message_processes_input(mocker, make_message, context):
    msg = make_message("magnet:?xt=urn:btih:abcdef")
    update = Update(update_id=1, message=msg)

    mocker.patch(
        "telegram_bot.handlers.message_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    reply_msg = make_message()
    reply_mock = mocker.patch.object(
        Message, "reply_text", AsyncMock(return_value=reply_msg)
    )
    process_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.process_user_input",
        AsyncMock(return_value="ti"),
    )
    validate_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.validate_and_enrich_torrent",
        AsyncMock(return_value=(None, {"type": "movie"})),
    )
    confirm_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.send_confirmation_prompt",
        AsyncMock(),
    )

    await handle_link_message(update, context)

    reply_mock.assert_awaited_once()
    process_mock.assert_awaited_once_with(
        "magnet:?xt=urn:btih:abcdef", context, reply_msg
    )
    validate_mock.assert_awaited_once_with("ti", reply_msg)
    confirm_mock.assert_awaited_once_with(reply_msg, context, "ti", {"type": "movie"})


@pytest.mark.asyncio
async def test_handle_search_message_routes_search(mocker, make_message, context):
    msg = make_message("query")
    update = Update(update_id=1, message=msg)

    mocker.patch(
        "telegram_bot.handlers.message_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    handler_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.handle_search_workflow",
        AsyncMock(),
    )
    context.user_data["active_workflow"] = "search"

    await handle_search_message(update, context)

    handler_mock.assert_awaited_once_with(update, context)


@pytest.mark.asyncio
async def test_handle_search_message_routes_delete(mocker, make_message, context):
    msg = make_message("query")
    update = Update(update_id=1, message=msg)

    mocker.patch(
        "telegram_bot.handlers.message_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    handler_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.handle_delete_workflow",
        AsyncMock(),
    )
    context.user_data["active_workflow"] = "delete"

    await handle_search_message(update, context)

    handler_mock.assert_awaited_once_with(update, context)
