from datetime import date
from unittest.mock import AsyncMock

import pytest
from telegram import CallbackQuery, Update

from telegram_bot.workflows.tracking_workflow.handlers import (
    TRACKING_AWAIT_MOVIE_TITLE,
    _candidate_summary_line,
    _format_tracking_item_line,
    _tracking_menu_text,
    handle_tracking_buttons,
    handle_tracking_workflow_message,
)
from telegram_bot.workflows.tracking_workflow.state import TRACKING_NEXT_ACTION_KEY


def test_tracking_menu_text_escapes_markdown_reserved_characters():
    menu_text = _tracking_menu_text()
    assert "Auto\\-Download" in menu_text


def test_candidate_summary_line_escapes_markdown_reserved_characters():
    line = _candidate_summary_line(
        {
            "canonical_title": "Future Movie",
            "year": 2030,
            "availability_date": None,
        }
    )
    assert line.startswith("\\- Future Movie \\(2030\\)\n")
    assert "\n  Source: metadata\\-only checks\n" in line
    assert line.endswith("Date: TBD")
    assert " (2030)" not in line


def test_tracking_item_line_escapes_markdown_reserved_characters():
    line = _format_tracking_item_line(
        {
            "canonical_title": "Project Hail Mary (film)",
            "year": 2026,
            "status": "waiting_release_window",
            "next_check_at_utc": "2026-04-28T19:00:00Z",
        },
        1,
    )
    assert "\\(film\\)" in line
    assert " \\(2026\\)" in line
    assert " (2026)" not in line


@pytest.mark.asyncio
async def test_tracking_workflow_schedules_valid_future_movie(
    mocker, make_message, make_callback_query, context
):
    context.application.bot_data = context.bot_data
    mocker.patch.object(CallbackQuery, "answer", AsyncMock())
    edit_mock = mocker.patch(
        "telegram_bot.workflows.tracking_workflow.handlers.safe_edit_message",
        AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.services.tracking.manager.persist_tracking_state_from_bot_data",
        return_value=None,
    )

    start_message = make_message(message_id=10)
    start_query = make_callback_query("track_schedule_movie", start_message)
    start_update = Update(update_id=1, callback_query=start_query)
    await handle_tracking_buttons(start_update, context)
    assert context.user_data.get(TRACKING_NEXT_ACTION_KEY) == TRACKING_AWAIT_MOVIE_TITLE

    candidate = {
        "title": "Future Movie",
        "canonical_title": "Future Movie",
        "year": 2030,
        "is_released": False,
        "release_date_status": "confirmed",
        "availability_date": date(2030, 1, 2),
        "availability_source": "streaming",
    }
    mocker.patch(
        "telegram_bot.workflows.tracking_workflow.handlers.movie_release_dates.find_movie_tracking_candidates",
        AsyncMock(return_value=[candidate]),
    )

    context.bot.send_message = AsyncMock()
    user_message = make_message("Future Movie", message_id=11)
    text_update = Update(update_id=2, message=user_message)
    await handle_tracking_workflow_message(text_update, context)

    context.bot.send_message.assert_not_called()
    assert any(
        call.kwargs.get("message_id") == start_message.message_id
        for call in edit_mock.await_args_list
    )
    assert TRACKING_NEXT_ACTION_KEY not in context.user_data
    assert len(context.user_data.get("tracking_candidates", [])) == 1

    pick_query = make_callback_query("track_pick_0", start_message)
    pick_update = Update(update_id=3, callback_query=pick_query)
    await handle_tracking_buttons(pick_update, context)

    confirm_query = make_callback_query("track_confirm", start_message)
    confirm_update = Update(update_id=4, callback_query=confirm_query)
    await handle_tracking_buttons(confirm_update, context)

    tracking_items = context.bot_data.get("tracking_items", {})
    assert len(tracking_items) == 1
    item = next(iter(tracking_items.values()))
    assert item["target_kind"] == "movie"
    assert item["canonical_title"] == "Future Movie"
    assert item["year"] == 2030
    assert item["release_date_status"] == "confirmed"
    assert item["availability_date"] == "2030-01-02"
    assert item["status"] == "waiting_release_window"
    assert item["next_check_at_utc"] is not None
    assert item["fulfillment_state"] == "pending"

    final_text = edit_mock.await_args_list[-1].kwargs["text"]
    assert "Scheduled tracking created" in final_text


@pytest.mark.asyncio
async def test_tracking_workflow_rejects_already_released_movie(
    mocker, make_message, make_callback_query, context
):
    context.application.bot_data = context.bot_data
    mocker.patch.object(CallbackQuery, "answer", AsyncMock())
    edit_mock = mocker.patch(
        "telegram_bot.workflows.tracking_workflow.handlers.safe_edit_message",
        AsyncMock(),
    )
    create_mock = mocker.patch(
        "telegram_bot.workflows.tracking_workflow.handlers.tracking_manager.create_movie_tracking_item"
    )

    start_message = make_message(message_id=30)
    start_query = make_callback_query("track_schedule_movie", start_message)
    start_update = Update(update_id=1, callback_query=start_query)
    await handle_tracking_buttons(start_update, context)
    assert context.user_data.get(TRACKING_NEXT_ACTION_KEY) == TRACKING_AWAIT_MOVIE_TITLE

    released_candidate = {
        "title": "Old Movie",
        "canonical_title": "Old Movie",
        "year": 2010,
        "is_released": True,
        "release_date_status": "confirmed",
        "availability_date": date(2010, 8, 1),
        "availability_source": "physical",
    }
    mocker.patch(
        "telegram_bot.workflows.tracking_workflow.handlers.movie_release_dates.find_movie_tracking_candidates",
        AsyncMock(return_value=[released_candidate]),
    )
    context.bot.send_message = AsyncMock()

    user_message = make_message("Old Movie", message_id=32)
    text_update = Update(update_id=2, message=user_message)
    await handle_tracking_workflow_message(text_update, context)

    context.bot.send_message.assert_not_called()
    assert any(
        call.kwargs.get("message_id") == start_message.message_id
        for call in edit_mock.await_args_list
    )
    create_mock.assert_not_called()
    assert not context.bot_data.get("tracking_items")
    assert TRACKING_NEXT_ACTION_KEY not in context.user_data
    rejection_text = edit_mock.await_args_list[-1].kwargs["text"].lower()
    assert "already released" in rejection_text or "no future release" in rejection_text
