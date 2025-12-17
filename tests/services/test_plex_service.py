import sys
from pathlib import Path
import pytest
from unittest.mock import Mock
from plexapi.exceptions import Unauthorized
from requests import exceptions as requests_exceptions
from telegram_bot.services.plex_service import (
    get_plex_server_status,
    ensure_collection_contains_movies,
)

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))


@pytest.mark.asyncio
async def test_get_plex_server_status_connected(mocker):
    mock_plex = mocker.Mock(version="1.0", platform="Linux")
    mocker.patch(
        "telegram_bot.services.plex_service.PlexServer", return_value=mock_plex
    )

    context = Mock()
    context.bot_data = {"PLEX_CONFIG": {"url": "http://plex", "token": "abc"}}

    result = await get_plex_server_status(context)
    assert "Connected" in result


@pytest.mark.asyncio
async def test_get_plex_server_status_unauthorized(mocker):
    mocker.patch(
        "telegram_bot.services.plex_service.PlexServer",
        side_effect=Unauthorized("bad token"),
    )

    context = Mock()
    context.bot_data = {"PLEX_CONFIG": {"url": "http://plex", "token": "abc"}}

    result = await get_plex_server_status(context)
    assert "Authentication Failed" in result


@pytest.mark.asyncio
async def test_get_plex_server_status_connection_error(mocker):
    mocker.patch(
        "telegram_bot.services.plex_service.PlexServer",
        side_effect=Exception("no connection"),
    )

    context = Mock()
    context.bot_data = {"PLEX_CONFIG": {"url": "http://plex", "token": "abc"}}

    result = await get_plex_server_status(context)
    assert "Connection Failed" in result


@pytest.mark.asyncio
async def test_get_plex_server_status_placeholder_token():
    context = Mock()
    context.bot_data = {"PLEX_CONFIG": {"url": "http://plex", "token": "PLEX_TOKEN"}}

    result = await get_plex_server_status(context)
    assert "Not configured" in result


@pytest.mark.asyncio
async def test_ensure_collection_contains_movies_adds_items(mocker):
    movie = mocker.Mock()
    movie.title = "Movie One"
    movie.year = 2021
    movie.addCollection = mocker.Mock()

    section = mocker.Mock()
    section.search.side_effect = [[movie]]

    plex = mocker.Mock()
    plex.library.section.return_value = section

    mocker.patch("telegram_bot.services.plex_service.PlexServer", return_value=plex)

    plex_config = {"url": "http://plex", "token": "123"}
    movies = [{"title": "Movie One", "year": 2021}]
    result = await ensure_collection_contains_movies(plex_config, "Saga", movies)

    assert result == ["Movie One (2021)"]
    movie.addCollection.assert_called_once_with("Saga")


@pytest.mark.asyncio
async def test_ensure_collection_contains_movies_suppresses_connection_error(mocker):
    mocker.patch(
        "telegram_bot.services.plex_service.PlexServer",
        side_effect=requests_exceptions.RequestException("offline"),
    )
    plex_config = {"url": "http://plex", "token": "123"}
    movies = [{"title": "Movie One", "year": 2021}]

    result = await ensure_collection_contains_movies(plex_config, "Saga", movies)

    assert result == []


@pytest.mark.asyncio
async def test_ensure_collection_contains_movies_skips_placeholder_token(mocker):
    plex_mock = mocker.patch("telegram_bot.services.plex_service.PlexServer")
    plex_config = {"url": "http://plex", "token": "PLEX_TOKEN"}
    movies = [{"title": "Movie One", "year": 2021}]

    result = await ensure_collection_contains_movies(plex_config, "Saga", movies)

    assert result == []
    plex_mock.assert_not_called()
