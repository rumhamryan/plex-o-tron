import pytest
from unittest.mock import AsyncMock
from telegram import Update
from telegram_bot.workflows.search_session import SearchSession, SearchStep
from telegram_bot.workflows.search_workflow import handle_search_buttons


@pytest.mark.asyncio
async def test_future_episodes_are_skipped(
    mocker, context, make_callback_query, make_message
):
    # Mock datetime.now() to return a fixed date: 2023-01-01
    mock_datetime = mocker.patch("telegram_bot.workflows.search_workflow.datetime")
    mock_datetime.now.return_value.date.return_value.isoformat.return_value = (
        "2023-01-01"
    )

    # Episodes: 1 (past), 2 (today), 3 (future)
    titles_meta = {
        1: {"title": "Ep1", "release_date": "2022-12-31"},
        2: {"title": "Ep2", "release_date": "2023-01-01"},
        3: {"title": "Ep3", "release_date": "2023-01-02"},
    }

    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_episode_titles_for_season",
        new=AsyncMock(return_value=(titles_meta, None)),
    )

    mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message", new=AsyncMock()
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_season_episode_count_from_wikipedia",
        new=AsyncMock(return_value=3),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.plex_service.get_existing_episodes_for_season",
        new=AsyncMock(return_value=set()),
    )

    mocker.patch(
        "telegram_bot.workflows.search_workflow._prompt_tv_season_resolution",
        new=AsyncMock(),
    )

    orch_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.search_logic.orchestrate_searches",
        new=AsyncMock(return_value=[]),
    )

    mocker.patch(
        "telegram_bot.workflows.search_workflow._present_season_download_confirmation",
        new=AsyncMock(),
    )

    session = SearchSession(media_type="tv", step=SearchStep.TV_SCOPE, season=1)
    session.set_title("Show")
    session.resolution = "1080p"  # Set resolution to avoid prompt
    session.season_episode_count = 3
    session.save(context.user_data)

    # Simulate user selecting "Any Codec", which triggers _perform_tv_season_search
    await handle_search_buttons(
        Update(
            update_id=1,
            callback_query=make_callback_query(
                "search_tv_season_codec_any", make_message()
            ),
        ),
        context,
    )

    # Check called searches
    searched_eps = []
    for call in orch_mock.await_args_list:
        q = call.args[0]
        # Regex to find episode number S01E0X
        import re

        m = re.search(r"S01E(\d{2})", q)
        if m:
            searched_eps.append(int(m.group(1)))

    # Expect 1 and 2 to be searched (past and today). 3 should be skipped (future).
    assert 1 in searched_eps
    assert 2 in searched_eps
    assert 3 not in searched_eps
