import time
import pytest
from unittest.mock import AsyncMock

from telegram import Update

from telegram_bot.workflows.search_session import SearchSession, SearchStep
from telegram_bot.workflows.search_workflow import (
    handle_search_buttons,
    handle_search_workflow,
    _clear_search_context,
    _present_season_download_confirmation,
    _compute_filtered_results,
    _build_results_keyboard,
    RESULTS_SESSION_TTL_SECONDS,
    EpisodeCandidate,
    _select_consistent_episode_set,
    _handle_collection_confirm,
)


def _extract_filter_row_texts(keyboard):
    for row in keyboard:
        if row and getattr(row[0], "callback_data", "").startswith(
            "search_results_filter_resolution_"
        ):
            return [btn.text for btn in row]
    return []


def _extract_codec_row_texts(keyboard):
    for row in keyboard:
        if row and getattr(row[0], "callback_data", "").startswith(
            "search_results_filter_codec_"
        ):
            return [btn.text for btn in row]
    return []


@pytest.mark.asyncio
async def test_search_movie_happy_path(
    mocker, context, make_callback_query, make_message
):
    context.bot_data["SEARCH_CONFIG"] = {"websites": []}
    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_movie_years_from_wikipedia",
        new=AsyncMock(return_value=([2010], "Inception")),
    )

    async def fake_present(message, ctx, results, query_str, *, session=None, **kwargs):
        if session:
            session.advance(SearchStep.CONFIRMATION)
            session.save(context.user_data)

    present_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow._present_search_results",
        new=AsyncMock(side_effect=fake_present),
    )
    orchestrate_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.search_logic.orchestrate_searches",
        new=AsyncMock(
            side_effect=[
                [{"title": "Inception 1080p", "page_url": "a"}],
                [{"title": "Inception 4K", "page_url": "b"}],
            ]
        ),
    )

    # Step 1: press start movie button
    start_update = Update(
        update_id=1,
        callback_query=make_callback_query("search_start_movie", make_message()),
    )
    await handle_search_buttons(start_update, context)
    session = SearchSession.from_user_data(context.user_data)
    assert session.media_type == "movie"
    assert session.step == SearchStep.MOVIE_SCOPE

    scope_update = Update(
        update_id=2,
        callback_query=make_callback_query("search_movie_scope_single", make_message()),
    )
    await handle_search_buttons(scope_update, context)
    session = SearchSession.from_user_data(context.user_data)
    assert session.media_type == "movie"
    assert session.step == SearchStep.TITLE

    # Step 2: user provides title and triggers combined search
    await handle_search_workflow(
        Update(update_id=3, message=make_message("Inception")), context
    )
    assert orchestrate_mock.await_count == 2
    first_call = orchestrate_mock.await_args_list[0]
    second_call = orchestrate_mock.await_args_list[1]
    assert first_call.args[:3] == ("Inception", "movie", context)
    assert first_call.kwargs["year"] == "2010"
    assert first_call.kwargs["resolution"] == "1080p"
    assert second_call.kwargs["resolution"] == "2160p"
    present_mock.assert_awaited_once()
    presented_results = present_mock.await_args.args[2]
    assert [r["title"] for r in presented_results] == [
        "Inception 1080p",
        "Inception 4K",
    ]
    query_label = present_mock.await_args.args[3]
    assert query_label.endswith("[All]")


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
    search_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow._search_movie_results",
        new=AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.search_logic.orchestrate_searches",
        new=AsyncMock(return_value=[]),
    )

    start_update = Update(
        update_id=10,
        callback_query=make_callback_query("search_start_movie", make_message()),
    )
    await handle_search_buttons(start_update, context)
    scope_update = Update(
        update_id=11,
        callback_query=make_callback_query("search_movie_scope_single", make_message()),
    )
    await handle_search_buttons(scope_update, context)
    await handle_search_workflow(
        Update(update_id=12, message=make_message("Oblivion")), context
    )
    fetch_mock.assert_not_awaited()
    search_mock.assert_awaited_once()
    session = SearchSession.from_user_data(context.user_data)
    assert session.final_title == "Oblivion (2013)"


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

    start_update = Update(
        update_id=20,
        callback_query=make_callback_query("search_start_movie", make_message()),
    )
    await handle_search_buttons(start_update, context)
    scope_update = Update(
        update_id=21,
        callback_query=make_callback_query("search_movie_scope_single", make_message()),
    )
    await handle_search_buttons(scope_update, context)
    await handle_search_workflow(
        Update(update_id=22, message=make_message("Interstellar")), context
    )
    fetch_mock.assert_not_awaited()
    process_mock.assert_awaited_once()
    notice = process_mock.await_args.kwargs.get("notice")
    assert notice and "Search configuration unavailable" in notice


@pytest.mark.asyncio
async def test_movie_scope_collection_sets_flag(
    mocker, context, make_callback_query, make_message
):
    context.bot_data["SEARCH_CONFIG"] = {"websites": []}
    start_message = make_message()
    start_callback = make_callback_query("search_start_movie", start_message)
    await handle_search_buttons(
        Update(update_id=10, callback_query=start_callback), context
    )

    scope_message = make_message()
    scope_callback = make_callback_query("search_movie_scope_collection", scope_message)
    await handle_search_buttons(
        Update(update_id=11, callback_query=scope_callback), context
    )

    session = SearchSession.from_user_data(context.user_data)
    assert session.collection_mode is True
    assert session.step == SearchStep.TITLE


@pytest.mark.asyncio
async def test_collection_lookup_handles_missing_franchise(
    mocker, context, make_callback_query, make_message
):
    context.bot_data["SEARCH_CONFIG"] = {"websites": []}
    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_movie_franchise_details",
        new=AsyncMock(return_value=None),
    )
    send_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_send_message",
        new=AsyncMock(return_value=make_message(message_id=99)),
    )
    edit_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message",
        new=AsyncMock(),
    )

    await handle_search_buttons(
        Update(
            update_id=20,
            callback_query=make_callback_query("search_start_movie", make_message()),
        ),
        context,
    )
    await handle_search_buttons(
        Update(
            update_id=21,
            callback_query=make_callback_query(
                "search_movie_scope_collection", make_message()
            ),
        ),
        context,
    )
    await handle_search_workflow(
        Update(update_id=22, message=make_message("Matrix")), context
    )

    send_mock.assert_awaited()
    assert "No franchise" in edit_mock.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_collection_lookup_ignores_unreleased_titles(
    mocker, context, make_callback_query, make_message
):
    context.bot_data["SEARCH_CONFIG"] = {"websites": []}
    status_message = make_message(message_id=120)
    mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_send_message",
        new=AsyncMock(return_value=status_message),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_movie_franchise_details",
        new=AsyncMock(
            return_value=(
                "Saga",
                [
                    {
                        "title": "Already Out",
                        "year": 2020,
                        "release_date": "2020-05-04",
                    },
                    {
                        "title": "Coming Soon",
                        "year": 2035,
                        "release_date": "2035-01-01",
                    },
                ],
            )
        ),
    )

    await handle_search_buttons(
        Update(
            update_id=30,
            callback_query=make_callback_query("search_start_movie", make_message()),
        ),
        context,
    )
    await handle_search_buttons(
        Update(
            update_id=31,
            callback_query=make_callback_query(
                "search_movie_scope_collection", make_message()
            ),
        ),
        context,
    )
    await handle_search_workflow(
        Update(update_id=32, message=make_message("Saga Entry")), context
    )

    session = SearchSession.from_user_data(context.user_data)
    assert session.collection_name == "Saga"
    assert len(session.collection_movies) == 1
    assert session.collection_movies[0]["title"] == "Already Out"
    assert session.collection_movies[0]["release_date"] == "2020-05-04"


@pytest.mark.asyncio
async def test_collection_lookup_rejects_all_unreleased_titles(
    mocker, context, make_callback_query, make_message
):
    context.bot_data["SEARCH_CONFIG"] = {"websites": []}
    status_message = make_message(message_id=150)
    edit_mock = AsyncMock()
    mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_send_message",
        new=AsyncMock(return_value=status_message),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message",
        new=edit_mock,
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_movie_franchise_details",
        new=AsyncMock(
            return_value=(
                "Future Saga",
                [
                    {"title": "Future One", "year": 2030, "release_date": "2030-01-01"},
                    {"title": "Future Two", "year": 2031, "release_date": None},
                ],
            )
        ),
    )

    await handle_search_buttons(
        Update(
            update_id=40,
            callback_query=make_callback_query("search_start_movie", make_message()),
        ),
        context,
    )
    await handle_search_buttons(
        Update(
            update_id=41,
            callback_query=make_callback_query(
                "search_movie_scope_collection", make_message()
            ),
        ),
        context,
    )
    await handle_search_workflow(
        Update(update_id=42, message=make_message("Future Saga")), context
    )

    assert "no released titles" in edit_mock.await_args.kwargs["text"].lower()
    session = SearchSession.from_user_data(context.user_data)
    assert not session.collection_movies


@pytest.mark.asyncio
async def test_collection_confirm_sets_pending_payload(
    mocker, context, make_callback_query, make_message
):
    session = SearchSession()
    session.media_type = "movie"
    session.collection_mode = True
    session.collection_movies = [
        {
            "title": "Movie One",
            "year": 2001,
            "identifier": "movie-1",
            "owned": False,
            "queued": False,
        },
        {
            "title": "Movie Two",
            "year": 2002,
            "identifier": "movie-2",
            "owned": False,
            "queued": False,
        },
    ]
    session.collection_resolution = "1080p"
    session.collection_codec = "x264"
    session.prompt_message_id = 55
    session.save(context.user_data)

    pending_payload = {
        "items": [
            {
                "link": "magnet:?xt=1",
                "parsed_info": {"title": "Movie One"},
                "movie": {"title": "Movie One", "year": 2001},
            },
            {
                "link": "magnet:?xt=2",
                "parsed_info": {"title": "Movie Two"},
                "movie": {"title": "Movie Two", "year": 2002},
            },
        ],
        "franchise": {
            "name": "Saga",
            "fs_name": "Saga",
            "movies": [{"title": "Movie One"}],
        },
    }

    mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    collect_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow._collect_collection_torrents",
        new=AsyncMock(return_value=(pending_payload, [])),
    )
    present_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow._present_collection_download_confirmation",
        new=AsyncMock(),
    )
    clear_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.clear_search_session"
    )

    callback = make_callback_query("search_collection_confirm", make_message())
    await _handle_collection_confirm(
        callback, context, SearchSession.from_user_data(context.user_data)
    )

    collect_mock.assert_awaited()
    present_mock.assert_awaited_once()
    clear_mock.assert_called_once()
    assert context.user_data["pending_collection_download"] is pending_payload


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
    present_mock = mocker.patch(
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

    # Episode step collects input and triggers search automatically
    await handle_search_workflow(
        Update(update_id=5, message=make_message("2")), context
    )
    orchestrate_mock.assert_awaited_once_with(
        "My Show S01E02", "tv", context, base_query_for_filter="My Show"
    )
    present_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_tv_title_fast_path_skips_prompts(
    mocker, context, make_callback_query, make_message
):
    send_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_send_message",
        new=AsyncMock(return_value=make_message()),
    )

    async def fake_present(message, ctx, results, query_str, *, session=None, **kwargs):
        if session:
            session.advance(SearchStep.CONFIRMATION)
            session.save(context.user_data)

    present_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow._present_search_results",
        new=AsyncMock(side_effect=fake_present),
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
    present_mock.assert_awaited()
    session = SearchSession.from_user_data(context.user_data)
    assert session.step == SearchStep.CONFIRMATION
    assert session.season == 2
    assert session.episode == 5
    assert session.tv_scope == "single"
    assert session.final_title == "The Bear S02E05"
    assert session.allow_detail_change is True

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


def test_movie_filter_row_excludes_720p():
    session = SearchSession(media_type="movie")
    session.results = [{"title": "Movie 1080p", "page_url": "x"}]
    session.results_query = "Movie"
    session.results_generated_at = time.time()
    keyboard = _build_results_keyboard(session, session.results, 1)
    labels = [text.replace("🟢", "") for text in _extract_filter_row_texts(keyboard)]
    assert labels == ["All", "1080p", "2160p"]


def test_tv_filter_row_excludes_2160p():
    session = SearchSession(media_type="tv")
    session.results = [{"title": "Show 720p", "page_url": "y"}]
    session.results_query = "Show"
    session.results_generated_at = time.time()
    session.results_resolution_filter = "720p"
    keyboard = _build_results_keyboard(session, session.results, 1)
    labels = [text.replace("🟢", "") for text in _extract_filter_row_texts(keyboard)]
    assert labels == ["All", "720p", "1080p"]


@pytest.mark.asyncio
async def test_codec_filter_buttons_toggle_state(
    mocker, context, make_callback_query, make_message
):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    session = SearchSession(media_type="movie")
    session.results = [
        {
            "title": "x264 option",
            "page_url": "x",
            "codec": "x264",
            "seeders": 10,
            "size_gb": 5,
            "source": "site",
        },
        {
            "title": "x265 option",
            "page_url": "y",
            "codec": "x265",
            "seeders": 15,
            "size_gb": 6,
            "source": "site",
        },
    ]
    session.results_query = "Example"
    session.results_generated_at = time.time()
    session.save(context.user_data)

    update = Update(
        update_id=1,
        callback_query=make_callback_query(
            "search_results_filter_codec_x265", make_message(message_id=10)
        ),
    )
    await handle_search_buttons(update, context)

    persisted = SearchSession.from_user_data(context.user_data)
    assert persisted.results_codec_filter == "x265"
    filtered = _compute_filtered_results(persisted)
    keyboard = _build_results_keyboard(persisted, filtered, 1)
    codec_labels = _extract_codec_row_texts(keyboard)
    assert "🟢x265" in codec_labels


@pytest.mark.asyncio
async def test_results_pagination_callback_updates_page(
    mocker, context, make_callback_query, make_message
):
    safe_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    session = SearchSession(media_type="movie")
    session.results = [
        {
            "title": f"Result {i}",
            "page_url": f"https://example.com/{i}",
            "codec": "H264",
            "seeders": i + 1,
            "size_gb": 5.0,
            "source": "site",
        }
        for i in range(7)
    ]
    session.results_query = "Example"
    session.results_generated_at = time.time()
    session.save(context.user_data)

    update = Update(
        update_id=1,
        callback_query=make_callback_query(
            "search_results_page_1", make_message(message_id=10)
        ),
    )
    await handle_search_buttons(update, context)

    persisted = SearchSession.from_user_data(context.user_data)
    assert persisted.results_page == 1
    assert safe_mock.await_count == 1


@pytest.mark.asyncio
async def test_results_filter_callback_updates_state(
    mocker, context, make_callback_query, make_message
):
    safe_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    session = SearchSession(media_type="movie")
    session.results = [
        {
            "title": "Show 720p",
            "page_url": "a",
            "codec": "X",
            "seeders": 5,
            "size_gb": 2,
        },
        {
            "title": "Show 1080p",
            "page_url": "b",
            "codec": "X",
            "seeders": 10,
            "size_gb": 4,
        },
    ]
    session.results_query = "Show"
    session.results_generated_at = time.time()
    session.save(context.user_data)

    update = Update(
        update_id=2,
        callback_query=make_callback_query(
            "search_results_filter_resolution_720p", make_message(message_id=11)
        ),
    )
    await handle_search_buttons(update, context)

    persisted = SearchSession.from_user_data(context.user_data)
    assert persisted.results_resolution_filter == "all"
    assert persisted.results_page == 0
    assert safe_mock.await_count == 0


def test_size_filter_allows_large_when_filtering_for_4k():
    session = SearchSession(media_type="movie")
    session.results = [
        {
            "title": "Compact",
            "page_url": "a",
            "codec": "X",
            "seeders": 50,
            "size_gb": 8,
            "source": "site",
        },
        {
            "title": "Huge 4K",
            "page_url": "b",
            "codec": "X",
            "seeders": 20,
            "size_gb": 18,
            "source": "site",
        },
    ]
    session.results_query = "Movie"
    session.results_max_size_gb = 10

    session.results_resolution_filter = "all"
    filtered_default = _compute_filtered_results(session)
    assert [r["title"] for r in filtered_default] == ["Compact"]

    session.results_resolution_filter = "2160p"
    filtered_four_k = _compute_filtered_results(session)
    assert [r["title"] for r in filtered_four_k] == ["Huge 4K"]


def test_compute_filtered_results_filters_by_codec():
    session = SearchSession(media_type="movie")
    session.results = [
        {
            "title": "Option A",
            "page_url": "a",
            "codec": "x264",
            "seeders": 20,
            "size_gb": 5,
            "source": "site",
        },
        {
            "title": "Option B",
            "page_url": "b",
            "codec": "x265",
            "seeders": 25,
            "size_gb": 6,
            "source": "site",
        },
        {
            "title": "No Codec",
            "page_url": "c",
            "seeders": 10,
            "size_gb": 4,
            "source": "site",
        },
    ]
    session.results_codec_filter = "x265"

    filtered = _compute_filtered_results(session)
    assert [r["title"] for r in filtered] == ["Option B"]


@pytest.mark.asyncio
async def test_results_callbacks_respect_expiration(
    mocker, context, make_callback_query, make_message
):
    safe_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    session = SearchSession(media_type="movie")
    session.results = [
        {
            "title": "Expired",
            "page_url": "a",
            "codec": "X",
            "seeders": 1,
            "size_gb": 5,
            "source": "site",
        }
    ]
    session.results_query = "Expired"
    session.results_generated_at = time.time() - (RESULTS_SESSION_TTL_SECONDS + 5)
    session.save(context.user_data)

    update = Update(
        update_id=3,
        callback_query=make_callback_query(
            "search_results_page_0", make_message(message_id=12)
        ),
    )
    await handle_search_buttons(update, context)

    assert safe_mock.await_count == 1
    text = safe_mock.await_args.kwargs["text"]
    assert "expired" in text.lower()


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
        "telegram_bot.workflows.search_workflow._perform_tv_season_search",
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
    perform_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow._perform_tv_season_search",
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
    perform_mock.assert_not_awaited()
    # Confirm we informed the user
    assert "already exist" in (edit_mock.await_args.kwargs.get("text") or "")


def _make_candidate(
    episode: int,
    uploader: str,
    *,
    size_gb: float,
    resolution: str = "1080p",
    source: str = "1337x",
    seeders: int = 150,
) -> EpisodeCandidate:
    return EpisodeCandidate(
        episode=episode,
        link=f"magnet:?xt=urn:btih:{episode}-{uploader}",
        title=f"Show S01E{episode:02d} {resolution} {uploader}",
        source=source,
        uploader=uploader,
        size_gb=size_gb,
        seeders=seeders,
        resolution=resolution,
        score=seeders,
    )


def test_consistent_episode_selection_prefers_uniform_release():
    candidates: dict[int, list[EpisodeCandidate]] = {}
    for ep in range(1, 4):
        candidates[ep] = [
            _make_candidate(ep, "SceneGroup", size_gb=1.0 + ep * 0.02),
            _make_candidate(ep, "Scatter", size_gb=1.9 + ep * 0.3, seeders=500),
        ]

    selection, summary = _select_consistent_episode_set(candidates)
    assert [cand.uploader for cand in selection] == ["SceneGroup"] * 3
    assert summary is not None
    assert summary.release_uploader == "SceneGroup"
    assert summary.fallback_episodes == []


def test_consistent_episode_selection_handles_4k_target():
    candidates: dict[int, list[EpisodeCandidate]] = {}
    for ep in range(1, 3):
        candidates[ep] = [
            _make_candidate(
                ep,
                "UHDVision",
                size_gb=4.4 + ep * 0.1,
                resolution="2160p",
                source="EZTV",
            ),
            _make_candidate(
                ep,
                "HDGroup",
                size_gb=1.0 + ep * 0.5,
                resolution="1080p",
                source="EZTV",
                seeders=400,
            ),
        ]

    selection, summary = _select_consistent_episode_set(candidates)
    assert all(c.resolution == "2160p" for c in selection)
    assert summary is not None
    assert summary.resolution == "2160p"
