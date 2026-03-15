from unittest.mock import AsyncMock

import pytest
from telegram import CallbackQuery, Update

from telegram_bot.handlers.callback_handlers import button_handler


@pytest.mark.asyncio
async def test_home_search_callback_routes_launcher(
    mocker, make_callback_query, context, make_message
):
    mocker.patch.object(CallbackQuery, "answer", AsyncMock())
    message = make_message(message_id=10)
    query = make_callback_query("home_search", message)
    update = Update(update_id=1, callback_query=query)

    context.bot_data["home_menu_messages"] = {message.chat_id: 10}

    mocker.patch(
        "telegram_bot.handlers.callback_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    launcher_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.launch_search_workflow",
        AsyncMock(),
    )

    await button_handler(update, context)

    launcher_mock.assert_awaited_once_with(context, message.chat_id)


@pytest.mark.asyncio
async def test_home_callback_from_stale_message_refreshes_menu(
    mocker, make_callback_query, context, make_message
):
    mocker.patch.object(CallbackQuery, "answer", AsyncMock())
    stale_message = make_message(message_id=3)
    query = make_callback_query("home_delete", stale_message)
    update = Update(update_id=1, callback_query=query)

    context.bot_data["home_menu_messages"] = {stale_message.chat_id: 99}

    mocker.patch(
        "telegram_bot.handlers.callback_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    show_home_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.show_home_menu",
        AsyncMock(),
    )
    launcher_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.launch_delete_workflow",
        AsyncMock(),
    )

    await button_handler(update, context)

    show_home_mock.assert_awaited_once_with(context, stale_message.chat_id)
    launcher_mock.assert_not_called()


@pytest.mark.asyncio
async def test_unauthorized_callback_rejected_before_home_logic(
    mocker, make_callback_query, context, make_message
):
    mocker.patch.object(CallbackQuery, "answer", AsyncMock())
    message = make_message(message_id=5)
    query = make_callback_query("home_help", message)
    update = Update(update_id=1, callback_query=query)

    mocker.patch(
        "telegram_bot.handlers.callback_handlers.is_user_authorized",
        AsyncMock(return_value=False),
    )
    launcher_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.launch_help",
        AsyncMock(),
    )

    await button_handler(update, context)

    launcher_mock.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_operation_routes_return_to_home(
    mocker, make_callback_query, context, make_message
):
    mocker.patch.object(CallbackQuery, "answer", AsyncMock())
    message = make_message(message_id=7)
    query = make_callback_query("cancel_operation", message)
    update = Update(update_id=1, callback_query=query)

    mocker.patch(
        "telegram_bot.handlers.callback_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    return_home_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.return_to_home",
        AsyncMock(),
    )

    await button_handler(update, context)

    return_home_mock.assert_awaited_once()
    kwargs = return_home_mock.await_args.kwargs
    assert kwargs["message_text"] == "Operation cancelled\\."


@pytest.mark.asyncio
async def test_non_home_search_callback_still_routes_search(
    mocker, make_callback_query, context, make_message
):
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
