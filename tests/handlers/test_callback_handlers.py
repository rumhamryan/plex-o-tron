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
    delete_home_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.delete_home_menu_message",
        AsyncMock(),
    )
    show_home_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.show_home_menu",
        AsyncMock(),
    )
    launcher_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.launch_search_workflow",
        AsyncMock(),
    )

    await button_handler(update, context)

    delete_home_mock.assert_awaited_once_with(context, message.chat_id)
    show_home_mock.assert_not_called()
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
@pytest.mark.parametrize(
    ("action", "launcher_attr"),
    [
        ("home_delete", "launch_delete_workflow"),
        ("home_link", "launch_link_workflow"),
    ],
)
async def test_home_workflow_actions_consume_menu_without_immediate_rerender(
    action, launcher_attr, mocker, make_callback_query, context, make_message
):
    mocker.patch.object(CallbackQuery, "answer", AsyncMock())
    message = make_message(message_id=21)
    query = make_callback_query(action, message)
    update = Update(update_id=1, callback_query=query)
    context.bot_data["home_menu_messages"] = {message.chat_id: 21}

    mocker.patch(
        "telegram_bot.handlers.callback_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    delete_home_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.delete_home_menu_message",
        AsyncMock(),
    )
    show_home_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.show_home_menu",
        AsyncMock(),
    )
    launcher_mock = mocker.patch(
        f"telegram_bot.handlers.callback_handlers.{launcher_attr}",
        AsyncMock(),
    )

    await button_handler(update, context)

    delete_home_mock.assert_awaited_once_with(context, message.chat_id)
    launcher_mock.assert_awaited_once_with(context, message.chat_id)
    show_home_mock.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "launcher_attr"),
    [
        ("home_help", "launch_help"),
        ("home_status", "launch_plex_status"),
        ("home_restart", "launch_plex_restart"),
    ],
)
async def test_home_utility_actions_consume_and_rerender_menu(
    action, launcher_attr, mocker, make_callback_query, context, make_message
):
    mocker.patch.object(CallbackQuery, "answer", AsyncMock())
    message = make_message(message_id=22)
    query = make_callback_query(action, message)
    update = Update(update_id=1, callback_query=query)
    context.bot_data["home_menu_messages"] = {message.chat_id: 22}

    mocker.patch(
        "telegram_bot.handlers.callback_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    delete_home_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.delete_home_menu_message",
        AsyncMock(),
    )
    show_home_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.show_home_menu",
        AsyncMock(),
    )
    launcher_mock = mocker.patch(
        f"telegram_bot.handlers.callback_handlers.{launcher_attr}",
        AsyncMock(),
    )

    await button_handler(update, context)

    delete_home_mock.assert_awaited_once_with(context, message.chat_id)
    launcher_mock.assert_awaited_once_with(context, message.chat_id)
    show_home_mock.assert_awaited_once_with(context, message.chat_id)


@pytest.mark.asyncio
async def test_home_refresh_consumes_and_rerenders_menu(
    mocker, make_callback_query, context, make_message
):
    mocker.patch.object(CallbackQuery, "answer", AsyncMock())
    message = make_message(message_id=23)
    query = make_callback_query("home_refresh", message)
    update = Update(update_id=1, callback_query=query)
    context.bot_data["home_menu_messages"] = {message.chat_id: 23}

    mocker.patch(
        "telegram_bot.handlers.callback_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    delete_home_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.delete_home_menu_message",
        AsyncMock(),
    )
    show_home_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.show_home_menu",
        AsyncMock(),
    )

    await button_handler(update, context)

    delete_home_mock.assert_awaited_once_with(context, message.chat_id)
    show_home_mock.assert_awaited_once_with(context, message.chat_id)


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
    assert kwargs["replace_home_menu"] is True


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "queue_fn"),
    [
        ("confirm_download", "add_download_to_queue"),
        ("confirm_season_download", "add_season_to_queue"),
        ("confirm_collection_download", "add_collection_to_queue"),
    ],
)
async def test_confirm_download_actions_rerender_home_only_when_started(
    action, queue_fn, mocker, make_callback_query, context, make_message
):
    mocker.patch.object(CallbackQuery, "answer", AsyncMock())
    message = make_message(message_id=31)
    query = make_callback_query(action, message)
    update = Update(update_id=1, callback_query=query)

    mocker.patch(
        "telegram_bot.handlers.callback_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    queue_mock = mocker.patch(
        f"telegram_bot.handlers.callback_handlers.{queue_fn}",
        AsyncMock(return_value=True),
    )
    show_home_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.show_home_menu",
        AsyncMock(),
    )

    await button_handler(update, context)

    queue_mock.assert_awaited_once_with(update, context)
    show_home_mock.assert_awaited_once_with(context, message.chat_id)


@pytest.mark.asyncio
async def test_confirm_download_does_not_rerender_home_when_queued_only(
    mocker, make_callback_query, context, make_message
):
    mocker.patch.object(CallbackQuery, "answer", AsyncMock())
    message = make_message(message_id=32)
    query = make_callback_query("confirm_download", message)
    update = Update(update_id=1, callback_query=query)

    mocker.patch(
        "telegram_bot.handlers.callback_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    queue_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.add_download_to_queue",
        AsyncMock(return_value=False),
    )
    show_home_mock = mocker.patch(
        "telegram_bot.handlers.callback_handlers.show_home_menu",
        AsyncMock(),
    )

    await button_handler(update, context)

    queue_mock.assert_awaited_once_with(update, context)
    show_home_mock.assert_not_called()
