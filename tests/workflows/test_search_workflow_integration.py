import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock
import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from telegram_bot.workflows import search_workflow
from telegram_bot.workflows.search_session import SearchSession


@pytest.mark.asyncio
async def test_tv_season_fallback_uses_wiki_titles_and_corrected_title(mocker):
    # Mock episode titles and corrected show title from Wikipedia
    mocker.patch(
        "telegram_bot.workflows.search_workflow.scraping_service.fetch_episode_titles_for_season",
        new=AsyncMock(return_value=({1: "Pilot"}, "Show (TV series)")),
    )

    # Season queries return no results to force fallback
    async def orch_side_effect(query, media_type, context, **kwargs):
        # Only return a result for explicit episode queries
        if "E01" in query:
            return [
                {
                    "title": "Show.S01E01.1080p.WEB.x265",
                    "page_url": "magnet:?xt=urn:btih:FAKE",
                    "score": 10,
                }
            ]
        return []

    m_orch = mocker.patch(
        "telegram_bot.workflows.search_workflow.search_logic.orchestrate_searches",
        new=AsyncMock(side_effect=orch_side_effect),
    )

    # Stub helpers used inside workflow
    mocker.patch(
        "telegram_bot.workflows.search_workflow.safe_edit_message",
        new=AsyncMock(return_value=None),
    )
    mocker.patch(
        "telegram_bot.workflows.search_workflow.parse_torrent_name",
        return_value={},
    )

    captured = {}

    async def _capture_confirmation(
        _message, _context, torrents, *, session=None, consistency_summary=None
    ):
        captured["torrents"] = torrents
        captured["summary"] = consistency_summary

    mocker.patch(
        "telegram_bot.workflows.search_workflow._present_season_download_confirmation",
        new=AsyncMock(side_effect=_capture_confirmation),
    )

    # Minimal context and message mocks
    ctx = Mock()
    ctx.user_data = {}
    session = SearchSession(media_type="tv")
    session.season_episode_count = 1
    session.save(ctx.user_data)
    message = Mock()

    await search_workflow._perform_tv_season_search(
        message,
        ctx,
        title="Show",
        season=1,
        force_individual_episodes=True,
    )

    # Orchestrator called first with season queries, then with episode query
    all_calls = [c.args[0] for c in m_orch.await_args_list]
    assert any(q.endswith("S01") or q.endswith("Season 1") for q in all_calls)
    assert any("S01E01" in q for q in all_calls)

    # Confirmation received with parsed_info enriched by Wikipedia
    torrents = captured.get("torrents")
    assert torrents and len(torrents) == 1
    pi = torrents[0]["parsed_info"]
    assert pi["episode_title"] == "Pilot"
    assert pi["title"] == "Show (TV series)"
    assert pi["season"] == 1 and pi["episode"] == 1 and pi["type"] == "tv"
