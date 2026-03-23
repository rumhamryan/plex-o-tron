from unittest.mock import AsyncMock

import pytest

from telegram_bot.handlers.command_handlers import (
    get_help_message_text,
    launch_delete_workflow,
    launch_link_workflow,
    launch_plex_restart,
    launch_plex_status,
    launch_search_workflow,
    launch_tracking_workflow,
)


@pytest.mark.asyncio
async def test_launch_search_workflow_sets_active_state(context):
    await launch_search_workflow(context, chat_id=456)

    assert context.user_data.get("active_workflow") == "search"
    assert context.bot_data["chat_navigation"][456]["state"] == "search"
    assert context.bot_data["chat_navigation"][456]["active_prompt_message_id"] == 1
    context.bot.send_message.assert_awaited_once()
    sent_text = context.bot.send_message.await_args.kwargs["text"]
    assert "search for" in sent_text.lower()


@pytest.mark.asyncio
async def test_launch_link_workflow_sets_active_state(context):
    await launch_link_workflow(context, chat_id=456)

    assert context.user_data.get("active_workflow") == "link"
    assert context.user_data.get("link_prompt_message_id") is not None
    assert context.bot_data["chat_navigation"][456]["state"] == "link"
    assert context.bot_data["chat_navigation"][456]["active_prompt_message_id"] == 1
    context.bot.send_message.assert_awaited_once()
    sent_text = context.bot.send_message.await_args.kwargs["text"]
    assert "\\.torrent" in sent_text
    assert context.bot.send_message.await_args.kwargs["parse_mode"] == "MarkdownV2"


@pytest.mark.asyncio
async def test_launch_delete_workflow_sets_active_state(context):
    await launch_delete_workflow(context, chat_id=456)

    assert context.user_data.get("active_workflow") == "delete"
    assert context.bot_data["chat_navigation"][456]["state"] == "delete"
    assert context.bot_data["chat_navigation"][456]["active_prompt_message_id"] == 1


@pytest.mark.asyncio
async def test_launch_tracking_workflow_sets_active_state(mocker, context):
    render_mock = mocker.patch(
        "telegram_bot.handlers.command_handlers.render_tracking_menu",
        AsyncMock(),
    )

    await launch_tracking_workflow(context, chat_id=456)

    assert context.user_data.get("active_workflow") == "track"
    assert context.bot_data["chat_navigation"][456]["state"] == "track"
    render_mock.assert_awaited_once_with(context, 456)


@pytest.mark.asyncio
async def test_launch_plex_status_calls_service(mocker, context):
    status_msg = AsyncMock()
    context.bot.send_message.return_value = status_msg

    service_mock = mocker.patch(
        "telegram_bot.handlers.command_handlers.get_plex_server_status",
        AsyncMock(return_value="All good"),
    )

    await launch_plex_status(context, chat_id=456)

    service_mock.assert_awaited_once_with(context)
    status_msg.edit_text.assert_awaited_once_with(text="All good", parse_mode="MarkdownV2")


@pytest.mark.asyncio
async def test_launch_plex_restart_success_uses_markdown(mocker, context):
    status_msg = AsyncMock()
    context.bot.send_message.return_value = status_msg
    restart_mock = mocker.patch(
        "telegram_bot.handlers.command_handlers.restart_plex_server",
        AsyncMock(return_value=(True, "")),
    )

    await launch_plex_restart(context, chat_id=456)

    restart_mock.assert_awaited_once_with()
    status_msg.edit_text.assert_awaited_once_with(
        text="✅ *Plex Restart Successful*",
        parse_mode="MarkdownV2",
    )


@pytest.mark.asyncio
async def test_launch_plex_restart_failure_sends_plain_details(mocker, context):
    status_msg = AsyncMock()
    context.bot.send_message.return_value = status_msg
    restart_mock = mocker.patch(
        "telegram_bot.handlers.command_handlers.restart_plex_server",
        AsyncMock(
            return_value=(
                False,
                "Restart script failed.\n\nScript output:\nError: Could not locate Plex executable.",
            )
        ),
    )

    await launch_plex_restart(context, chat_id=456)

    restart_mock.assert_awaited_once_with()
    status_msg.edit_text.assert_awaited_once_with(
        text=(
            "❌ Plex Restart Failed\n\n"
            "Restart script failed.\n\nScript output:\n"
            "Error: Could not locate Plex executable."
        )
    )


def test_help_text_does_not_advertise_command_or_idle_link_entrypoints():
    message = get_help_message_text().lower()
    assert "/search" not in message
    assert "just paste a magnet" not in message
    assert "auto\\-download" in message
