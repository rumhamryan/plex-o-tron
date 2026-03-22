from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from telegram_bot.ui.home_menu import (
    build_home_menu_markup,
    clear_home_menu_message_id,
    delete_home_menu_message,
    get_home_menu_message_id,
    set_home_menu_message_id,
    show_home_menu,
)


def test_build_home_menu_markup_layout():
    markup = build_home_menu_markup()
    callback_rows = [[button.callback_data for button in row] for row in markup.inline_keyboard]

    assert callback_rows == [
        ["home_search", "home_delete"],
        ["home_track", "home_link"],
        ["home_status", "home_restart"],
        ["home_help"],
    ]


def test_home_menu_message_id_storage():
    application = SimpleNamespace(bot_data={})
    assert get_home_menu_message_id(application, 456) is None

    set_home_menu_message_id(application, 456, 12)
    assert get_home_menu_message_id(application, 456) == 12
    assert application.bot_data["chat_navigation"][456]["home_menu_message_id"] == 12

    clear_home_menu_message_id(application, 456)
    assert get_home_menu_message_id(application, 456) is None
    assert application.bot_data["chat_navigation"][456]["home_menu_message_id"] is None


@pytest.mark.asyncio
async def test_show_home_menu_falls_back_to_send_when_canonical_edit_fails(context, make_message):
    context.bot_data["home_menu_messages"] = {456: 12}
    context.application = SimpleNamespace(bot_data=context.bot_data)
    context.bot.edit_message_text = AsyncMock(side_effect=BadRequest("Message to edit not found"))
    fresh_message = make_message(message_id=99)
    context.bot.send_message = AsyncMock(return_value=fresh_message)

    rendered = await show_home_menu(context, 456)

    assert rendered.message_id == 99
    assert context.bot_data["home_menu_messages"][456] == 99
    assert context.bot_data["chat_navigation"][456]["home_menu_message_id"] == 99
    context.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_show_home_menu_replaces_id_after_target_edit_failure(mocker, context, make_message):
    context.application = SimpleNamespace(bot_data=context.bot_data)
    target_message = make_message(message_id=14)
    mocker.patch.object(
        type(target_message),
        "edit_text",
        AsyncMock(side_effect=BadRequest("Message can't be edited")),
    )
    fresh_message = make_message(message_id=33)
    context.bot.send_message = AsyncMock(return_value=fresh_message)

    rendered = await show_home_menu(context, 456, target_message=target_message)

    assert rendered.message_id == 33
    assert context.bot_data["home_menu_messages"][456] == 33
    assert context.bot_data["chat_navigation"][456]["home_menu_message_id"] == 33


@pytest.mark.asyncio
async def test_delete_home_menu_message_clears_stored_message_id(context):
    context.bot_data["home_menu_messages"] = {456: 12}
    context.application = SimpleNamespace(bot_data=context.bot_data)
    context.bot.delete_message = AsyncMock()

    await delete_home_menu_message(context, 456)

    context.bot.delete_message.assert_awaited_once_with(chat_id=456, message_id=12)
    assert 456 not in context.bot_data["home_menu_messages"]
    assert context.bot_data["chat_navigation"][456]["home_menu_message_id"] is None


@pytest.mark.asyncio
async def test_delete_home_menu_message_clears_id_even_when_delete_fails(context):
    context.bot_data["home_menu_messages"] = {456: 12}
    context.application = SimpleNamespace(bot_data=context.bot_data)
    context.bot.delete_message = AsyncMock(side_effect=BadRequest("Message to delete not found"))

    await delete_home_menu_message(context, 456)

    assert 456 not in context.bot_data["home_menu_messages"]
    assert context.bot_data["chat_navigation"][456]["home_menu_message_id"] is None
