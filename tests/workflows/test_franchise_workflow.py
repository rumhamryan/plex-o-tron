import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from telegram import Update, Chat, CallbackQuery
from telegram.ext import ContextTypes

from telegram_bot.workflows.franchise_workflow import (
    handle_franchise_confirmation,
    run_franchise_logic,
    handle_franchise_toggle,
    handle_franchise_confirm,
)


@pytest.mark.asyncio
async def test_handle_franchise_confirmation_triggers_logic():
    update = MagicMock(spec=Update)
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
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
async def test_run_franchise_logic_shows_ui():
    update = MagicMock(spec=Update)
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    chat = MagicMock(spec=Chat)
    chat.id = 123
    update.effective_chat = chat
    context.user_data = {}

    seed_torrent = {
        "parsed_info": {"title": "The Matrix", "year": 1999},
        "clean_name": "The Matrix 1999 1080p",
        "original_message_id": 100,
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
            "telegram_bot.workflows.franchise_workflow.safe_send_message",
            new_callable=AsyncMock,
        ) as mock_send,
        patch(
            "telegram_bot.workflows.franchise_workflow.safe_edit_message",
            new_callable=AsyncMock,
        ) as mock_edit,
    ):
        mock_fetch.return_value = franchise_info
        mock_send.return_value = MagicMock(message_id=999)

        await run_franchise_logic(update, context, seed_torrent)

        # Should populate user_data
        assert "franchise_pending" in context.user_data
        data = context.user_data["franchise_pending"]
        assert len(data["franchise_info"]["movies"]) == 2

        # Should show selection UI
        mock_edit.assert_awaited()
        args, kwargs = mock_edit.await_args
        assert "Select the movies" in kwargs["text"]


@pytest.mark.asyncio
async def test_franchise_confirm_flow():
    update = MagicMock(spec=Update)
    query = MagicMock(spec=CallbackQuery)
    query.message = MagicMock()
    update.callback_query = query

    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)

    franchise_info = {
        "name": "The Matrix",
        "movies": [
            {"title": "The Matrix", "year": 1999, "selected": True},
            {
                "title": "The Matrix Reloaded",
                "year": 2003,
                "selected": False,
            },  # Deselected
        ],
    }

    seed_torrent = {
        "parsed_info": {"title": "The Matrix", "year": 1999},
        "clean_name": "The Matrix 1999 1080p",
    }

    context.user_data = {
        "franchise_pending": {
            "franchise_info": franchise_info,
            "seed_torrent": seed_torrent,
            "chat_id": 123,
            "status_message_id": 999,
        }
    }

    context.bot_data = {"SAVE_PATHS": {"default": "/tmp"}}

    with (
        patch(
            "telegram_bot.services.search_logic.orchestrate_searches",
            new_callable=AsyncMock,
        ) as mock_search,
        patch(
            "telegram_bot.workflows.franchise_workflow.process_queue_for_user",
            new_callable=AsyncMock,
        ) as mock_process,
        patch(
            "telegram_bot.services.plex_service.create_plex_collection",
            new_callable=AsyncMock,
        ),
        patch(
            "telegram_bot.workflows.franchise_workflow.safe_edit_message",
            new_callable=AsyncMock,
        ),
        patch(
            "telegram_bot.workflows.franchise_workflow.safe_send_message",
            new_callable=AsyncMock,
        ) as mock_send,
        patch("os.makedirs"),
        patch("os.path.exists", return_value=False),
    ):
        mock_send.return_value = MagicMock(message_id=888)

        await handle_franchise_confirm(update, context)

        # Should NOT search for Reloaded because it was unselected
        mock_search.assert_not_awaited()

        # Should NOT search for The Matrix (seed)
        # Verify pending is cleared
        assert "franchise_pending" not in context.user_data


@pytest.mark.asyncio
async def test_franchise_toggle():
    update = MagicMock(spec=Update)
    query = MagicMock(spec=CallbackQuery)
    query.data = "franchise_toggle_1"
    update.callback_query = query
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)

    franchise_info = {
        "name": "The Matrix",
        "movies": [
            {"title": "The Matrix", "selected": True},
            {"title": "The Matrix Reloaded", "selected": True},
        ],
    }

    context.user_data = {
        "franchise_pending": {
            "franchise_info": franchise_info,
            "chat_id": 123,
            "status_message_id": 999,
        }
    }

    with patch(
        "telegram_bot.workflows.franchise_workflow.safe_edit_message",
        new_callable=AsyncMock,
    ) as mock_edit:
        await handle_franchise_toggle(update, context)

        # Should toggle index 1 to False
        assert franchise_info["movies"][1]["selected"] is False
        # Should refresh UI
        mock_edit.assert_awaited()
