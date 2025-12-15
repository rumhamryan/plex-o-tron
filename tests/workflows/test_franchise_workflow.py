import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from telegram import Update, Chat
from telegram.ext import ContextTypes

from telegram_bot.workflows.franchise_workflow import (
    handle_franchise_confirmation,
    run_franchise_logic,
)


@pytest.mark.asyncio
async def test_handle_franchise_confirmation_triggers_logic():
    update = MagicMock(spec=Update)
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    # The fix relies on pending_torrent.parsed_info.collection_mode
    context.user_data = {
        "pending_torrent": {
            "parsed_info": {"title": "The Matrix", "collection_mode": True}
        },
    }

    with (
        patch(
            "telegram_bot.workflows.franchise_workflow.add_download_to_queue",
            new_callable=AsyncMock,
        ) as mock_add,
        patch(
            "telegram_bot.workflows.franchise_workflow.run_franchise_logic",
            new_callable=AsyncMock,
        ) as mock_run,
    ):
        await handle_franchise_confirmation(update, context)

        mock_add.assert_awaited_once()
        mock_run.assert_called_once()


@pytest.mark.asyncio
async def test_run_franchise_logic_flow():
    update = MagicMock(spec=Update)
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    chat = MagicMock(spec=Chat)
    chat.id = 123
    update.effective_chat = chat

    seed_torrent = {
        "parsed_info": {"title": "The Matrix", "year": 1999},
        "clean_name": "The Matrix 1999 1080p",
        "original_message_id": 100,
    }

    context.bot_data = {
        "SAVE_PATHS": {"default": "/tmp/downloads", "movies": "/tmp/movies"}
    }

    franchise_info = {
        "name": "The Matrix",
        "movies": [
            {"title": "The Matrix", "year": 1999},
            {"title": "The Matrix Reloaded", "year": 2003},
        ],
    }

    with (
        patch(
            "telegram_bot.services.scraping_service.fetch_franchise_details",
            new_callable=AsyncMock,
        ) as mock_fetch,
        patch(
            "telegram_bot.services.search_logic.orchestrate_searches",
            new_callable=AsyncMock,
        ) as mock_search,
        patch(
            "telegram_bot.services.plex_service.create_plex_collection",
            new_callable=AsyncMock,
        ) as mock_plex,
        patch(
            "telegram_bot.workflows.franchise_workflow.safe_send_message",
            new_callable=AsyncMock,
        ) as mock_send,
        patch(
            "telegram_bot.workflows.franchise_workflow.safe_edit_message",
            new_callable=AsyncMock,
        ),
        patch(
            "telegram_bot.workflows.franchise_workflow.process_queue_for_user",
            new_callable=AsyncMock,
        ),
        patch("os.makedirs") as mock_makedirs,
        patch("os.path.exists", return_value=False),
    ):
        mock_fetch.return_value = franchise_info
        mock_search.return_value = [
            {"title": "The Matrix Reloaded", "size_gb": 5, "page_url": "http://test"}
        ]
        mock_send.return_value = MagicMock(message_id=999)

        await run_franchise_logic(update, context, seed_torrent)

        mock_fetch.assert_awaited_with("The Matrix")
        # Should search for Reloaded
        mock_search.assert_awaited()
        # Should not search for The Matrix (seed)
        args, _ = mock_search.call_args
        assert "Reloaded" in args[0]

        # Should create collection
        mock_plex.assert_awaited()

        # Should create directory
        mock_makedirs.assert_called()
