import pytest
from unittest.mock import AsyncMock

from telegram import Update

from telegram_bot.workflows.delete_workflow import (
    handle_delete_buttons,
    handle_delete_workflow,
)


@pytest.mark.asyncio
async def test_delete_show_happy_path(
    mocker, context, make_callback_query, make_message
):
    mocker.patch(
        "telegram_bot.workflows.delete_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    find_media_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow.find_media_by_name",
        new=AsyncMock(return_value="/path/show"),
    )
    delete_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow._delete_item_from_plex",
        new=AsyncMock(return_value=(False, "Could not find")),
    )
    rmtree_mock = mocker.patch("telegram_bot.workflows.delete_workflow.shutil.rmtree")
    mocker.patch(
        "telegram_bot.workflows.delete_workflow.os.path.exists", return_value=True
    )
    mocker.patch(
        "telegram_bot.workflows.delete_workflow.os.path.isdir", return_value=True
    )
    mocker.patch(
        "telegram_bot.workflows.delete_workflow.asyncio.to_thread",
        new=AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs)),
    )

    # Start delete TV workflow
    await handle_delete_buttons(
        Update(
            update_id=1,
            callback_query=make_callback_query("delete_start_tv", make_message()),
        ),
        context,
    )
    assert context.user_data["next_action"] == "delete_tv_show_search"

    # Provide show title
    context.bot_data["SAVE_PATHS"] = {}
    await handle_delete_workflow(
        Update(update_id=2, message=make_message("My Show")), context
    )
    find_media_mock.assert_awaited_once()
    assert context.user_data["show_path_to_delete"] == "/path/show"

    # Choose to delete entire show
    await handle_delete_buttons(
        Update(
            update_id=3,
            callback_query=make_callback_query("delete_tv_all", make_message()),
        ),
        context,
    )
    assert context.user_data["path_to_delete"] == "/path/show"

    # Confirm deletion
    context.bot_data["PLEX_CONFIG"] = {"url": "u", "token": "t"}
    await handle_delete_buttons(
        Update(
            update_id=4,
            callback_query=make_callback_query("confirm_delete", make_message()),
        ),
        context,
    )
    delete_mock.assert_awaited_once_with(
        "/path/show", {"url": "u", "token": "t"}, context
    )
    rmtree_mock.assert_called_once_with("/path/show")


@pytest.mark.asyncio
async def test_delete_workflow_not_found(mocker, context, make_message):
    safe_edit_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.workflows.delete_workflow.find_media_by_name",
        new=AsyncMock(return_value=None),
    )
    context.bot_data["SAVE_PATHS"] = {}
    context.user_data["next_action"] = "delete_tv_show_search"

    await handle_delete_workflow(
        Update(update_id=1, message=make_message("Unknown")), context
    )
    assert (
        "No single TV show directory found" in safe_edit_mock.await_args.kwargs["text"]
    )
