import pytest
from unittest.mock import AsyncMock

from telegram import Update
from telegram.helpers import escape_markdown

from telegram_bot.workflows.delete_workflow import (
    handle_delete_buttons,
    handle_delete_workflow,
    _handle_confirm_delete_button,
    _present_delete_results,
    _has_name_twin,
    _calculate_path_size,
)


@pytest.mark.asyncio
async def test_delete_show_happy_path(
    mocker, context, make_callback_query, make_message
):
    mocker.patch(
        "telegram_bot.workflows.delete_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    find_media_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow.find_media_by_name",
        new=AsyncMock(return_value="/path/show"),
    )
    delete_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow._delete_item_from_plex",
        new=AsyncMock(
            return_value=(
                {"status": "not_found", "detail": "Could not find"},
                mocker.Mock(),
            )
        ),
    )
    rmtree_mock = mocker.patch("telegram_bot.workflows.delete_workflow.shutil.rmtree")
    mocker.patch(
        "telegram_bot.workflows.delete_workflow.os.path.exists", return_value=True
    )
    mocker.patch(
        "telegram_bot.workflows.delete_workflow.os.path.isdir", return_value=True
    )
    mocker.patch(
        "telegram_bot.workflows.delete_workflow.asyncio.to_thread",
        new=AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs)),
    )

    # Start delete TV workflow
    await handle_delete_buttons(
        Update(
            update_id=1,
            callback_query=make_callback_query("delete_start_tv", make_message()),
        ),
        context,
    )
    assert context.user_data["next_action"] == "delete_tv_show_search"

    # Provide show title
    context.bot_data["SAVE_PATHS"] = {}
    await handle_delete_workflow(
        Update(update_id=2, message=make_message("My Show")), context
    )
    find_media_mock.assert_awaited_once()
    assert context.user_data["show_path_to_delete"] == "/path/show"

    # Choose to delete entire show
    await handle_delete_buttons(
        Update(
            update_id=3,
            callback_query=make_callback_query("delete_tv_all", make_message()),
        ),
        context,
    )
    assert context.user_data["path_to_delete"] == "/path/show"

    # Confirm deletion
    context.bot_data["PLEX_CONFIG"] = {"url": "u", "token": "t"}
    await handle_delete_buttons(
        Update(
            update_id=4,
            callback_query=make_callback_query("confirm_delete", make_message()),
        ),
        context,
    )
    delete_mock.assert_awaited_once_with("/path/show", {"url": "u", "token": "t"})
    rmtree_mock.assert_called_once_with("/path/show")


@pytest.mark.asyncio
async def test_delete_workflow_not_found(mocker, context, make_message):
    safe_edit_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.workflows.delete_workflow.find_media_by_name",
        new=AsyncMock(return_value=None),
    )
    context.bot_data["SAVE_PATHS"] = {}
    context.user_data["next_action"] = "delete_tv_show_search"

    await handle_delete_workflow(
        Update(update_id=1, message=make_message("Unknown")), context
    )
    assert (
        "No single TV show directory found" in safe_edit_mock.await_args.kwargs["text"]
    )


@pytest.mark.asyncio
async def test_confirm_delete_skip_due_to_name_twin(
    mocker, context, make_callback_query, make_message
):
    safe_edit_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    delete_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow._delete_item_from_plex",
        new=AsyncMock(
            return_value=(
                {
                    "status": "skip",
                    "detail": "Skipped due to twin",
                    "plex_deleted": False,
                },
                mocker.Mock(),
            )
        ),
    )
    fs_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow._delete_from_filesystem",
        new=AsyncMock(return_value=(True, "file")),
    )
    mocker.patch(
        "telegram_bot.workflows.delete_workflow._format_size_label",
        return_value="4.5 GB",
    )

    context.user_data["path_to_delete"] = "/downloads/movie.mkv"
    context.user_data["delete_target_kind"] = "movie_file"
    context.bot_data["PLEX_CONFIG"] = {"url": "u", "token": "t"}

    await _handle_confirm_delete_button(
        make_callback_query("confirm_delete", make_message()), context
    )

    delete_mock.assert_awaited_once()
    fs_mock.assert_awaited_once_with("/downloads/movie.mkv")
    final_text = safe_edit_mock.await_args.kwargs["text"]
    assert "Plex Skipped" in final_text
    assert "\nSize: 4\\.5 GB" in final_text


@pytest.mark.asyncio
async def test_confirm_delete_placeholder_token_uses_manual_delete(
    mocker, context, make_callback_query, make_message
):
    safe_edit_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    delete_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow._delete_item_from_plex",
        new=AsyncMock(),
    )
    fs_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow._delete_from_filesystem",
        new=AsyncMock(return_value=(True, "file")),
    )
    mocker.patch(
        "telegram_bot.workflows.delete_workflow._format_size_label",
        return_value="1.0 GB",
    )

    context.user_data["path_to_delete"] = "/downloads/movie.mkv"
    context.bot_data["PLEX_CONFIG"] = {"url": "u", "token": "PLEX_TOKEN"}

    await _handle_confirm_delete_button(
        make_callback_query("confirm_delete", make_message()), context
    )

    delete_mock.assert_not_called()
    fs_mock.assert_awaited_once_with("/downloads/movie.mkv")
    final_text = safe_edit_mock.await_args.kwargs["text"]
    assert "Deleted From Disk" in final_text
    assert "placeholder" in final_text


@pytest.mark.asyncio
async def test_confirm_delete_manual_fallback_on_connection_error(
    mocker, context, make_callback_query, make_message
):
    safe_edit_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    delete_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow._delete_item_from_plex",
        new=AsyncMock(
            return_value=(
                {
                    "status": "error",
                    "detail": "An error occurred while communicating with Plex: boom",
                },
                None,
            )
        ),
    )
    fs_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow._delete_from_filesystem",
        new=AsyncMock(return_value=(True, "file")),
    )
    mocker.patch(
        "telegram_bot.workflows.delete_workflow._format_size_label",
        return_value="3.0 GB",
    )

    context.user_data["path_to_delete"] = "/downloads/show"
    context.bot_data["PLEX_CONFIG"] = {"url": "u", "token": "t"}

    await _handle_confirm_delete_button(
        make_callback_query("confirm_delete", make_message()), context
    )

    delete_mock.assert_awaited_once()
    fs_mock.assert_awaited_once_with("/downloads/show")
    final_text = safe_edit_mock.await_args.kwargs["text"]
    assert "Plex Unavailable" in final_text
    assert "boom" in final_text


@pytest.mark.asyncio
async def test_collection_delete_triggers_plex_cleanup(
    mocker, context, make_callback_query, make_message
):
    safe_edit_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    delete_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow._delete_item_from_plex",
        new=AsyncMock(
            return_value=(
                {
                    "status": "success",
                    "detail": "Deleted Plex item",
                    "plex_deleted": True,
                },
                mocker.Mock(),
            )
        ),
    )
    collection_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow._delete_plex_collection",
        new=AsyncMock(return_value=True),
    )
    mocker.patch(
        "telegram_bot.workflows.delete_workflow._format_size_label",
        return_value="1.0 GB",
    )
    mocker.patch(
        "telegram_bot.workflows.delete_workflow.os.path.exists", return_value=False
    )

    context.user_data["path_to_delete"] = "/downloads/MovieCollection"
    context.bot_data["PLEX_CONFIG"] = {"url": "u", "token": "t"}

    await _handle_confirm_delete_button(
        make_callback_query("confirm_delete", make_message()), context
    )

    delete_mock.assert_awaited_once()
    collection_mock.assert_not_awaited()
    final_text = safe_edit_mock.await_args.kwargs["text"]
    assert "Successfully Deleted from Plex" in final_text
    assert "\nSize: 1\\.0 GB" in final_text


@pytest.mark.asyncio
async def test_present_delete_results_lists_show_sizes(mocker, context, make_message):
    safe_edit_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.workflows.delete_workflow._format_size_label",
        side_effect=lambda path: "1.0 GB" if path.endswith("a.mkv") else "2.0 GB",
    )
    context.user_data.clear()

    await _present_delete_results(
        [
            "/downloads/movie_a.mkv",
            "/downloads/movie_b.mkv",
        ],
        make_message(),
        "single movie",
        "Movie",
        context,
        "movie_file",
    )

    reply_markup = safe_edit_mock.await_args.kwargs["reply_markup"]
    buttons = reply_markup.inline_keyboard
    assert "movie_a.mkv | 1.0 GB" in buttons[0][0].text
    assert "movie_b.mkv | 2.0 GB" in buttons[1][0].text


@pytest.mark.asyncio
async def test_success_message_includes_extension_and_size(
    mocker, context, make_callback_query, make_message
):
    safe_edit_mock = mocker.patch(
        "telegram_bot.workflows.delete_workflow.safe_edit_message",
        new=AsyncMock(),
    )
    mocker.patch(
        "telegram_bot.workflows.delete_workflow._delete_item_from_plex",
        new=AsyncMock(
            return_value=(
                {
                    "status": "success",
                    "detail": "Deleted Plex item",
                    "plex_deleted": True,
                },
                mocker.Mock(),
            )
        ),
    )
    mocker.patch(
        "telegram_bot.workflows.delete_workflow._format_size_label",
        return_value="4.0 GB",
    )
    mocker.patch(
        "telegram_bot.workflows.delete_workflow.os.path.exists", return_value=False
    )

    context.user_data["path_to_delete"] = "/downloads/movie.mkv"
    context.user_data["delete_target_kind"] = "movie_file"
    context.bot_data["PLEX_CONFIG"] = {"url": "u", "token": "t"}

    await _handle_confirm_delete_button(
        make_callback_query("confirm_delete", make_message()), context
    )

    final_text = safe_edit_mock.await_args.kwargs["text"]
    escaped_name = escape_markdown("movie.mkv", version=2)
    assert f"{escaped_name}\nSize: 4\\.0 GB" in final_text
    assert "Successfully Deleted from Plex" in final_text


def test_has_name_twin_detects_case_insensitive(tmp_path):
    first = tmp_path / "Movie.File.mkv"
    first.write_text("data")
    twin = tmp_path / "movie.file.mp4"
    twin.write_text("more data")

    assert _has_name_twin(str(first)) is True
    assert _has_name_twin(str(twin)) is True


def test_has_name_twin_false_for_missing(tmp_path):
    missing = tmp_path / "missing.file.mkv"
    assert _has_name_twin(str(missing)) is False


def test_calculate_path_size_file(tmp_path):
    target = tmp_path / "movie.mkv"
    data = b"x" * 512
    target.write_bytes(data)
    assert _calculate_path_size(str(target)) == len(data)


def test_calculate_path_size_directory(tmp_path):
    root = tmp_path / "collection"
    root.mkdir()
    (root / "part1.bin").write_bytes(b"a" * 256)
    nested = root / "nested"
    nested.mkdir()
    (nested / "part2.bin").write_bytes(b"b" * 128)
    assert _calculate_path_size(str(root)) == 384
