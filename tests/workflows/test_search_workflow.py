import pytest
from unittest.mock import AsyncMock

from telegram import Update

from telegram_bot.workflows.search_workflow import (
    handle_search_buttons,
    handle_search_workflow,
    _clear_search_context,
    _present_season_download_confirmation,
)


@pytest.mark.asyncio
async def test_search_movie_happy_path(
    mocker, context, make_callback_query, make_message
):
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
    start_update = Update(
        update_id=1,
        callback_query=make_callback_query("search_start_movie", make_message()),
    )
    await handle_search_buttons(start_update, context)
    assert context.user_data["next_action"] == "search_movie_get_title"

    # Step 2: user provides title
    await handle_search_workflow(
        Update(update_id=2, message=make_message("Inception")), context
    )
    orchestrate_mock.assert_awaited_once_with("Inception", "movie", context)

    orchestrate_mock.reset_mock()

    # Prepare context for resolution step
    context.user_data["search_final_title"] = "Inception (2010)"
    context.user_data["search_media_type"] = "movie"

    # Step 5: resolution button triggers final search
    await handle_search_buttons(
        Update(
            update_id=3,
            callback_query=make_callback_query(
                "search_resolution_1080p", make_message()
            ),
        ),
        context,
    )
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
    mocker.patch("telegram_bot.workflows.search_workflow._send_prompt", new=AsyncMock())
    orchestrate_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.search_logic.orchestrate_searches",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow._present_search_results",
        new=AsyncMock(),
    )

    # Start TV search
    await handle_search_buttons(
        Update(
            update_id=1,
            callback_query=make_callback_query("search_start_tv", make_message()),
        ),
        context,
    )
    assert context.user_data["next_action"] == "search_tv_get_title"

    # Title step
    await handle_search_workflow(
        Update(update_id=2, message=make_message("My Show")), context
    )
    assert context.user_data["next_action"] == "search_tv_get_season"
    assert context.user_data["search_query_title"] == "My Show"

    # Season step
    await handle_search_workflow(
        Update(update_id=3, message=make_message("1")), context
    )
    assert context.user_data["next_action"] == "search_tv_scope"
    assert context.user_data["search_season_number"] == 1

    # Scope selection to single episode
    await handle_search_buttons(
        Update(
            update_id=4,
            callback_query=make_callback_query(
                "search_tv_scope_single", make_message()
            ),
        ),
        context,
    )
    assert context.user_data["next_action"] == "search_tv_get_episode"

    # Episode step collects input and prompts for resolution
    await handle_search_workflow(
        Update(update_id=5, message=make_message("2")), context
    )

    # Select resolution to trigger the search
    await handle_search_buttons(
        Update(
            update_id=6,
            callback_query=make_callback_query(
                "search_resolution_1080p", make_message()
            ),
        ),
        context,
    )
    orchestrate_mock.assert_awaited_once_with(
        "My Show S01E02", "tv", context, base_query_for_filter="My Show"
    )


@pytest.mark.asyncio
async def test_search_cancel_clears_context(
    mocker, context, make_callback_query, make_message
):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    start_update = Update(
        update_id=1,
        callback_query=make_callback_query("search_start_movie", make_message()),
    )
    await handle_search_buttons(start_update, context)
    assert context.user_data

    _clear_search_context(context)
    assert "active_workflow" not in context.user_data
    assert "next_action" not in context.user_data


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_data,expected_titles",
    [
        ("search_resolution_4k", ["Movie 2160p", "Movie 4K"]),
        ("search_resolution_1080p", ["Movie 1080p"]),
    ],
)
async def test_resolution_filters_results(
    mocker, context, make_callback_query, make_message, callback_data, expected_titles
):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    sample_results = [
        {"title": "Movie 1080p"},
        {"title": "Movie 2160p"},
        {"title": "Movie 4K"},
    ]
    mocker.patch(
        "telegram_bot.workflows.search_workflow.search_logic.orchestrate_searches",
        new=AsyncMock(return_value=sample_results),
    )
    present_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow._present_search_results",
        new=AsyncMock(),
    )

    context.user_data["search_final_title"] = "Movie (2021)"
    context.user_data["search_media_type"] = "movie"

    message = make_message()
    await handle_search_buttons(
        Update(
            update_id=1,
            callback_query=make_callback_query(callback_data, message),
        ),
        context,
    )

    assert present_mock.await_count == 1
    filtered = present_mock.await_args.args[2]
    assert [r["title"] for r in filtered] == expected_titles


@pytest.mark.asyncio
async def test_tv_season_reply_offers_scope_buttons(mocker, context, make_message):
    send_mock = mocker.patch.object(
        context.bot, "send_message", AsyncMock(return_value=make_message())
    )
    context.user_data["next_action"] = "search_tv_get_season"
    context.user_data["search_query_title"] = "My Show"
    await handle_search_workflow(
        Update(update_id=1, message=make_message("1")), context
    )
    send_mock.assert_awaited_once()
    kwargs = send_mock.await_args.kwargs
    keyboard = kwargs["reply_markup"].inline_keyboard
    assert keyboard[0][0].callback_data == "search_tv_scope_single"
    assert keyboard[0][1].callback_data == "search_tv_scope_season"


@pytest.mark.asyncio
async def test_handle_tv_scope_selection_single(
    mocker, context, make_callback_query, make_message
):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message", new=AsyncMock()
    )
    send_prompt = mocker.patch(
        "telegram_bot.workflows.search_workflow._send_prompt", new=AsyncMock()
    )
    context.user_data["search_query_title"] = "Show"
    context.user_data["search_season_number"] = 1
    update = Update(
        update_id=1,
        callback_query=make_callback_query("search_tv_scope_single", make_message()),
    )
    await handle_search_buttons(update, context)
    assert context.user_data["next_action"] == "search_tv_get_episode"
    send_prompt.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_tv_scope_selection_season(
    mocker, context, make_callback_query, make_message
):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message", new=AsyncMock()
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_season_episode_count_from_wikipedia",
        new=AsyncMock(return_value=2),
    )
    orch_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.search_logic.orchestrate_searches",
        new=AsyncMock(
            side_effect=[
                [{"title": "Show Season 1 Complete", "page_url": "pack", "score": 10}],
                [],
            ]
        ),
    )
    present_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow._present_season_download_confirmation",
        new=AsyncMock(),
    )
    context.user_data["search_query_title"] = "Show"
    context.user_data["search_season_number"] = 1
    # Select season scope
    update = Update(
        update_id=1,
        callback_query=make_callback_query("search_tv_scope_season", make_message()),
    )
    await handle_search_buttons(update, context)

    # Resolution gating: select a resolution to trigger the search
    res_update = Update(
        update_id=2,
        callback_query=make_callback_query("search_resolution_1080p", make_message()),
    )
    await handle_search_buttons(res_update, context)

    assert orch_mock.call_count == 2
    present_mock.assert_awaited_once()
    passed = present_mock.await_args.args[2]
    assert passed[0]["link"] == "pack"
    assert passed[0]["parsed_info"]["is_season_pack"]


@pytest.mark.asyncio
async def test_handle_tv_scope_selection_season_fallback(
    mocker, context, make_callback_query, make_message
):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message", new=AsyncMock()
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_season_episode_count_from_wikipedia",
        new=AsyncMock(return_value=2),
    )

    orch_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.search_logic.orchestrate_searches",
        new=AsyncMock(
            side_effect=[[], [], [{"title": "Show S01E01", "page_url": "e1"}], []]
        ),
    )
    present_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow._present_season_download_confirmation",
        new=AsyncMock(),
    )
    context.user_data["search_query_title"] = "Show"
    context.user_data["search_season_number"] = 1
    # Select season scope
    update = Update(
        update_id=1,
        callback_query=make_callback_query("search_tv_scope_season", make_message()),
    )
    await handle_search_buttons(update, context)

    # Resolution gating: select a resolution to trigger the search
    res_update = Update(
        update_id=2,
        callback_query=make_callback_query("search_resolution_1080p", make_message()),
    )
    await handle_search_buttons(res_update, context)

    assert orch_mock.call_count == 4

    # Episode searches should use the plain title for fuzzy filtering
    assert orch_mock.await_args_list[2].kwargs["base_query_for_filter"] == "Show"
    assert orch_mock.await_args_list[3].kwargs["base_query_for_filter"] == "Show"
    passed = present_mock.await_args.args[2]
    assert passed[0]["link"] == "e1"
    assert passed[0]["parsed_info"]["episode"] == 1


@pytest.mark.asyncio
async def test_present_season_download_confirmation(mocker, context, make_message):
    safe_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message", new=AsyncMock()
    )
    context.user_data["search_season_number"] = 1
    context.user_data["season_episode_count"] = 2
    torrents = [{"link": "a", "parsed_info": {}}, {"link": "b", "parsed_info": {}}]
    await _present_season_download_confirmation(make_message(), context, torrents)
    assert context.user_data["pending_season_download"] == torrents
    assert "torrents for" in safe_mock.await_args.kwargs["text"]
    safe_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_present_season_download_confirmation_pack(mocker, context, make_message):
    safe_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message", new=AsyncMock()
    )
    context.user_data["search_season_number"] = 1
    context.user_data["season_episode_count"] = 2
    torrents = [{"link": "a", "parsed_info": {"is_season_pack": True}}]
    await _present_season_download_confirmation(make_message(), context, torrents)
    assert context.user_data["pending_season_download"] == torrents
    assert "season pack" in safe_mock.await_args.kwargs["text"].lower()
    safe_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_present_season_download_confirmation_pack_has_reject_button(
    mocker, context, make_message
):
    safe_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message", new=AsyncMock()
    )
    context.user_data["search_season_number"] = 1
    context.user_data["season_episode_count"] = 10
    torrents = [{"link": "pack", "parsed_info": {"is_season_pack": True}}]
    message = make_message()
    await _present_season_download_confirmation(message, context, torrents)
    kwargs = safe_mock.await_args.kwargs
    # Find the Reject button in the inline keyboard
    keyboard = kwargs["reply_markup"].inline_keyboard
    labels = [btn.text for row in keyboard for btn in row]
    assert "Reject" in labels


@pytest.mark.asyncio
async def test_handle_reject_season_pack_triggers_individual(
    mocker, context, make_update, make_callback_query, make_message
):
    # Arrange minimal state and mocks
    context.user_data["tv_base_title"] = "Show"
    context.user_data["search_season_number"] = 1
    context.user_data["search_resolution"] = "1080p"
    safe_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message", new=AsyncMock()
    )
    perf_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow._perform_tv_season_search_with_resolution",
        new=AsyncMock(),
    )

    message = make_message()
    update = make_update(
        callback_query=make_callback_query("reject_season_pack", message)
    )

    from telegram_bot.workflows.search_workflow import handle_reject_season_pack

    await handle_reject_season_pack(update, context)

    safe_mock.assert_awaited()
    perf_mock.assert_awaited_once()
    args, kwargs = perf_mock.await_args
    # Assert forced individual episodes
    assert kwargs.get("force_individual_episodes") is True
