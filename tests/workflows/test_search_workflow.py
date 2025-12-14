import pytest
from unittest.mock import AsyncMock

from telegram import Update

from telegram_bot.workflows.search_session import SearchSession, SearchStep
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
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_movie_years_from_wikipedia",
        new=AsyncMock(return_value=([2010], "Inception")),
    )
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
    session = SearchSession.from_user_data(context.user_data)
    assert session.media_type == "movie"
    assert session.step == SearchStep.TITLE

    # Step 2: user provides title
    await handle_search_workflow(
        Update(update_id=2, message=make_message("Inception")), context
    )
    orchestrate_mock.assert_awaited_once_with("Inception", "movie", context)

    orchestrate_mock.reset_mock()

    # Prepare context for resolution step
    session = SearchSession.from_user_data(context.user_data)
    session.set_final_title("Inception (2010)")
    session.media_type = "movie"
    session.advance(SearchStep.RESOLUTION)
    session.save(context.user_data)

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
async def test_movie_search_uses_cached_year_without_config(
    mocker, context, make_callback_query, make_message
):
    context.bot_data.clear()
    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.get_cached_movie_years",
        return_value=([2013], "Oblivion"),
    )
    fetch_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_movie_years_from_wikipedia",
        new=AsyncMock(),
    )
    prompt_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow._prompt_for_resolution",
        new=AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.search_logic.orchestrate_searches",
        new=AsyncMock(return_value=[]),
    )

    await handle_search_buttons(
        Update(
            update_id=1,
            callback_query=make_callback_query("search_start_movie", make_message()),
        ),
        context,
    )
    await handle_search_workflow(
        Update(update_id=2, message=make_message("Oblivion")), context
    )
    fetch_mock.assert_not_awaited()
    prompt_mock.assert_awaited_once()
    args = prompt_mock.await_args.args
    assert args[2] == "Oblivion (2013)"


@pytest.mark.asyncio
async def test_movie_search_without_config_sets_notice(
    mocker, context, make_callback_query, make_message
):
    context.bot_data.clear()
    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.get_cached_movie_years",
        return_value=None,
    )
    fetch_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_movie_years_from_wikipedia",
        new=AsyncMock(),
    )
    process_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow._process_preliminary_results",
        new=AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.search_logic.orchestrate_searches",
        new=AsyncMock(return_value=[]),
    )

    await handle_search_buttons(
        Update(
            update_id=1,
            callback_query=make_callback_query("search_start_movie", make_message()),
        ),
        context,
    )
    await handle_search_workflow(
        Update(update_id=2, message=make_message("Interstellar")), context
    )
    fetch_mock.assert_not_awaited()
    notice = process_mock.await_args.kwargs.get("notice")
    assert notice and "Search configuration unavailable" in notice


@pytest.mark.asyncio
async def test_search_tv_happy_path(mocker, context, make_callback_query, make_message):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_total_seasons_from_wikipedia",
        new=AsyncMock(return_value=5),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_season_episode_count_from_wikipedia",
        new=AsyncMock(return_value=10),
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
    session = SearchSession.from_user_data(context.user_data)
    assert session.media_type == "tv"
    assert session.step == SearchStep.TITLE

    # Title step
    await handle_search_workflow(
        Update(update_id=2, message=make_message("My Show")), context
    )
    session = SearchSession.from_user_data(context.user_data)
    assert session.step == SearchStep.TV_SEASON
    assert session.title == "My Show"

    # Season step
    await handle_search_workflow(
        Update(update_id=3, message=make_message("1")), context
    )
    session = SearchSession.from_user_data(context.user_data)
    assert session.step == SearchStep.TV_SCOPE
    assert session.season == 1

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
    session = SearchSession.from_user_data(context.user_data)
    assert session.step == SearchStep.TV_EPISODE

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
async def test_tv_title_fast_path_skips_prompts(
    mocker, context, make_callback_query, make_message
):
    send_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_send_message",
        new=AsyncMock(return_value=make_message()),
    )
    seasons_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_total_seasons_from_wikipedia",
        new=AsyncMock(),
    )

    await handle_search_buttons(
        Update(
            update_id=10,
            callback_query=make_callback_query("search_start_tv", make_message()),
        ),
        context,
    )

    await handle_search_workflow(
        Update(update_id=11, message=make_message("The Bear S02E05")), context
    )

    seasons_mock.assert_not_awaited()
    session = SearchSession.from_user_data(context.user_data)
    assert session.step == SearchStep.RESOLUTION
    assert session.season == 2
    assert session.episode == 5
    assert session.tv_scope == "single"
    assert session.final_title == "The Bear S02E05"

    assert send_mock.await_count >= 1


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
    assert "search_session" not in context.user_data


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

    session = SearchSession(media_type="movie")
    session.set_final_title("Movie (2021)")
    session.advance(SearchStep.RESOLUTION)
    session.save(context.user_data)

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
    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_total_seasons_from_wikipedia",
        new=AsyncMock(return_value=5),
    )
    send_mock = mocker.patch.object(
        context.bot, "send_message", AsyncMock(return_value=make_message())
    )
    session = SearchSession(media_type="tv", step=SearchStep.TV_SEASON)
    session.set_title("My Show")
    session.save(context.user_data)
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
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_season_episode_count_from_wikipedia",
        new=AsyncMock(return_value=10),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message", new=AsyncMock()
    )
    send_prompt = mocker.patch(
        "telegram_bot.workflows.search_workflow._send_prompt", new=AsyncMock()
    )
    session = SearchSession(media_type="tv", step=SearchStep.TV_SCOPE, season=1)
    session.set_title("Show")
    session.save(context.user_data)
    update = Update(
        update_id=1,
        callback_query=make_callback_query("search_tv_scope_single", make_message()),
    )
    await handle_search_buttons(update, context)
    session = SearchSession.from_user_data(context.user_data)
    assert session.step == SearchStep.TV_EPISODE
    send_prompt.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_tv_scope_selection_season(
    mocker, context, make_callback_query, make_message
):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_episode_titles_for_season",
        new=AsyncMock(return_value=({}, None)),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.plex_service.get_existing_episodes_for_season",
        new=AsyncMock(return_value=set()),
    )
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
    present_results_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow._present_search_results",
        new=AsyncMock(),
    )
    session = SearchSession(media_type="tv", step=SearchStep.TV_SCOPE, season=1)
    session.set_title("Show")
    session.save(context.user_data)
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
    present_results_mock.assert_awaited_once()
    # Ensure we presented pack candidates as normal results
    args, _ = present_results_mock.await_args
    results_passed = args[2]
    assert isinstance(results_passed, list) and results_passed
    assert results_passed[0]["page_url"] == "pack"


@pytest.mark.asyncio
async def test_handle_tv_scope_selection_season_fallback(
    mocker, context, make_callback_query, make_message
):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_episode_titles_for_season",
        new=AsyncMock(return_value=({1: "Episode 1", 2: "Episode 2"}, None)),
    )
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
    session = SearchSession(media_type="tv", step=SearchStep.TV_SCOPE, season=1)
    session.set_title("Show")
    session.save(context.user_data)
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
    session = SearchSession(media_type="tv", season=1)
    session.season_episode_count = 2
    session.save(context.user_data)
    torrents = [{"link": "a", "parsed_info": {}}, {"link": "b", "parsed_info": {}}]
    await _present_season_download_confirmation(
        make_message(), context, torrents, session=session
    )
    assert context.user_data["pending_season_download"] == torrents
    assert "torrents for" in safe_mock.await_args.kwargs["text"]
    safe_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_present_season_download_confirmation_pack(mocker, context, make_message):
    safe_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message", new=AsyncMock()
    )
    session = SearchSession(media_type="tv", season=1)
    session.season_episode_count = 2
    session.save(context.user_data)
    torrents = [{"link": "a", "parsed_info": {"is_season_pack": True}}]
    await _present_season_download_confirmation(
        make_message(), context, torrents, session=session
    )
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
    session = SearchSession(media_type="tv", season=1)
    session.season_episode_count = 10
    session.save(context.user_data)
    torrents = [{"link": "pack", "parsed_info": {"is_season_pack": True}}]
    message = make_message()
    await _present_season_download_confirmation(
        message, context, torrents, session=session
    )
    kwargs = safe_mock.await_args.kwargs
    # Find the Reject button in the inline keyboard
    keyboard = kwargs["reply_markup"].inline_keyboard
    labels = [btn.text for row in keyboard for btn in row]
    assert "⛔ Reject" in labels


@pytest.mark.asyncio
async def test_handle_reject_season_pack_triggers_individual(
    mocker, context, make_update, make_callback_query, make_message
):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_episode_titles_for_season",
        new=AsyncMock(return_value=({}, None)),
    )
    # Arrange minimal state and mocks
    session = SearchSession(media_type="tv", season=1, resolution="1080p")
    session.set_title("Show")
    session.save(context.user_data)
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


@pytest.mark.asyncio
async def test_entire_season_skips_pack_and_targets_missing(
    mocker, context, make_callback_query, make_message
):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_episode_titles_for_season",
        new=AsyncMock(return_value=({i: f"Ep {i}" for i in range(1, 6)}, None)),
    )
    # Mock messaging and data sources
    mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message", new=AsyncMock()
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_season_episode_count_from_wikipedia",
        new=AsyncMock(return_value=5),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.plex_service.get_existing_episodes_for_season",
        new=AsyncMock(return_value={2, 4}),
    )

    # orchestrate_searches returns results only for targeted episodes
    async def orch_side_effect(query, media_type, ctx, **kwargs):  # noqa: ARG001
        # Only respond with results when searching individual episodes
        m = __import__("re").search(r"S(\d{2})E(\d{2})", query)
        if m:
            ep = int(m.group(2))
            return [{"title": f"Show S01E{ep:02d}", "page_url": f"ep{ep}"}]
        return []

    orch_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.search_logic.orchestrate_searches",
        new=AsyncMock(side_effect=orch_side_effect),
    )

    present_conf_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow._present_season_download_confirmation",
        new=AsyncMock(),
    )

    # Seed state prior to selecting season scope
    session = SearchSession(media_type="tv", step=SearchStep.TV_SCOPE, season=1)
    session.set_title("Show")
    session.save(context.user_data)

    # 1) User chooses Entire Season (populates missing/owned and prompts resolution)
    await handle_search_buttons(
        Update(
            update_id=1,
            callback_query=make_callback_query(
                "search_tv_scope_season", make_message()
            ),
        ),
        context,
    )

    session = SearchSession.from_user_data(context.user_data)
    assert session.season_episode_count == 5
    assert session.existing_episodes == [2, 4]
    assert session.missing_episode_numbers == [1, 3, 5]

    # 2) User chooses resolution which triggers season search
    await handle_search_buttons(
        Update(
            update_id=2,
            callback_query=make_callback_query(
                "search_resolution_1080p", make_message()
            ),
        ),
        context,
    )

    # Should have invoked episode searches only for missing [1,3,5]
    searched_eps = []
    for call in orch_mock.await_args_list:
        q = call.args[0]
        m = __import__("re").search(r"S01E(\d{2})", q)
        if m:
            searched_eps.append(int(m.group(1)))
    assert sorted(set(searched_eps)) == [1, 3, 5]

    # And present confirmation with exactly those episodes
    torrents = present_conf_mock.await_args.args[2]
    assert {t["parsed_info"]["episode"] for t in torrents} == {1, 3, 5}


@pytest.mark.asyncio
async def test_entire_season_all_owned_exits_early(
    mocker, context, make_callback_query, make_message
):
    edit_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message", new=AsyncMock()
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_season_episode_count_from_wikipedia",
        new=AsyncMock(return_value=3),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.plex_service.get_existing_episodes_for_season",
        new=AsyncMock(return_value={1, 2, 3}),
    )
    prompt_res_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow._prompt_for_resolution",
        new=AsyncMock(),
    )

    # Seed title/season
    session = SearchSession(media_type="tv", step=SearchStep.TV_SCOPE, season=1)
    session.set_title("Show")
    session.save(context.user_data)

    await handle_search_buttons(
        Update(
            update_id=1,
            callback_query=make_callback_query(
                "search_tv_scope_season", make_message()
            ),
        ),
        context,
    )

    # Should have detected all episodes present and not prompted for resolution
    session = SearchSession.from_user_data(context.user_data)
    assert session.missing_episode_numbers == []
    prompt_res_mock.assert_not_awaited()
    # Confirm we informed the user
    assert "already exist" in (edit_mock.await_args.kwargs.get("text") or "")
