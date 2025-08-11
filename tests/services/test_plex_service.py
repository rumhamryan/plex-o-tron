from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

import pytest
from types import SimpleNamespace
from plexapi.exceptions import Unauthorized

from telegram_bot.services.plex_service import get_plex_server_status


@pytest.mark.asyncio
async def test_get_plex_server_status_connected(mocker):
    mock_plex = mocker.Mock(version="1.0", platform="Linux")
    mocker.patch("telegram_bot.services.plex_service.PlexServer", return_value=mock_plex)

    context = SimpleNamespace(
        bot_data={"PLEX_CONFIG": {"url": "http://plex", "token": "abc"}}
    )

    result = await get_plex_server_status(context)
    assert "Connected" in result


@pytest.mark.asyncio
async def test_get_plex_server_status_unauthorized(mocker):
    mocker.patch(
        "telegram_bot.services.plex_service.PlexServer",
        side_effect=Unauthorized("bad token"),
    )

    context = SimpleNamespace(
        bot_data={"PLEX_CONFIG": {"url": "http://plex", "token": "abc"}}
    )

    result = await get_plex_server_status(context)
    assert "Authentication Failed" in result


@pytest.mark.asyncio
async def test_get_plex_server_status_connection_error(mocker):
    mocker.patch(
        "telegram_bot.services.plex_service.PlexServer",
        side_effect=Exception("no connection"),
    )

    context = SimpleNamespace(
        bot_data={"PLEX_CONFIG": {"url": "http://plex", "token": "abc"}}
    )

    result = await get_plex_server_status(context)
    assert "Connection Failed" in result

