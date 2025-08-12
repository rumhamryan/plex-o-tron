import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from telegram_bot.state import (
    load_state,
    post_init,
    post_shutdown,
    save_state,
)


def test_save_and_load_state_roundtrip(mocker):
    file_path = "state.json"
    active_downloads = {
        "123": {
            "name": "test",
            "task": object(),
            "lock": object(),
            "handle": object(),
            "requeued": False,
        }
    }
    download_queues = {"123": [{"name": "queued"}]}

    mock_open_write = mocker.mock_open()
    mocker.patch("builtins.open", mock_open_write)

    save_state(file_path, active_downloads, download_queues)

    handle = mock_open_write()
    written_data = "".join(call.args[0] for call in handle.write.call_args_list)
    assert "task" not in written_data
    assert "lock" not in written_data
    assert "handle" not in written_data

    mock_open_read = mocker.mock_open(read_data=written_data)
    mocker.patch("builtins.open", mock_open_read)
    mocker.patch("os.path.exists", return_value=True)

    loaded_active, loaded_queue = load_state(file_path)
    assert loaded_active == {"123": {"name": "test", "requeued": False}}
    assert loaded_queue == download_queues


def test_load_state_missing_file(mocker):
    mocker.patch("os.path.exists", return_value=False)
    active, queue = load_state("missing.json")
    assert active == {}
    assert queue == {}


def test_load_state_json_error(mocker):
    mocker.patch("os.path.exists", return_value=True)
    mocker.patch("builtins.open", mocker.mock_open(read_data="{}"))
    mocker.patch("json.load", side_effect=json.JSONDecodeError("msg", "doc", 0))
    error_mock = mocker.patch("telegram_bot.state.logger.error")

    active, queue = load_state("bad.json")
    assert active == {}
    assert queue == {}
    error_mock.assert_called()


@pytest.mark.asyncio
async def test_post_init_resumes_persisted_download(mocker):
    application = SimpleNamespace(bot_data={})
    mocker.patch(
        "telegram_bot.state.load_state",
        return_value=({"1": {"name": "movie"}}, {}),
    )
    download_mock = mocker.Mock(return_value="coro")
    mocker.patch(
        "telegram_bot.services.download_manager.download_task_wrapper",
        new=download_mock,
    )
    create_task_mock = mocker.patch("telegram_bot.state.asyncio.create_task")

    await post_init(application)

    download_mock.assert_called_once()
    passed_data, passed_app = download_mock.call_args[0]
    assert passed_data["name"] == "movie"
    assert passed_app is application
    create_task_mock.assert_called_once_with("coro")


@pytest.mark.asyncio
async def test_post_shutdown_cancels_tasks_and_saves_state(mocker):
    task = SimpleNamespace(cancel=mocker.Mock(), done=mocker.Mock(return_value=False))
    application = SimpleNamespace(
        bot_data={
            "active_downloads": {"1": {"task": task}},
            "download_queues": {},
        }
    )
    mocker.patch("telegram_bot.state.asyncio.gather", new=AsyncMock())
    save_mock = mocker.patch("telegram_bot.state.save_state")

    await post_shutdown(application)

    task.cancel.assert_called_once()
    save_mock.assert_called_once()
