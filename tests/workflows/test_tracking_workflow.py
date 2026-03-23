from datetime import date
from unittest.mock import AsyncMock

import pytest
from telegram import CallbackQuery, Update

from telegram_bot.workflows.tracking_workflow.handlers import (
    TRACKING_AWAIT_MOVIE_TITLE,
    TRACKING_AWAIT_TV_TITLE,
    _candidate_summary_line,
    _tracking_menu_keyboard,
    _tracking_menu_text,
    handle_tracking_buttons,
    handle_tracking_workflow_message,
)
from telegram_bot.workflows.tracking_workflow.state import (
    TRACKING_NEXT_ACTION_KEY,
    TRACKING_TARGET_KIND_KEY,
)


def test_tracking_menu_text_escapes_markdown_reserved_characters():
    menu_text = _tracking_menu_text()
    assert "Auto\\-Download" in menu_text


def test_tracking_menu_keyboard_uses_compact_schedule_labels():
    keyboard = _tracking_menu_keyboard()
    rows = keyboard.inline_keyboard
    assert rows[0][0].text == "🎬 Schedule Movie"
    assert rows[1][0].text == "📺 Schedule TV Show"
    assert rows[2][0].text == "📋 Review Scheduled Items"


def test_candidate_summary_line_movie_escapes_markdown_reserved_characters():
    line = _candidate_summary_line(
        {
            "target_kind": "movie",
            "canonical_title": "Future Movie",
            "year": 2030,
            "availability_date": None,
        }
    )
    assert line.startswith("\\- Future Movie \\(2030\\)\n")
    assert "\n  Source: metadata\\-only checks\n" in line
    assert line.endswith("Date: TBD")


def test_candidate_summary_line_tv_includes_schedule_mode_and_dates():
    line = _candidate_summary_line(
        {
            "target_kind": "tv",
            "canonical_title": "Future Show",
            "first_air_date": date(2020, 1, 1),
            "next_air_date": date(2026, 7, 2),
        }
    )
    assert "\\- Future Show" in line
    assert "ongoing\\_next\\_episode" in line
    assert "2026\\-07\\-02" in line


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
    return_home_mock = mocker.patch(
        "telegram_bot.workflows.tracking_workflow.handlers.return_to_home",
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
    assert context.user_data.get(TRACKING_TARGET_KIND_KEY) == "movie"

    candidate = {
        "target_kind": "movie",
        "schedule_mode": "future_release",
        "title": "Future Movie",
        "canonical_title": "Future Movie",
        "year": 2030,
        "is_released": False,
        "release_date_status": "confirmed",
        "availability_date": date(2030, 1, 2),
        "availability_source": "streaming",
    }
    mocker.patch(
        "telegram_bot.workflows.tracking_workflow.handlers.MOVIE_TRACKING_ADAPTER.resolve_candidates_from_user_input",
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
    assert context.user_data.get("tracking_selected_candidate_index") == 0

    confirm_query = make_callback_query("track_confirm", start_message)
    confirm_update = Update(update_id=3, callback_query=confirm_query)
    await handle_tracking_buttons(confirm_update, context)

    tracking_items = context.bot_data.get("tracking_items", {})
    assert len(tracking_items) == 1
    item = next(iter(tracking_items.values()))
    assert item["target_kind"] == "movie"
    assert item["schedule_mode"] == "future_release"
    assert item["canonical_title"] == "Future Movie"
    assert item["year"] == 2030
    assert item["release_date_status"] == "confirmed"
    assert item["availability_date"] == "2030-01-02"
    assert item["status"] == "awaiting_window"
    assert item["next_check_at_utc"] is not None
    assert item["fulfillment_state"] == "pending"

    return_home_mock.assert_awaited_once()
    success_message = return_home_mock.await_args.kwargs["message_text"]
    assert isinstance(success_message, str)
    assert "Schedule created" in success_message
    assert "Future Movie" in success_message


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
        "target_kind": "movie",
        "title": "Old Movie",
        "canonical_title": "Old Movie",
        "year": 2010,
        "is_released": True,
        "release_date_status": "confirmed",
        "availability_date": date(2010, 8, 1),
        "availability_source": "physical",
    }
    mocker.patch(
        "telegram_bot.workflows.tracking_workflow.handlers.MOVIE_TRACKING_ADAPTER.resolve_candidates_from_user_input",
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
    assert context.user_data.get(TRACKING_NEXT_ACTION_KEY) == TRACKING_AWAIT_MOVIE_TITLE
    rejection_text = edit_mock.await_args_list[-1].kwargs["text"].lower()
    assert "already released" in rejection_text or "no future release" in rejection_text
    assert "send another movie title" in rejection_text
    keyboard = edit_mock.await_args_list[-1].kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data == "cancel_operation"


@pytest.mark.asyncio
async def test_tracking_workflow_keeps_selection_step_for_multiple_candidates(
    mocker, make_message, make_callback_query, context
):
    context.application.bot_data = context.bot_data
    mocker.patch.object(CallbackQuery, "answer", AsyncMock())
    edit_mock = mocker.patch(
        "telegram_bot.workflows.tracking_workflow.handlers.safe_edit_message",
        AsyncMock(),
    )

    start_message = make_message(message_id=50)
    start_query = make_callback_query("track_schedule_movie", start_message)
    start_update = Update(update_id=1, callback_query=start_query)
    await handle_tracking_buttons(start_update, context)

    candidates = [
        {
            "target_kind": "movie",
            "title": "Future Movie A",
            "canonical_title": "Future Movie A",
            "year": 2030,
            "is_released": False,
            "release_date_status": "confirmed",
            "availability_date": date(2030, 1, 2),
            "availability_source": "streaming",
        },
        {
            "target_kind": "movie",
            "title": "Future Movie B",
            "canonical_title": "Future Movie B",
            "year": 2031,
            "is_released": False,
            "release_date_status": "confirmed",
            "availability_date": date(2031, 3, 12),
            "availability_source": "physical",
        },
    ]
    mocker.patch(
        "telegram_bot.workflows.tracking_workflow.handlers.MOVIE_TRACKING_ADAPTER.resolve_candidates_from_user_input",
        AsyncMock(return_value=candidates),
    )

    context.bot.send_message = AsyncMock()
    user_message = make_message("Future Movie", message_id=51)
    text_update = Update(update_id=2, message=user_message)
    await handle_tracking_workflow_message(text_update, context)

    assert context.user_data.get("tracking_selected_candidate_index") is None
    latest_text = edit_mock.await_args_list[-1].kwargs["text"]
    assert "Select A Movie To Schedule" in latest_text
    keyboard = edit_mock.await_args_list[-1].kwargs["reply_markup"]
    assert keyboard.inline_keyboard[-1][0].callback_data == "cancel_operation"


@pytest.mark.asyncio
async def test_tracking_workflow_schedules_tv_ongoing_item(
    mocker, make_message, make_callback_query, context
):
    context.application.bot_data = context.bot_data
    mocker.patch.object(CallbackQuery, "answer", AsyncMock())
    return_home_mock = mocker.patch(
        "telegram_bot.workflows.tracking_workflow.handlers.return_to_home",
        AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.services.tracking.manager.persist_tracking_state_from_bot_data",
        return_value=None,
    )

    start_message = make_message(message_id=100)
    start_query = make_callback_query("track_schedule_tv", start_message)
    start_update = Update(update_id=1, callback_query=start_query)
    await handle_tracking_buttons(start_update, context)
    assert context.user_data.get(TRACKING_NEXT_ACTION_KEY) == TRACKING_AWAIT_TV_TITLE
    assert context.user_data.get(TRACKING_TARGET_KIND_KEY) == "tv"

    tv_candidate = {
        "target_kind": "tv",
        "schedule_mode": "ongoing_next_episode",
        "title": "Future Show",
        "canonical_title": "Future Show",
        "tmdb_series_id": 1234,
        "first_air_date": date(2021, 1, 1),
        "next_air_date": date(2026, 6, 15),
    }
    mocker.patch(
        "telegram_bot.workflows.tracking_workflow.handlers.TV_ONGOING_TRACKING_ADAPTER.resolve_candidates_from_user_input",
        AsyncMock(return_value=[tv_candidate]),
    )

    user_message = make_message("Future Show", message_id=101)
    text_update = Update(update_id=2, message=user_message)
    await handle_tracking_workflow_message(text_update, context)

    confirm_query = make_callback_query("track_confirm", start_message)
    confirm_update = Update(update_id=3, callback_query=confirm_query)
    await handle_tracking_buttons(confirm_update, context)

    tracking_items = context.bot_data.get("tracking_items", {})
    assert len(tracking_items) == 1
    item = next(iter(tracking_items.values()))
    assert item["target_kind"] == "tv"
    assert item["schedule_mode"] == "ongoing_next_episode"
    assert item["display_title"] == "Future Show"
    assert item["status"] == "awaiting_metadata"
    assert item["target_payload"]["tmdb_series_id"] == 1234
    assert item["target_payload"]["episode_cursor"] is None
    assert item["target_payload"]["pending_episode"] is None
    assert item["next_check_at_utc"] is not None
    return_home_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_tracking_review_message_does_not_repeat_item_list(
    mocker, make_message, make_callback_query, context
):
    context.application.bot_data = context.bot_data
    mocker.patch.object(CallbackQuery, "answer", AsyncMock())
    edit_mock = mocker.patch(
        "telegram_bot.workflows.tracking_workflow.handlers.safe_edit_message",
        AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.workflows.tracking_workflow.handlers.tracking_manager.list_tracking_items",
        return_value=[
            {"id": "abc123", "target_kind": "movie", "display_title": "Movie One"},
            {"id": "def456", "target_kind": "tv", "display_title": "Show Two"},
        ],
    )

    message = make_message(message_id=70)
    review_query = make_callback_query("track_review", message)
    review_update = Update(update_id=1, callback_query=review_query)
    await handle_tracking_buttons(review_update, context)

    text = edit_mock.await_args.kwargs["text"]
    assert "Active Scheduled Items" in text
    assert "Movie One" not in text
    assert "Show Two" not in text
    keyboard = edit_mock.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[-1][0].callback_data == "cancel_operation"


@pytest.mark.asyncio
async def test_tracking_review_with_no_items_returns_home_with_notice(
    mocker, make_message, make_callback_query, context
):
    context.application.bot_data = context.bot_data
    mocker.patch.object(CallbackQuery, "answer", AsyncMock())
    return_home_mock = mocker.patch(
        "telegram_bot.workflows.tracking_workflow.handlers.return_to_home",
        AsyncMock(),
    )
    edit_mock = mocker.patch(
        "telegram_bot.workflows.tracking_workflow.handlers.safe_edit_message",
        AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.workflows.tracking_workflow.handlers.tracking_manager.list_tracking_items",
        return_value=[],
    )

    message = make_message(message_id=71)
    review_query = make_callback_query("track_review", message)
    review_update = Update(update_id=1, callback_query=review_query)
    await handle_tracking_buttons(review_update, context)

    return_home_mock.assert_awaited_once()
    assert return_home_mock.await_args.kwargs["source_message"] is message
    assert "No scheduled items to review" in return_home_mock.await_args.kwargs["message_text"]
    edit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_tracking_cancel_confirm_exits_to_home_and_removes_item(
    mocker, make_message, make_callback_query, context
):
    context.application.bot_data = context.bot_data
    mocker.patch.object(CallbackQuery, "answer", AsyncMock())
    return_home_mock = mocker.patch(
        "telegram_bot.workflows.tracking_workflow.handlers.return_to_home",
        AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.services.tracking.manager.persist_tracking_state_from_bot_data",
        return_value=None,
    )

    context.bot_data["tracking_items"] = {
        "trk_1234": {
            "id": "trk_1234",
            "chat_id": 456,
            "target_kind": "movie",
            "display_title": "Future Movie",
            "canonical_title": "Future Movie",
            "title": "Future Movie",
            "status": "awaiting_window",
            "created_at_utc": "2026-03-23T00:00:00Z",
            "target_payload": {"canonical_title": "Future Movie"},
            "retry": {"consecutive_failures": 0, "last_error": None},
        }
    }

    message = make_message(message_id=90)
    confirm_query = make_callback_query("track_cancel_confirm_trk_1234", message)
    confirm_update = Update(update_id=1, callback_query=confirm_query)
    await handle_tracking_buttons(confirm_update, context)

    assert "trk_1234" not in context.bot_data["tracking_items"]
    return_home_mock.assert_awaited_once()
    assert return_home_mock.await_args.kwargs["message_text"] == "✅ Scheduled item cancelled\\."
