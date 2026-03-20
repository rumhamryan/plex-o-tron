import time
from datetime import date
import pytest
from unittest.mock import AsyncMock

from telegram import Update

from telegram_bot.workflows.search_session import SearchSession, SearchStep
from telegram_bot.workflows.search_workflow import handle_search_buttons, handle_search_workflow
from telegram_bot.workflows.search_workflow.helpers import (
    _ensure_identifier,
    _format_collection_movie_label,
)
from telegram_bot.workflows.search_workflow.movie_collection_flow import (
    _classify_collection_release,
    _handle_collection_accept,
    _ensure_existing_movie_in_collection,
    _handle_collection_confirm,
    _prompt_collection_confirmation,
    _resolve_collection_search_template,
    _resolve_current_year_release_date,
    _resolve_collection_release,
)
from telegram_bot.workflows.search_workflow.results import (
    RESULTS_SESSION_TTL_SECONDS,
    _build_results_keyboard,
    _compute_filtered_results,
)
from telegram_bot.workflows.search_workflow.state import _clear_search_context
from telegram_bot.workflows.search_workflow.tv_flow import (
    EpisodeCandidate,
    _present_season_download_confirmation,
    _prompt_tv_season_preferences,
    _select_consistent_episode_set,
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
        if row and getattr(row[0], "callback_data", "").startswith("search_results_filter_codec_"):
            return [btn.text for btn in row]
    return []


@pytest.mark.asyncio
async def test_search_movie_happy_path(mocker, context, make_callback_query, make_message):
    context.bot_data["SEARCH_CONFIG"] = {"websites": []}
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_flow.scraping_service.fetch_movie_years_from_wikipedia",
        new=AsyncMock(return_value=([2010], "Inception")),
    )

    async def fake_present(message, ctx, results, query_str, *, session=None, **kwargs):
        if session:
            session.advance(SearchStep.CONFIRMATION)
            session.save(context.user_data)

    present_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_flow._present_search_results",
        new=AsyncMock(side_effect=fake_present),
    )
    orchestrate_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_flow.search_logic.orchestrate_searches",
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
    await handle_search_workflow(Update(update_id=3, message=make_message("Inception")), context)
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
async def test_movie_query_with_resolution_and_codec_skips_prompt(
    mocker, context, make_callback_query, make_message
):
    orchestrate_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_flow.search_logic.orchestrate_searches",
        new=AsyncMock(
            return_value=[
                {
                    "title": "Inception 2010 1080p x265",
                    "page_url": "movie-1080p-x265",
                    "codec": "x265",
                    "seeders": 50,
                    "size_gib": 7.5,
                    "source": "YTS",
                }
            ]
        ),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_flow.safe_send_message",
        new=AsyncMock(return_value=make_message(message_id=30)),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.results.safe_edit_message",
        new=AsyncMock(),
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
            callback_query=make_callback_query("search_movie_scope_single", make_message()),
        ),
        context,
    )
    await handle_search_workflow(
        Update(update_id=42, message=make_message("Inception 2010 1080p x265")),
        context,
    )

    orchestrate_mock.assert_awaited_once()
    assert orchestrate_mock.await_args.args[:3] == ("Inception", "movie", context)
    assert orchestrate_mock.await_args.kwargs["year"] == "2010"
    assert orchestrate_mock.await_args.kwargs["resolution"] == "1080p"

    session = SearchSession.from_user_data(context.user_data)
    assert session.step == SearchStep.CONFIRMATION
    assert session.final_title == "Inception (2010)"
    assert session.results_resolution_filter == "1080p"
    assert session.results_codec_filter == "x265"


@pytest.mark.asyncio
async def test_movie_search_uses_cached_year_without_config(
    mocker, context, make_callback_query, make_message
):
    context.bot_data.clear()
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_flow.scraping_service.get_cached_movie_years",
        return_value=([2013], "Oblivion"),
    )
    fetch_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_flow.scraping_service.fetch_movie_years_from_wikipedia",
        new=AsyncMock(),
    )
    search_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_flow._search_movie_results",
        new=AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_flow.search_logic.orchestrate_searches",
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
    await handle_search_workflow(Update(update_id=12, message=make_message("Oblivion")), context)
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
        "telegram_bot.workflows.search_workflow.movie_flow.scraping_service.get_cached_movie_years",
        return_value=None,
    )
    fetch_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_flow.scraping_service.fetch_movie_years_from_wikipedia",
        new=AsyncMock(),
    )
    process_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_flow._process_preliminary_results",
        new=AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_flow.search_logic.orchestrate_searches",
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
async def test_movie_scope_collection_sets_flag(mocker, context, make_callback_query, make_message):
    context.bot_data["SEARCH_CONFIG"] = {"websites": []}
    start_message = make_message()
    start_callback = make_callback_query("search_start_movie", start_message)
    await handle_search_buttons(Update(update_id=10, callback_query=start_callback), context)

    scope_message = make_message()
    scope_callback = make_callback_query("search_movie_scope_collection", scope_message)
    await handle_search_buttons(Update(update_id=11, callback_query=scope_callback), context)

    session = SearchSession.from_user_data(context.user_data)
    assert session.collection_mode is True
    assert session.step == SearchStep.TITLE


@pytest.mark.asyncio
async def test_collection_lookup_handles_missing_franchise(
    mocker, context, make_callback_query, make_message
):
    context.bot_data["SEARCH_CONFIG"] = {"websites": []}

    async def fake_fetch_movie_franchise_details(_title, *, progress_callback=None):
        if progress_callback is not None:
            await progress_callback("review", None)
            await progress_callback("compare", None)
            await progress_callback("inspect", None)
            await progress_callback("score", "Matrix film series")
        return None

    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.scraping_service.fetch_movie_franchise_details",
        new=AsyncMock(side_effect=fake_fetch_movie_franchise_details),
    )
    send_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_send_message",
        new=AsyncMock(return_value=make_message(message_id=99)),
    )
    edit_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_edit_message",
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
            callback_query=make_callback_query("search_movie_scope_collection", make_message()),
        ),
        context,
    )
    await handle_search_workflow(Update(update_id=22, message=make_message("Matrix")), context)

    send_mock.assert_awaited()
    assert send_mock.await_args.args[2] == "🧩 Wikipedia lookup: searching for *Matrix* franchise…"
    assert edit_mock.await_args_list[0].kwargs["text"] == (
        "🧩 Wikipedia lookup: reviewing Wikipedia franchise candidates…"
    )
    assert edit_mock.await_args_list[1].kwargs["text"] == (
        "🧩 Wikipedia lookup: comparing likely franchise pages…"
    )
    assert edit_mock.await_args_list[2].kwargs["text"] == (
        "🧩 Wikipedia lookup: opening likely franchise pages…"
    )
    assert edit_mock.await_args_list[3].kwargs["text"] == (
        "🧩 Wikipedia lookup: extracting films from *Matrix film series* and scoring the franchise match…"
    )
    assert "No franchise" in edit_mock.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_collection_lookup_ignores_unreleased_titles(
    mocker, context, make_callback_query, make_message
):
    context.bot_data["SEARCH_CONFIG"] = {"websites": []}
    status_message = make_message(message_id=120)
    send_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_send_message",
        new=AsyncMock(return_value=status_message),
    )
    edit_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_edit_message",
        new=AsyncMock(),
    )

    async def fake_fetch_movie_franchise_details(_title, *, progress_callback=None):
        if progress_callback is not None:
            await progress_callback("review", None)
            await progress_callback("compare", None)
            await progress_callback("inspect", None)
            await progress_callback("score", "Saga film series")
        return (
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

    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.scraping_service.fetch_movie_franchise_details",
        new=AsyncMock(side_effect=fake_fetch_movie_franchise_details),
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
            callback_query=make_callback_query("search_movie_scope_collection", make_message()),
        ),
        context,
    )
    await handle_search_workflow(Update(update_id=32, message=make_message("Saga Entry")), context)

    assert (
        send_mock.await_args.args[2] == "🧩 Wikipedia lookup: searching for *Saga Entry* franchise…"
    )
    assert [call.kwargs.get("text") for call in edit_mock.await_args_list[:5]] == [
        "🧩 Wikipedia lookup: reviewing Wikipedia franchise candidates…",
        "🧩 Wikipedia lookup: comparing likely franchise pages…",
        "🧩 Wikipedia lookup: opening likely franchise pages…",
        "🧩 Wikipedia lookup: extracting films from *Saga film series* and scoring the franchise match…",
        "🧩 Wikipedia lookup: validating collection entries and release dates…",
    ]
    session = SearchSession.from_user_data(context.user_data)
    assert session.collection_name == "Saga"
    assert len(session.collection_movies) == 1
    assert session.collection_movies[0]["title"] == "Already Out"
    assert session.collection_movies[0]["release_date"] == "2020-05-04"


@pytest.mark.asyncio
async def test_collection_lookup_strips_duplicate_trailing_year_from_titles(
    mocker, context, make_callback_query, make_message
):
    context.bot_data["SEARCH_CONFIG"] = {"websites": []}
    status_message = make_message(message_id=120)
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_send_message",
        new=AsyncMock(return_value=status_message),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_edit_message",
        new=AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.scraping_service.fetch_movie_franchise_details",
        new=AsyncMock(
            return_value=(
                "Star Wars",
                [
                    {
                        "title": "Star Wars (1977)",
                        "year": 1977,
                        "release_date": "1977-05-25",
                    },
                    {
                        "title": "The Empire Strikes Back (1980)",
                        "year": 1980,
                        "release_date": "1980-05-21",
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
            callback_query=make_callback_query("search_movie_scope_collection", make_message()),
        ),
        context,
    )
    await handle_search_workflow(Update(update_id=32, message=make_message("Star Wars")), context)

    session = SearchSession.from_user_data(context.user_data)
    assert [movie["title"] for movie in session.collection_movies] == [
        "Star Wars",
        "The Empire Strikes Back",
    ]
    assert [movie["year"] for movie in session.collection_movies] == [1977, 1980]


@pytest.mark.asyncio
async def test_collection_lookup_normalizes_resolved_franchise_name(
    mocker, context, make_callback_query, make_message
):
    context.bot_data["SEARCH_CONFIG"] = {"websites": []}
    status_message = make_message(message_id=122)
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_send_message",
        new=AsyncMock(return_value=status_message),
    )
    edit_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_edit_message",
        new=AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.scraping_service.fetch_movie_franchise_details",
        new=AsyncMock(
            return_value=(
                "Harry Potter (film series)",
                [
                    {
                        "title": "Harry Potter and the Philosopher's Stone",
                        "year": 2001,
                        "release_date": "2001-11-16",
                    },
                    {
                        "title": "Harry Potter and the Chamber of Secrets",
                        "year": 2002,
                        "release_date": "2002-11-15",
                    },
                ],
            )
        ),
    )

    await handle_search_buttons(
        Update(
            update_id=36,
            callback_query=make_callback_query("search_start_movie", make_message()),
        ),
        context,
    )
    await handle_search_buttons(
        Update(
            update_id=37,
            callback_query=make_callback_query("search_movie_scope_collection", make_message()),
        ),
        context,
    )
    await handle_search_workflow(
        Update(update_id=38, message=make_message("Harry Potter")),
        context,
    )

    session = SearchSession.from_user_data(context.user_data)
    assert session.collection_name == "Harry Potter"
    assert session.collection_fs_name == "Harry Potter"
    assert "Harry Potter (film series)" not in edit_mock.await_args.kwargs["text"]
    assert "*Harry Potter*" in edit_mock.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_collection_lookup_accepts_past_year_only_titles(
    mocker, context, make_callback_query, make_message
):
    class _FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 3, 12)

    context.bot_data["SEARCH_CONFIG"] = {"websites": []}
    status_message = make_message(message_id=121)
    mocker.patch("telegram_bot.workflows.search_workflow.movie_collection_flow.date", _FixedDate)
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_send_message",
        new=AsyncMock(return_value=status_message),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_edit_message",
        new=AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.scraping_service.fetch_movie_franchise_details",
        new=AsyncMock(
            return_value=(
                "The Equalizer",
                [
                    {"title": "The Equalizer", "year": 2014, "release_date": None},
                    {"title": "The Equalizer 2", "year": 2018, "release_date": None},
                    {"title": "The Equalizer 3", "year": 2023, "release_date": None},
                ],
            )
        ),
    )

    await handle_search_buttons(
        Update(
            update_id=33,
            callback_query=make_callback_query("search_start_movie", make_message()),
        ),
        context,
    )
    await handle_search_buttons(
        Update(
            update_id=34,
            callback_query=make_callback_query("search_movie_scope_collection", make_message()),
        ),
        context,
    )
    await handle_search_workflow(
        Update(update_id=35, message=make_message("The Equalizer")), context
    )

    session = SearchSession.from_user_data(context.user_data)
    assert [movie["title"] for movie in session.collection_movies] == [
        "The Equalizer",
        "The Equalizer 2",
        "The Equalizer 3",
    ]
    assert [movie["release_date"] for movie in session.collection_movies] == [None, None, None]


@pytest.mark.asyncio
async def test_collection_lookup_rejects_future_year_only_titles(
    mocker, context, make_callback_query, make_message
):
    class _FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 3, 12)

    context.bot_data["SEARCH_CONFIG"] = {"websites": []}
    status_message = make_message(message_id=122)
    edit_mock = AsyncMock()
    logger_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.logger"
    )
    mocker.patch("telegram_bot.workflows.search_workflow.movie_collection_flow.date", _FixedDate)
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_send_message",
        new=AsyncMock(return_value=status_message),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_edit_message",
        new=edit_mock,
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.scraping_service.fetch_movie_franchise_details",
        new=AsyncMock(
            return_value=(
                "Future Saga",
                [
                    {"title": "Future One", "year": 2030, "release_date": None},
                    {"title": "Future Two", "year": 2031, "release_date": None},
                ],
            )
        ),
    )

    await handle_search_buttons(
        Update(
            update_id=36,
            callback_query=make_callback_query("search_start_movie", make_message()),
        ),
        context,
    )
    await handle_search_buttons(
        Update(
            update_id=37,
            callback_query=make_callback_query("search_movie_scope_collection", make_message()),
        ),
        context,
    )
    await handle_search_workflow(Update(update_id=38, message=make_message("Future Saga")), context)

    assert "no released titles" in edit_mock.await_args.kwargs["text"].lower()
    logger_mock.info.assert_any_call(
        "[COLLECTION] Franchise '%s' matched for '%s' has no released titles yet (%d unreleased entries).",
        "Future Saga",
        "Future Saga",
        2,
    )
    session = SearchSession.from_user_data(context.user_data)
    assert not session.collection_movies


@pytest.mark.asyncio
async def test_collection_lookup_rejects_entries_missing_all_release_metadata(
    mocker, context, make_callback_query, make_message
):
    class _FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 3, 12)

    context.bot_data["SEARCH_CONFIG"] = {"websites": []}
    status_message = make_message(message_id=123)
    edit_mock = AsyncMock()
    logger_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.logger"
    )
    mocker.patch("telegram_bot.workflows.search_workflow.movie_collection_flow.date", _FixedDate)
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_send_message",
        new=AsyncMock(return_value=status_message),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_edit_message",
        new=edit_mock,
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.scraping_service.fetch_movie_franchise_details",
        new=AsyncMock(
            return_value=(
                "Metadata Void",
                [
                    {"title": "Unknown One", "year": None, "release_date": None},
                    {"title": "Unknown Two"},
                ],
            )
        ),
    )

    await handle_search_buttons(
        Update(
            update_id=39,
            callback_query=make_callback_query("search_start_movie", make_message()),
        ),
        context,
    )
    await handle_search_buttons(
        Update(
            update_id=40,
            callback_query=make_callback_query("search_movie_scope_collection", make_message()),
        ),
        context,
    )
    await handle_search_workflow(
        Update(update_id=41, message=make_message("Metadata Void")), context
    )

    assert "contains no movies i can queue" in edit_mock.await_args.kwargs["text"].lower()
    logger_mock.info.assert_any_call(
        "[COLLECTION] Franchise '%s' matched for '%s' has no queueable titles (%d unreleased, %d missing release metadata).",
        "Metadata Void",
        "Metadata Void",
        0,
        2,
    )
    session = SearchSession.from_user_data(context.user_data)
    assert not session.collection_movies


def test_classify_collection_release_accepts_past_iso_date():
    state, parsed_year, release_date = _classify_collection_release(
        {"title": "Released", "year": 2020, "release_date": "2020-05-04"},
        date(2026, 3, 12),
    )

    assert state == "released"
    assert parsed_year == 2020
    assert release_date == date(2020, 5, 4)


def test_classify_collection_release_rejects_future_iso_date():
    state, parsed_year, release_date = _classify_collection_release(
        {"title": "Upcoming", "year": 2035, "release_date": "2035-01-01"},
        date(2026, 3, 12),
    )

    assert state == "unreleased"
    assert parsed_year == 2035
    assert release_date == date(2035, 1, 1)


def test_classify_collection_release_prefers_release_date_over_year():
    state, parsed_year, release_date = _classify_collection_release(
        {"title": "Conflicting", "year": 2020, "release_date": "2035-01-01"},
        date(2026, 3, 12),
    )

    assert state == "unreleased"
    assert parsed_year == 2020
    assert release_date == date(2035, 1, 1)


@pytest.mark.asyncio
async def test_resolve_collection_release_accepts_current_year_movie_with_resolved_past_date(
    mocker,
):
    logger_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.logger"
    )
    resolve_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow._resolve_current_year_release_date",
        new=AsyncMock(return_value=date(2026, 1, 15)),
    )

    state, parsed_year, release_date = await _resolve_collection_release(
        {"title": "Now Playing", "year": 2026, "release_date": None},
        date(2026, 3, 12),
    )

    assert state == "released"
    assert parsed_year == 2026
    assert release_date == date(2026, 1, 15)
    resolve_mock.assert_awaited_once_with("Now Playing", 2026)
    logger_mock.info.assert_called_once_with(
        "[COLLECTION] Current-year release resolved for '%s' (%s): %s -> released",
        "Now Playing",
        2026,
        "2026-01-15",
    )


@pytest.mark.asyncio
async def test_resolve_current_year_release_date_uses_plain_page_title_when_available(mocker):
    fetch_years_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.scraping_service.fetch_movie_years_from_wikipedia",
        new=AsyncMock(return_value=([2026], None)),
    )
    page_mock = mocker.Mock()
    page_mock.title = "The Mandalorian and Grogu"
    wikipedia_page_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.wikipedia.page",
        return_value=page_mock,
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow._fetch_html_from_page",
        new=AsyncMock(
            return_value="""
            <table class="infobox vevent">
                <tr>
                    <th scope="row" class="infobox-label">
                        <div>Release date</div>
                    </th>
                    <td class="infobox-data">
                        <div class="plainlist film-date">
                            <ul>
                                <li>May 22, 2026<span style="display: none;"> (<span class="bday dtstart published updated itvstart">2026-05-22</span>)</span></li>
                            </ul>
                        </div>
                    </td>
                </tr>
            </table>
            """
        ),
    )

    release_date = await _resolve_current_year_release_date(
        "The Mandalorian and Grogu (2026)",
        2026,
    )

    assert release_date == date(2026, 5, 22)
    fetch_years_mock.assert_awaited_once_with("The Mandalorian and Grogu")
    wikipedia_page_mock.assert_called_once_with(
        "The Mandalorian and Grogu",
        auto_suggest=False,
        redirect=True,
    )


@pytest.mark.asyncio
async def test_resolve_collection_release_rejects_current_year_movie_with_resolved_future_date(
    mocker,
):
    logger_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.logger"
    )
    resolve_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow._resolve_current_year_release_date",
        new=AsyncMock(return_value=date(2026, 12, 25)),
    )

    state, parsed_year, release_date = await _resolve_collection_release(
        {"title": "Coming Later", "year": 2026, "release_date": None},
        date(2026, 3, 12),
    )

    assert state == "unreleased"
    assert parsed_year == 2026
    assert release_date == date(2026, 12, 25)
    resolve_mock.assert_awaited_once_with("Coming Later", 2026)
    logger_mock.info.assert_called_once_with(
        "[COLLECTION] Current-year release resolved for '%s' (%s): %s -> unreleased",
        "Coming Later",
        2026,
        "2026-12-25",
    )


@pytest.mark.asyncio
async def test_resolve_collection_release_falls_back_when_current_year_date_not_found(mocker):
    resolve_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow._resolve_current_year_release_date",
        new=AsyncMock(return_value=None),
    )

    state, parsed_year, release_date = await _resolve_collection_release(
        {"title": "TBD", "year": 2026, "release_date": None},
        date(2026, 3, 12),
    )

    assert state == "released"
    assert parsed_year == 2026
    assert release_date is None
    resolve_mock.assert_awaited_once_with("TBD", 2026)


@pytest.mark.asyncio
async def test_resolve_collection_release_skips_extra_lookup_for_non_current_year_movies(mocker):
    resolve_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow._resolve_current_year_release_date",
        new=AsyncMock(),
    )

    older_state, older_year, older_release_date = await _resolve_collection_release(
        {"title": "Older Film", "year": 2025, "release_date": None},
        date(2026, 3, 12),
    )
    future_state, future_year, future_release_date = await _resolve_collection_release(
        {"title": "Future Film", "year": 2027, "release_date": None},
        date(2026, 3, 12),
    )

    assert older_state == "released"
    assert older_year == 2025
    assert older_release_date is None
    assert future_state == "unreleased"
    assert future_year == 2027
    assert future_release_date is None
    resolve_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_collection_lookup_handles_mixed_current_year_release_resolution(
    mocker, context, make_callback_query, make_message
):
    class _FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 3, 12)

    context.bot_data["SEARCH_CONFIG"] = {"websites": []}
    status_message = make_message(message_id=124)
    resolve_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow._resolve_current_year_release_date",
        new=AsyncMock(side_effect=[date(2026, 1, 20), date(2026, 11, 1), None]),
    )
    mocker.patch("telegram_bot.workflows.search_workflow.movie_collection_flow.date", _FixedDate)
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_send_message",
        new=AsyncMock(return_value=status_message),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_edit_message",
        new=AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.scraping_service.fetch_movie_franchise_details",
        new=AsyncMock(
            return_value=(
                "Mixed Saga",
                [
                    {"title": "Earlier This Year", "year": 2026, "release_date": None},
                    {"title": "Later This Year", "year": 2026, "release_date": None},
                    {"title": "Still TBD", "year": 2026, "release_date": None},
                    {"title": "Already Released", "year": 2024, "release_date": None},
                ],
            )
        ),
    )

    await handle_search_buttons(
        Update(
            update_id=43,
            callback_query=make_callback_query("search_start_movie", make_message()),
        ),
        context,
    )
    await handle_search_buttons(
        Update(
            update_id=44,
            callback_query=make_callback_query("search_movie_scope_collection", make_message()),
        ),
        context,
    )
    await handle_search_workflow(Update(update_id=45, message=make_message("Mixed Saga")), context)

    session = SearchSession.from_user_data(context.user_data)
    assert [movie["title"] for movie in session.collection_movies] == [
        "Earlier This Year",
        "Still TBD",
        "Already Released",
    ]
    assert [movie["release_date"] for movie in session.collection_movies] == [
        "2026-01-20",
        None,
        None,
    ]
    assert resolve_mock.await_count == 3
    assert [call.args for call in resolve_mock.await_args_list] == [
        ("Earlier This Year", 2026),
        ("Later This Year", 2026),
        ("Still TBD", 2026),
    ]


@pytest.mark.asyncio
async def test_collection_confirmation_logs_titles(mocker, context, make_message):
    safe_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_edit_message",
        new=AsyncMock(),
    )
    logger_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.logger"
    )
    session = SearchSession(media_type="movie")
    session.collection_name = "Saga"
    session.collection_movies = [
        {"title": "Movie One", "year": 2001},
        {"title": "Movie Two", "year": 2002},
    ]

    await _prompt_collection_confirmation(make_message(), context, session)

    logger_mock.info.assert_called_once_with(
        "[COLLECTION] Prompting use/cancel confirmation for '%s' with titles: %s",
        "Saga",
        "Movie One (2001), Movie Two (2002)",
    )
    safe_mock.assert_awaited_once()
    reply_markup = safe_mock.await_args.kwargs["reply_markup"]
    labels = [button.text for row in reply_markup.inline_keyboard for button in row]
    callbacks = [button.callback_data for row in reply_markup.inline_keyboard for button in row]
    assert labels == ["✅ Use Collection", "❌ Cancel"]
    assert callbacks == ["search_collection_accept", "cancel_operation"]


@pytest.mark.asyncio
async def test_collection_confirmation_lists_every_movie(mocker, context, make_message):
    safe_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_edit_message",
        new=AsyncMock(),
    )
    session = SearchSession(media_type="movie")
    session.collection_name = "Saga"
    session.collection_movies = [
        {"title": f"Movie {idx}", "year": 2000 + idx} for idx in range(1, 9)
    ]

    await _prompt_collection_confirmation(make_message(), context, session)

    text = safe_mock.await_args.kwargs["text"]
    for idx in range(1, 9):
        assert f"Movie {idx} \\({2000 + idx}\\)" in text
    assert "…and" not in text


def test_format_collection_movie_label_avoids_duplicate_year_suffix():
    assert (
        _format_collection_movie_label(
            {
                "title": "Star Wars (1977)",
                "year": 1977,
                "release_date": "1977-05-25",
            }
        )
        == "Star Wars (1977)"
    )


@pytest.mark.asyncio
async def test_collection_lookup_rejects_all_unreleased_titles(
    mocker, context, make_callback_query, make_message
):
    context.bot_data["SEARCH_CONFIG"] = {"websites": []}
    status_message = make_message(message_id=150)
    edit_mock = AsyncMock()
    logger_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.logger"
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_send_message",
        new=AsyncMock(return_value=status_message),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_edit_message",
        new=edit_mock,
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.scraping_service.fetch_movie_franchise_details",
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
            callback_query=make_callback_query("search_movie_scope_collection", make_message()),
        ),
        context,
    )
    await handle_search_workflow(Update(update_id=42, message=make_message("Future Saga")), context)

    assert "no released titles" in edit_mock.await_args.kwargs["text"].lower()
    logger_mock.info.assert_any_call(
        "[COLLECTION] Franchise '%s' matched for '%s' has no released titles yet (%d unreleased entries).",
        "Future Saga",
        "Future Saga",
        2,
    )
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
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_edit_message",
        new=AsyncMock(),
    )
    collect_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow._collect_collection_torrents",
        new=AsyncMock(return_value=(pending_payload, [])),
    )
    owned_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow._collect_owned_collection_summaries",
        new=AsyncMock(return_value=["summary block"]),
    )
    present_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow._present_collection_download_confirmation",
        new=AsyncMock(),
    )
    clear_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.clear_search_session"
    )

    callback = make_callback_query("search_collection_confirm", make_message())
    await _handle_collection_confirm(
        callback, context, SearchSession.from_user_data(context.user_data)
    )

    collect_mock.assert_awaited()
    owned_mock.assert_awaited_once()
    present_mock.assert_awaited_once()
    clear_mock.assert_called_once()
    assert context.user_data["pending_collection_download"] is pending_payload
    assert pending_payload["owned_summaries"] == ["summary block"]


def test_resolve_collection_search_template_uses_supported_movie_preferences(context):
    context.bot_data["SEARCH_CONFIG"] = {
        "preferences": {
            "movies": {
                "resolutions": {
                    "720p": 9,
                    "1080p": 3,
                    "2160p": 5,
                    "4k": 5,
                },
                "codecs": {
                    "x264": 1,
                    "h264": 1,
                    "x265": 2,
                    "hevc": 2,
                },
            }
        }
    }

    assert _resolve_collection_search_template(context) == ("2160p", "x265")


def test_resolve_collection_search_template_falls_back_when_movie_preferences_missing(context):
    context.bot_data["SEARCH_CONFIG"] = {"preferences": {"movies": {}}}

    assert _resolve_collection_search_template(context) == ("1080p", "x265")


def test_collection_identifier_stays_within_telegram_callback_limit():
    identifier = _ensure_identifier(
        {
            "title": "Harry Potter and the Deathly Hallows - Part 1",
            "year": 2010,
        },
        7,
    )

    assert identifier == "movie-7"
    assert len(f"search_collection_toggle_{identifier}") <= 64


@pytest.mark.asyncio
async def test_collection_accept_uses_configured_movie_template(
    mocker, context, make_callback_query, make_message
):
    context.bot_data["SEARCH_CONFIG"] = {
        "preferences": {
            "movies": {
                "resolutions": {
                    "1080p": 3,
                    "2160p": 5,
                    "4k": 5,
                },
                "codecs": {
                    "x264": 1,
                    "x265": 2,
                    "hevc": 2,
                },
            }
        }
    }
    session = SearchSession(media_type="movie", collection_mode=True)
    session.collection_movies = [
        {
            "title": "Movie One",
            "year": 2001,
            "identifier": "movie-1",
        }
    ]

    prepare_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow._prepare_collection_directory",
        new=AsyncMock(return_value=2),
    )
    render_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow._render_collection_movie_picker",
        new=AsyncMock(),
    )

    message = make_message(message_id=77)
    callback = make_callback_query("search_collection_accept", message)
    await _handle_collection_accept(callback, context, session)

    prepare_mock.assert_awaited_once_with(context, session)
    render_mock.assert_awaited_once_with(message, context, session)
    assert session.collection_owned_count == 2
    assert session.collection_resolution == "2160p"
    assert session.collection_codec == "x265"
    assert session.step == SearchStep.CONFIRMATION

    persisted = SearchSession.from_user_data(context.user_data)
    assert persisted.collection_resolution == "2160p"
    assert persisted.collection_codec == "x265"


@pytest.mark.asyncio
async def test_existing_movie_folder_flattened(tmp_path):
    movies_root = tmp_path / "movies"
    franchise_dir = movies_root / "Saga"
    nested_dir = franchise_dir / "Movie One (2020)"
    nested_dir.mkdir(parents=True)
    movie_path = nested_dir / "Movie One (2020).mkv"
    movie_path.write_bytes(b"data")

    result = await _ensure_existing_movie_in_collection(
        str(movies_root), str(franchise_dir), "Movie One (2020)"
    )

    assert result is True
    flattened = franchise_dir / "Movie One (2020).mkv"
    assert flattened.exists()
    assert not nested_dir.exists()


@pytest.mark.asyncio
async def test_existing_movie_file_moves_into_collection(tmp_path):
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    franchise_dir = movies_root / "Saga"
    franchise_dir.mkdir()
    movie_path = movies_root / "Movie Two (2021).mkv"
    movie_path.write_bytes(b"data")

    result = await _ensure_existing_movie_in_collection(
        str(movies_root), str(franchise_dir), "Movie Two (2021)"
    )

    assert result is True
    moved = franchise_dir / "Movie Two (2021).mkv"
    assert moved.exists()
    assert not movie_path.exists()


@pytest.mark.asyncio
async def test_search_tv_happy_path(mocker, context, make_callback_query, make_message):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.safe_edit_message",
        new=AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.scraping_service.fetch_total_seasons_from_wikipedia",
        new=AsyncMock(return_value=5),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.scraping_service.fetch_episode_titles_for_season",
        new=AsyncMock(return_value=({2: {"title": "Ep 2", "release_date": "2020-01-01"}}, None)),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.scraping_service.fetch_season_episode_count_from_wikipedia",
        new=AsyncMock(return_value=10),
    )
    mocker.patch("telegram_bot.workflows.search_workflow.tv_flow._send_prompt", new=AsyncMock())
    orchestrate_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.search_logic.orchestrate_searches",
        new=AsyncMock(return_value=[]),
    )
    present_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow._present_search_results",
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
    await handle_search_workflow(Update(update_id=2, message=make_message("My Show")), context)
    session = SearchSession.from_user_data(context.user_data)
    assert session.step == SearchStep.TV_SEASON
    assert session.title == "My Show"

    # Season step
    await handle_search_workflow(Update(update_id=3, message=make_message("1")), context)
    session = SearchSession.from_user_data(context.user_data)
    assert session.step == SearchStep.TV_SCOPE
    assert session.season == 1

    # Scope selection to single episode
    await handle_search_buttons(
        Update(
            update_id=4,
            callback_query=make_callback_query("search_tv_scope_single", make_message()),
        ),
        context,
    )
    session = SearchSession.from_user_data(context.user_data)
    assert session.step == SearchStep.TV_EPISODE

    # Episode step collects input and triggers search automatically
    await handle_search_workflow(Update(update_id=5, message=make_message("2")), context)
    orchestrate_mock.assert_awaited_once_with(
        "My Show S01E02", "tv", context, base_query_for_filter="My Show"
    )
    present_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_tv_title_fast_path_skips_prompts(mocker, context, make_callback_query, make_message):
    send_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.safe_send_message",
        new=AsyncMock(return_value=make_message()),
    )

    async def fake_present(message, ctx, results, query_str, *, session=None, **kwargs):
        if session:
            session.advance(SearchStep.CONFIRMATION)
            session.save(context.user_data)

    present_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow._present_search_results",
        new=AsyncMock(side_effect=fake_present),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.scraping_service.fetch_episode_titles_for_season",
        new=AsyncMock(return_value=({5: {"title": "Ep 5", "release_date": "2020-01-01"}}, None)),
    )
    seasons_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.scraping_service.fetch_total_seasons_from_wikipedia",
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
async def test_search_cancel_clears_context(mocker, context, make_callback_query, make_message):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.handlers.safe_edit_message",
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
        "telegram_bot.workflows.search_workflow.results.safe_edit_message",
        new=AsyncMock(),
    )
    session = SearchSession(media_type="movie")
    session.results = [
        {
            "title": "x264 option",
            "page_url": "x",
            "codec": "x264",
            "seeders": 10,
            "size_gib": 5,
            "source": "site",
        },
        {
            "title": "x265 option",
            "page_url": "y",
            "codec": "x265",
            "seeders": 15,
            "size_gib": 6,
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
        "telegram_bot.workflows.search_workflow.results.safe_edit_message",
        new=AsyncMock(),
    )
    session = SearchSession(media_type="movie")
    session.results = [
        {
            "title": f"Result {i}",
            "page_url": f"https://example.com/{i}",
            "codec": "H264",
            "seeders": i + 1,
            "size_gib": 5.0,
            "source": "site",
        }
        for i in range(7)
    ]
    session.results_query = "Example"
    session.results_generated_at = time.time()
    session.save(context.user_data)

    update = Update(
        update_id=1,
        callback_query=make_callback_query("search_results_page_1", make_message(message_id=10)),
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
        "telegram_bot.workflows.search_workflow.results.safe_edit_message",
        new=AsyncMock(),
    )
    session = SearchSession(media_type="movie")
    session.results = [
        {
            "title": "Show 720p",
            "page_url": "a",
            "codec": "X",
            "seeders": 5,
            "size_gib": 2,
        },
        {
            "title": "Show 1080p",
            "page_url": "b",
            "codec": "X",
            "seeders": 10,
            "size_gib": 4,
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
            "size_gib": 8,
            "source": "site",
        },
        {
            "title": "Huge 4K",
            "page_url": "b",
            "codec": "X",
            "seeders": 20,
            "size_gib": 18,
            "source": "site",
        },
    ]
    session.results_query = "Movie"
    session.results_max_size_gib = 10

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
            "size_gib": 5,
            "source": "site",
        },
        {
            "title": "Option B",
            "page_url": "b",
            "codec": "x265",
            "seeders": 25,
            "size_gib": 6,
            "source": "site",
        },
        {
            "title": "No Codec",
            "page_url": "c",
            "seeders": 10,
            "size_gib": 4,
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
        "telegram_bot.workflows.search_workflow.handlers.safe_edit_message",
        new=AsyncMock(),
    )
    session = SearchSession(media_type="movie")
    session.results = [
        {
            "title": "Expired",
            "page_url": "a",
            "codec": "X",
            "seeders": 1,
            "size_gib": 5,
            "source": "site",
        }
    ]
    session.results_query = "Expired"
    session.results_generated_at = time.time() - (RESULTS_SESSION_TTL_SECONDS + 5)
    session.save(context.user_data)

    update = Update(
        update_id=3,
        callback_query=make_callback_query("search_results_page_0", make_message(message_id=12)),
    )
    await handle_search_buttons(update, context)

    assert safe_mock.await_count == 1
    text = safe_mock.await_args.kwargs["text"]
    assert "expired" in text.lower()


@pytest.mark.asyncio
async def test_tv_season_reply_offers_scope_buttons(mocker, context, make_message):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.scraping_service.fetch_total_seasons_from_wikipedia",
        new=AsyncMock(return_value=5),
    )
    send_mock = mocker.patch.object(
        context.bot, "send_message", AsyncMock(return_value=make_message())
    )
    session = SearchSession(media_type="tv", step=SearchStep.TV_SEASON)
    session.set_title("My Show")
    session.save(context.user_data)
    await handle_search_workflow(Update(update_id=1, message=make_message("1")), context)
    send_mock.assert_awaited_once()
    kwargs = send_mock.await_args.kwargs
    keyboard = kwargs["reply_markup"].inline_keyboard
    assert keyboard[0][0].callback_data == "search_tv_scope_single"
    assert keyboard[1][0].callback_data == "search_tv_scope_season"


@pytest.mark.asyncio
async def test_handle_tv_scope_selection_single(mocker, context, make_callback_query, make_message):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.scraping_service.fetch_season_episode_count_from_wikipedia",
        new=AsyncMock(return_value=10),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.safe_edit_message", new=AsyncMock()
    )
    send_prompt = mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow._send_prompt", new=AsyncMock()
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
async def test_tv_query_with_resolution_and_codec_skips_prompt(
    mocker, context, make_callback_query, make_message
):
    orchestrate_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.search_logic.orchestrate_searches",
        new=AsyncMock(
            return_value=[
                {
                    "title": "Show S01E02 720p x265",
                    "page_url": "show-0102-720p-x265",
                    "codec": "x265",
                    "seeders": 35,
                    "size_gib": 1.1,
                    "source": "EZTV",
                }
            ]
        ),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow._validate_episode_released",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.safe_send_message",
        new=AsyncMock(return_value=make_message(message_id=60)),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.results.safe_edit_message",
        new=AsyncMock(),
    )

    await handle_search_buttons(
        Update(
            update_id=70,
            callback_query=make_callback_query("search_start_tv", make_message()),
        ),
        context,
    )
    await handle_search_workflow(
        Update(update_id=71, message=make_message("Show S01E02 720p x265")),
        context,
    )

    orchestrate_mock.assert_awaited_once()
    assert orchestrate_mock.await_args.args[:3] == ("Show S01E02 720p x265", "tv", context)
    assert orchestrate_mock.await_args.kwargs["base_query_for_filter"] == "Show"

    session = SearchSession.from_user_data(context.user_data)
    assert session.step == SearchStep.CONFIRMATION
    assert session.final_title == "Show S01E02"
    assert session.results_resolution_filter == "720p"
    assert session.results_codec_filter == "x265"
    assert session.allow_detail_change is True


@pytest.mark.asyncio
async def test_handle_tv_scope_selection_season(mocker, context, make_callback_query, make_message):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.scraping_service.fetch_episode_titles_for_season",
        new=AsyncMock(return_value=({}, None)),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.plex_service.get_existing_episodes_for_season",
        new=AsyncMock(return_value=set()),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.safe_edit_message", new=AsyncMock()
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.scraping_service.fetch_season_episode_count_from_wikipedia",
        new=AsyncMock(return_value=2),
    )
    prompt_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow._prompt_tv_season_preferences",
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

    prompt_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_tv_scope_selection_season_fallback(
    mocker, context, make_callback_query, make_message
):
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.scraping_service.fetch_episode_titles_for_season",
        new=AsyncMock(
            return_value=(
                {
                    1: {"title": "Episode 1", "release_date": None},
                    2: {"title": "Episode 2", "release_date": None},
                },
                None,
            )
        ),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.safe_edit_message", new=AsyncMock()
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.scraping_service.fetch_season_episode_count_from_wikipedia",
        new=AsyncMock(return_value=2),
    )
    prompt_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow._prompt_tv_season_preferences",
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

    prompt_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_present_season_download_confirmation(mocker, context, make_message):
    safe_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.safe_edit_message", new=AsyncMock()
    )
    session = SearchSession(media_type="tv", season=1)
    session.season_episode_count = 2
    session.save(context.user_data)
    torrents = [{"link": "a", "parsed_info": {}}, {"link": "b", "parsed_info": {}}]
    await _present_season_download_confirmation(make_message(), context, torrents, session=session)
    assert context.user_data["pending_season_download"] == torrents
    assert "torrents for" in safe_mock.await_args.kwargs["text"]
    safe_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_present_season_download_confirmation_pack(mocker, context, make_message):
    safe_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.safe_edit_message", new=AsyncMock()
    )
    session = SearchSession(media_type="tv", season=1)
    session.season_episode_count = 2
    session.save(context.user_data)
    torrents = [{"link": "a", "parsed_info": {"is_season_pack": True}}]
    await _present_season_download_confirmation(make_message(), context, torrents, session=session)
    assert context.user_data["pending_season_download"] == torrents
    assert "season pack" in safe_mock.await_args.kwargs["text"].lower()
    safe_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_present_season_download_confirmation_pack_has_reject_button(
    mocker, context, make_message
):
    safe_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.safe_edit_message", new=AsyncMock()
    )
    session = SearchSession(media_type="tv", season=1)
    session.season_episode_count = 10
    session.save(context.user_data)
    torrents = [{"link": "pack", "parsed_info": {"is_season_pack": True}}]
    message = make_message()
    await _present_season_download_confirmation(message, context, torrents, session=session)
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
        "telegram_bot.workflows.search_workflow.tv_flow.scraping_service.fetch_episode_titles_for_season",
        new=AsyncMock(return_value=({}, None)),
    )
    # Arrange minimal state and mocks
    session = SearchSession(media_type="tv", season=1, resolution="1080p")
    session.set_title("Show")
    session.save(context.user_data)
    safe_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.safe_edit_message", new=AsyncMock()
    )
    perf_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow._perform_tv_season_search",
        new=AsyncMock(),
    )

    message = make_message()
    update = make_update(callback_query=make_callback_query("reject_season_pack", message))

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
        "telegram_bot.workflows.search_workflow.tv_flow.scraping_service.fetch_episode_titles_for_season",
        new=AsyncMock(
            return_value=(
                {i: {"title": f"Ep {i}", "release_date": None} for i in range(1, 6)},
                None,
            )
        ),
    )
    # Mock messaging and data sources
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.safe_edit_message", new=AsyncMock()
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.scraping_service.fetch_season_episode_count_from_wikipedia",
        new=AsyncMock(return_value=5),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.plex_service.get_existing_episodes_for_season",
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
        "telegram_bot.workflows.search_workflow.tv_flow.search_logic.orchestrate_searches",
        new=AsyncMock(side_effect=orch_side_effect),
    )

    present_conf_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow._present_season_download_confirmation",
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
            callback_query=make_callback_query("search_tv_scope_season", make_message()),
        ),
        context,
    )

    # 2) User chooses 1080p
    session = SearchSession.from_user_data(context.user_data)
    # The session should have the missing episodes calculated
    assert session.season_episode_count == 5
    assert session.existing_episodes == [2, 4]
    assert session.missing_episode_numbers == [1, 3, 5]

    await handle_search_buttons(
        Update(
            update_id=2,
            callback_query=make_callback_query("search_tv_season_resolution_1080p", make_message()),
        ),
        context,
    )

    # 3) User chooses a codec
    await handle_search_buttons(
        Update(
            update_id=3,
            callback_query=make_callback_query("search_tv_season_codec_x265", make_message()),
        ),
        context,
    )

    # 4) User continues with the selected preferences
    await handle_search_buttons(
        Update(
            update_id=4,
            callback_query=make_callback_query(
                "search_tv_season_preferences_continue", make_message()
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
async def test_tv_season_preferences_prompt_uses_unified_filter_layout(
    mocker, context, make_message
):
    safe_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.preferences.safe_edit_message",
        new=AsyncMock(),
    )
    session = SearchSession(media_type="tv", season=1)
    session.resolution = "1080p"
    session.tv_codec = "x264"

    await _prompt_tv_season_preferences(make_message(), context, session)

    kwargs = safe_mock.await_args.kwargs
    labels = [btn.text for row in kwargs["reply_markup"].inline_keyboard for btn in row]
    assert "🟢1080p" in labels
    assert "🟢x264 / AVC" in labels
    assert "➡️ Search" in labels
    assert "Best Available" not in labels
    assert "Either Codec" not in labels


@pytest.mark.asyncio
async def test_entire_season_all_owned_exits_early(
    mocker, context, make_callback_query, make_message
):
    edit_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.safe_edit_message", new=AsyncMock()
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.scraping_service.fetch_season_episode_count_from_wikipedia",
        new=AsyncMock(return_value=3),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow.plex_service.get_existing_episodes_for_season",
        new=AsyncMock(return_value={1, 2, 3}),
    )
    perform_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.tv_flow._perform_tv_season_search",
        new=AsyncMock(),
    )

    # Seed title/season
    session = SearchSession(media_type="tv", step=SearchStep.TV_SCOPE, season=1)
    session.set_title("Show")
    session.save(context.user_data)

    await handle_search_buttons(
        Update(
            update_id=1,
            callback_query=make_callback_query("search_tv_scope_season", make_message()),
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
    size_gib: float,
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
        size_gib=size_gib,
        seeders=seeders,
        resolution=resolution,
        score=seeders,
    )


def test_consistent_episode_selection_prefers_uniform_release():
    candidates: dict[int, list[EpisodeCandidate]] = {}
    for ep in range(1, 4):
        candidates[ep] = [
            _make_candidate(ep, "SceneGroup", size_gib=1.0 + ep * 0.02),
            _make_candidate(ep, "Scatter", size_gib=1.9 + ep * 0.3, seeders=500),
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
                size_gib=4.4 + ep * 0.1,
                resolution="2160p",
                source="EZTV",
            ),
            _make_candidate(
                ep,
                "HDGroup",
                size_gib=1.0 + ep * 0.5,
                resolution="1080p",
                source="EZTV",
                seeders=400,
            ),
        ]

    selection, summary = _select_consistent_episode_set(candidates)
    assert all(c.resolution == "2160p" for c in selection)
    assert summary is not None
    assert summary.resolution == "2160p"
