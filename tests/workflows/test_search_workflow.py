import pytest
from unittest.mock import AsyncMock

from telegram import Update

from telegram_bot.workflows.search_workflow import (
    handle_search_buttons,
    handle_search_workflow,
    _clear_search_context,
)


@pytest.mark.asyncio
async def test_search_movie_happy_path(mocker, context, make_callback_query, make_message):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    orchestrate_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.search_logic.orchestrate_searches",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow._process_preliminary_results",
        new=AsyncMock(),
    )

    # Step 1: press start movie button
    start_update = Update(update_id=1, callback_query=make_callback_query("search_start_movie", make_message()))
    await handle_search_buttons(start_update, context)
    assert context.user_data["next_action"] == "search_movie_get_title"

    # Step 2: user provides title
    await handle_search_workflow(Update(update_id=2, message=make_message("Inception")), context)
    orchestrate_mock.assert_awaited_once_with("Inception", "movie", context)

    orchestrate_mock.reset_mock()

    # Prepare context for resolution step
    context.user_data["search_final_title"] = "Inception (2010)"
    context.user_data["search_media_type"] = "movie"

    # Step 5: resolution button triggers final search
    await handle_search_buttons(Update(update_id=3, callback_query=make_callback_query("search_resolution_1080p", make_message())), context)
    orchestrate_mock.assert_awaited_once_with(
        "Inception",
        "movie",
        context,
        year="2010",
        resolution="1080p",
    )


@pytest.mark.asyncio
async def test_search_tv_happy_path(mocker, context, make_callback_query, make_message):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    orchestrate_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.search_logic.orchestrate_searches",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow._present_search_results",
        new=AsyncMock(),
    )

    # Start TV search
    await handle_search_buttons(Update(update_id=1, callback_query=make_callback_query("search_start_tv", make_message())), context)
    assert context.user_data["next_action"] == "search_tv_get_title"

    # Title step
    await handle_search_workflow(Update(update_id=2, message=make_message("My Show")), context)
    assert context.user_data["next_action"] == "search_tv_get_season"
    assert context.user_data["search_query_title"] == "My Show"

    # Season step
    await handle_search_workflow(Update(update_id=3, message=make_message("1")), context)
    assert context.user_data["next_action"] == "search_tv_get_episode"
    assert context.user_data["search_season_number"] == 1

    # Episode step triggers search
    await handle_search_workflow(Update(update_id=4, message=make_message("2")), context)
    orchestrate_mock.assert_awaited_once_with("My Show S01E02", "tv", context)


@pytest.mark.asyncio
async def test_search_cancel_clears_context(mocker, context, make_callback_query, make_message):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    start_update = Update(update_id=1, callback_query=make_callback_query("search_start_movie", make_message()))
    await handle_search_buttons(start_update, context)
    assert context.user_data

    _clear_search_context(context)
    assert "active_workflow" not in context.user_data
    assert "next_action" not in context.user_data
