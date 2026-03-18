from unittest.mock import AsyncMock

import pytest
from telegram import Message, Update

from telegram_bot.handlers.message_handlers import handle_link_message, handle_user_message
from telegram_bot.workflows.navigation import mark_chat_workflow_active, return_to_home


@pytest.mark.asyncio
async def test_idle_dm_text_renders_home_menu(mocker, make_message, context):
    msg = make_message("hello")
    update = Update(update_id=1, message=msg)

    mocker.patch(
        "telegram_bot.handlers.message_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    show_home_menu_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.show_home_menu",
        AsyncMock(),
    )

    await handle_user_message(update, context)

    show_home_menu_mock.assert_awaited_once_with(context, msg.chat_id)
    msg.get_bot().delete_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_idle_dm_text_with_active_home_menu_refreshes_menu(mocker, make_message, context):
    msg = make_message("hello")
    update = Update(update_id=1, message=msg)
    context.bot_data["home_menu_messages"] = {msg.chat_id: 42}

    mocker.patch(
        "telegram_bot.handlers.message_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    show_home_menu_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.show_home_menu",
        AsyncMock(),
    )

    await handle_user_message(update, context)

    show_home_menu_mock.assert_awaited_once_with(context, msg.chat_id)
    msg.get_bot().delete_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_active_search_text_routes_to_search_workflow(mocker, make_message, context):
    msg = make_message("query")
    update = Update(update_id=1, message=msg)

    mark_chat_workflow_active(context, msg.chat_id, "search")
    context.user_data["search_session"] = {"media_type": "movie", "step": "title"}

    mocker.patch(
        "telegram_bot.handlers.message_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    search_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.handle_search_workflow",
        AsyncMock(),
    )
    show_home_menu_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.show_home_menu",
        AsyncMock(),
    )

    await handle_user_message(update, context)

    search_mock.assert_awaited_once_with(update, context)
    show_home_menu_mock.assert_not_called()


@pytest.mark.asyncio
async def test_active_delete_text_routes_to_delete_workflow(mocker, make_message, context):
    msg = make_message("query")
    update = Update(update_id=1, message=msg)

    mark_chat_workflow_active(context, msg.chat_id, "delete")
    context.user_data["next_action"] = "delete_tv_show_search"

    mocker.patch(
        "telegram_bot.handlers.message_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    delete_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.handle_delete_workflow",
        AsyncMock(),
    )

    await handle_user_message(update, context)

    delete_mock.assert_awaited_once_with(update, context)


@pytest.mark.asyncio
async def test_idle_magnet_text_does_not_bypass_home_menu(mocker, make_message, context):
    msg = make_message("magnet:?xt=urn:btih:abcdef")
    update = Update(update_id=1, message=msg)

    mocker.patch(
        "telegram_bot.handlers.message_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    show_home_menu_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.show_home_menu",
        AsyncMock(),
    )
    process_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.process_user_input",
        AsyncMock(),
    )

    await handle_user_message(update, context)

    show_home_menu_mock.assert_awaited_once_with(context, msg.chat_id)
    process_mock.assert_not_called()
    msg.get_bot().delete_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_search_state_deletes_user_message_before_menu(mocker, make_message, context):
    msg = make_message("recover")
    update = Update(update_id=1, message=msg)
    context.user_data["active_workflow"] = "search"

    mocker.patch(
        "telegram_bot.handlers.message_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    show_home_menu_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.show_home_menu",
        AsyncMock(),
    )

    await handle_user_message(update, context)

    show_home_menu_mock.assert_awaited_once_with(context, msg.chat_id)
    msg.get_bot().delete_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_delete_state_deletes_user_message_before_menu(mocker, make_message, context):
    msg = make_message("recover")
    update = Update(update_id=1, message=msg)
    context.user_data["active_workflow"] = "delete"

    mocker.patch(
        "telegram_bot.handlers.message_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    show_home_menu_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.show_home_menu",
        AsyncMock(),
    )

    await handle_user_message(update, context)

    show_home_menu_mock.assert_awaited_once_with(context, msg.chat_id)
    msg.get_bot().delete_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_link_state_falls_back_to_home_menu(mocker, make_message, context):
    msg = make_message("recover")
    update = Update(update_id=1, message=msg)
    mark_chat_workflow_active(context, msg.chat_id, "link")

    mocker.patch(
        "telegram_bot.handlers.message_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    show_home_menu_mock = mocker.patch(
        "telegram_bot.ui.home_menu.show_home_menu",
        AsyncMock(),
    )
    link_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.handle_link_message",
        AsyncMock(),
    )

    await handle_user_message(update, context)

    link_mock.assert_not_awaited()
    show_home_menu_mock.assert_awaited_once_with(context, msg.chat_id)
    assert context.bot_data["chat_navigation"][msg.chat_id]["state"] == "idle"


@pytest.mark.asyncio
async def test_cancel_then_plain_search_recovers_home_menu_instead_of_search_workflow(
    mocker, make_message, context
):
    mark_chat_workflow_active(context, 456, "search", prompt_message_id=15)
    context.user_data["search_session"] = {"media_type": "movie", "step": "title"}

    mocker.patch(
        "telegram_bot.handlers.message_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )
    await return_to_home(context, 456)

    msg = make_message("search", message_id=16)
    update = Update(update_id=1, message=msg)
    show_home_menu_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.show_home_menu",
        AsyncMock(),
    )
    search_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.handle_search_workflow",
        AsyncMock(),
    )

    await handle_user_message(update, context)

    search_mock.assert_not_awaited()
    show_home_menu_mock.assert_awaited_once_with(context, msg.chat_id)


@pytest.mark.asyncio
async def test_link_workflow_message_processes_input_waits_for_download_start_or_cancel(
    mocker, make_message, context
):
    msg = make_message("magnet:?xt=urn:btih:abcdef")
    update = Update(update_id=1, message=msg)
    mark_chat_workflow_active(context, msg.chat_id, "link", prompt_message_id=1234)
    context.user_data["link_prompt_message_id"] = 1234

    reply_msg = make_message(message_id=77)
    mocker.patch.object(Message, "reply_text", AsyncMock(return_value=reply_msg))
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
    show_home_menu_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.show_home_menu",
        AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.handlers.message_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )

    await handle_user_message(update, context)

    process_mock.assert_awaited_once_with("magnet:?xt=urn:btih:abcdef", context, reply_msg)
    validate_mock.assert_awaited_once_with("ti", reply_msg)
    confirm_mock.assert_awaited_once_with(reply_msg, context, "ti", {"type": "movie"})
    show_home_menu_mock.assert_not_called()
    context.bot.delete_message.assert_awaited_once_with(chat_id=msg.chat_id, message_id=1234)
    assert "link_prompt_message_id" not in context.user_data
    assert context.user_data.get("active_workflow") == "link"


@pytest.mark.asyncio
async def test_non_link_text_inside_link_workflow_stays_in_workflow(mocker, make_message, context):
    msg = make_message("not-a-link")
    update = Update(update_id=1, message=msg)
    mark_chat_workflow_active(context, msg.chat_id, "link", prompt_message_id=1234)

    reply_msg = make_message(message_id=78)
    mocker.patch.object(Message, "reply_text", AsyncMock(return_value=reply_msg))
    process_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.process_user_input",
        AsyncMock(return_value=None),
    )
    show_home_menu_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.show_home_menu",
        AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.handlers.message_handlers.is_user_authorized",
        AsyncMock(return_value=True),
    )

    await handle_user_message(update, context)

    process_mock.assert_awaited_once_with("not-a-link", context, reply_msg)
    show_home_menu_mock.assert_not_called()
    assert context.user_data.get("active_workflow") == "link"


@pytest.mark.asyncio
async def test_handle_link_message_direct_invocation(mocker, make_message, context):
    msg = make_message("magnet:?xt=urn:btih:abcdef")
    update = Update(update_id=1, message=msg)
    context.user_data["link_prompt_message_id"] = 2222

    reply_msg = make_message(message_id=79)
    mocker.patch.object(Message, "reply_text", AsyncMock(return_value=reply_msg))
    process_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.process_user_input",
        AsyncMock(return_value="ti"),
    )
    mocker.patch(
        "telegram_bot.handlers.message_handlers.validate_and_enrich_torrent",
        AsyncMock(return_value=(None, {"type": "movie"})),
    )
    mocker.patch(
        "telegram_bot.handlers.message_handlers.send_confirmation_prompt",
        AsyncMock(),
    )
    show_home_menu_mock = mocker.patch(
        "telegram_bot.handlers.message_handlers.show_home_menu",
        AsyncMock(),
    )

    await handle_link_message(update, context)

    process_mock.assert_awaited_once_with("magnet:?xt=urn:btih:abcdef", context, reply_msg)
    context.bot.delete_message.assert_awaited_once_with(chat_id=msg.chat_id, message_id=2222)
    show_home_menu_mock.assert_not_called()
