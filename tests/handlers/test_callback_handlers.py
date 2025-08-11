import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from telegram import Update, CallbackQuery

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from telegram_bot.handlers.callback_handlers import button_handler


@pytest.mark.asyncio
async def test_button_handler_routes_search(mocker, make_callback_query, context, make_message):
    mocker.patch.object(CallbackQuery, "answer", AsyncMock())
    query = make_callback_query("search_start_movie", make_message())
    update = Update(update_id=1, callback_query=query)

    mocker.patch(
        "telegram_bot.handlers.callback_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    handler_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.handle_search_buttons",
        AsyncMock(),
    )

    await button_handler(update, context)

    handler_mock.assert_awaited_once_with(update, context)


@pytest.mark.asyncio
async def test_button_handler_routes_delete(mocker, make_callback_query, context, make_message):
    mocker.patch.object(CallbackQuery, "answer", AsyncMock())
    query = make_callback_query("delete_start_tv", make_message())
    update = Update(update_id=1, callback_query=query)

    mocker.patch(
        "telegram_bot.handlers.callback_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    handler_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.handle_delete_buttons",
        AsyncMock(),
    )

    await button_handler(update, context)

    handler_mock.assert_awaited_once_with(update, context)


@pytest.mark.asyncio
async def test_button_handler_routes_download(mocker, make_callback_query, context, make_message):
    mocker.patch.object(CallbackQuery, "answer", AsyncMock())
    query = make_callback_query("confirm_download", make_message())
    update = Update(update_id=1, callback_query=query)

    mocker.patch(
        "telegram_bot.handlers.callback_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    handler_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.add_download_to_queue",
        AsyncMock(),
    )

    await button_handler(update, context)

    handler_mock.assert_awaited_once_with(update, context)
