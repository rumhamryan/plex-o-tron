import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from unittest.mock import Mock
from plexapi.exceptions import Unauthorized
from requests import exceptions as requests_exceptions
from telegram_bot.services.plex_service import (
    get_plex_server_status,
    ensure_collection_contains_movies,
    get_existing_episodes_for_season,
    wait_for_movies_to_be_available,
)

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))


@pytest.mark.asyncio
async def test_get_plex_server_status_connected(mocker):
    mock_plex = mocker.Mock(version="1.0", platform="Linux")
    mocker.patch("telegram_bot.services.plex_service.create_plex_client", return_value=mock_plex)

    context = Mock()
    context.bot_data = {"PLEX_CONFIG": {"url": "http://plex", "token": "abc"}}

    result = await get_plex_server_status(context)
    assert "Connected" in result


@pytest.mark.asyncio
async def test_get_plex_server_status_unauthorized(mocker):
    mocker.patch(
        "telegram_bot.services.plex_service.create_plex_client",
        side_effect=Unauthorized("bad token"),
    )

    context = Mock()
    context.bot_data = {"PLEX_CONFIG": {"url": "http://plex", "token": "abc"}}

    result = await get_plex_server_status(context)
    assert "Authentication Failed" in result


@pytest.mark.asyncio
async def test_get_plex_server_status_connection_error(mocker):
    mocker.patch(
        "telegram_bot.services.plex_service.create_plex_client",
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
    section.collection.side_effect = Exception("not found")
    section.collections.return_value = []

    plex = mocker.Mock()
    plex.library.section.return_value = section

    mocker.patch("telegram_bot.services.plex_service.create_plex_client", return_value=plex)

    plex_config = {"url": "http://plex", "token": "123"}
    movies = [{"title": "Movie One", "year": 2021}]
    result = await ensure_collection_contains_movies(plex_config, "Saga", movies)

    assert result == ["Movie One (2021)"]
    movie.addCollection.assert_called_once_with("Saga")


@pytest.mark.asyncio
async def test_ensure_collection_contains_movies_renames_existing_collection_alias(mocker):
    movie = mocker.Mock()
    movie.title = "Mission: Impossible"
    movie.year = 1996
    movie.addCollection = mocker.Mock()

    existing_collection = mocker.Mock()
    existing_collection.title = "Mission: Impossible"
    existing_collection.editTitle = mocker.Mock()

    section = mocker.Mock()
    section.search.return_value = [movie]
    section.collection.side_effect = Exception("not found")
    section.collections.return_value = [existing_collection]

    plex = mocker.Mock()
    plex.library.section.return_value = section

    mocker.patch("telegram_bot.services.plex_service.create_plex_client", return_value=plex)

    plex_config = {"url": "http://plex", "token": "123"}
    movies = [{"title": "Mission: Impossible", "year": 1996}]
    result = await ensure_collection_contains_movies(plex_config, "Mission Impossible", movies)

    assert result == ["Mission: Impossible (1996)"]
    existing_collection.editTitle.assert_called_once_with("Mission Impossible")
    movie.addCollection.assert_called_once_with("Mission Impossible")


@pytest.mark.asyncio
async def test_ensure_collection_contains_movies_falls_back_when_collection_rename_fails(mocker):
    movie = mocker.Mock()
    movie.title = "Mission: Impossible"
    movie.year = 1996
    movie.addCollection = mocker.Mock()

    existing_collection = mocker.Mock()
    existing_collection.title = "Mission: Impossible"
    existing_collection.editTitle.side_effect = RuntimeError("rename failed")

    section = mocker.Mock()
    section.search.return_value = [movie]
    section.collection.side_effect = Exception("not found")
    section.collections.return_value = [existing_collection]

    plex = mocker.Mock()
    plex.library.section.return_value = section

    mocker.patch("telegram_bot.services.plex_service.create_plex_client", return_value=plex)

    plex_config = {"url": "http://plex", "token": "123"}
    movies = [{"title": "Mission: Impossible", "year": 1996}]
    result = await ensure_collection_contains_movies(plex_config, "Mission Impossible", movies)

    assert result == ["Mission: Impossible (1996)"]
    movie.addCollection.assert_called_once_with("Mission: Impossible")


@pytest.mark.asyncio
async def test_ensure_collection_contains_movies_uses_existing_requested_collection_on_conflict(
    mocker,
):
    movie = mocker.Mock()
    movie.title = "Mission: Impossible"
    movie.year = 1996
    movie.addCollection = mocker.Mock()

    canonical_collection = mocker.Mock()
    canonical_collection.title = "Mission Impossible"
    alias_collection = mocker.Mock()
    alias_collection.title = "Mission: Impossible"
    alias_collection.editTitle = mocker.Mock()

    section = mocker.Mock()
    section.search.return_value = [movie]
    section.collection.side_effect = Exception("not found")
    section.collections.return_value = [canonical_collection, alias_collection]

    plex = mocker.Mock()
    plex.library.section.return_value = section

    mocker.patch("telegram_bot.services.plex_service.create_plex_client", return_value=plex)

    plex_config = {"url": "http://plex", "token": "123"}
    movies = [{"title": "Mission: Impossible", "year": 1996}]
    result = await ensure_collection_contains_movies(plex_config, "Mission Impossible", movies)

    assert result == ["Mission: Impossible (1996)"]
    alias_collection.editTitle.assert_not_called()
    movie.addCollection.assert_called_once_with("Mission Impossible")


@pytest.mark.asyncio
async def test_ensure_collection_contains_movies_matches_variant_titles_from_year_search(mocker):
    movie = mocker.Mock()
    movie.title = "Mission: Impossible II"
    movie.year = 2000
    movie.addCollection = mocker.Mock()

    section = mocker.Mock()
    section.search.side_effect = [[], [], [movie]]
    section.collection.side_effect = Exception("not found")
    section.collections.return_value = []

    plex = mocker.Mock()
    plex.library.section.return_value = section

    mocker.patch("telegram_bot.services.plex_service.create_plex_client", return_value=plex)

    plex_config = {"url": "http://plex", "token": "123"}
    movies = [{"title": "Mission: Impossible 2", "year": 2000}]
    result = await ensure_collection_contains_movies(plex_config, "Mission Impossible", movies)

    assert result == ["Mission: Impossible II (2000)"]
    movie.addCollection.assert_called_once_with("Mission Impossible")


@pytest.mark.asyncio
async def test_ensure_collection_contains_movies_uses_path_fallback_when_title_search_misses(
    mocker,
):
    movie = mocker.Mock()
    movie.title = "Mission: Impossible - Dead Reckoning Part One"
    movie.year = 2023
    movie.addCollection = mocker.Mock()
    part = mocker.Mock(
        file="/mnt/movies/Mission Impossible/07 - Mission Impossible - Dead Reckoning Part One (2023).mp4"
    )
    movie.media = [mocker.Mock(parts=[part])]

    section = mocker.Mock()
    section.search.side_effect = [[], [], []]
    section.all.return_value = [movie]
    section.collection.side_effect = Exception("not found")
    section.collections.return_value = []

    plex = mocker.Mock()
    plex.library.section.return_value = section

    mocker.patch("telegram_bot.services.plex_service.create_plex_client", return_value=plex)

    plex_config = {"url": "http://plex", "token": "123"}
    movies = [
        {
            "title": "Dead Reckoning Part One",
            "year": 2023,
            "destination_path": "/mnt/movies/Mission Impossible/07 - Mission Impossible - Dead Reckoning Part One (2023).mp4",
        }
    ]
    result = await ensure_collection_contains_movies(plex_config, "Mission Impossible", movies)

    assert result == ["Mission: Impossible - Dead Reckoning Part One (2023)"]
    movie.addCollection.assert_called_once_with("Mission Impossible")
    section.all.assert_called_once()


@pytest.mark.asyncio
async def test_ensure_collection_contains_movies_suppresses_connection_error(mocker):
    mocker.patch(
        "telegram_bot.services.plex_service.create_plex_client",
        side_effect=requests_exceptions.RequestException("offline"),
    )
    plex_config = {"url": "http://plex", "token": "123"}
    movies = [{"title": "Movie One", "year": 2021}]

    result = await ensure_collection_contains_movies(plex_config, "Saga", movies)

    assert result == []


@pytest.mark.asyncio
async def test_ensure_collection_contains_movies_skips_placeholder_token(mocker):
    plex_mock = mocker.patch("telegram_bot.services.plex_service.create_plex_client")
    plex_config = {"url": "http://plex", "token": "PLEX_TOKEN"}
    movies = [{"title": "Movie One", "year": 2021}]

    result = await ensure_collection_contains_movies(plex_config, "Saga", movies)

    assert result == []
    plex_mock.assert_not_called()


@pytest.mark.asyncio
async def test_wait_for_movies_to_be_available_returns_early_when_movies_found(mocker):
    movie = mocker.Mock()
    movie.title = "Movie One"
    movie.year = 2021

    section = mocker.Mock()
    section.search.side_effect = [[], [movie]]

    plex = mocker.Mock()
    plex.library.section.return_value = section

    mocker.patch("telegram_bot.services.plex_service.create_plex_client", return_value=plex)
    current_time = [0.0]

    async def fake_sleep(seconds):
        current_time[0] += seconds

    mocker.patch(
        "telegram_bot.services.plex_service.asyncio.get_running_loop",
        return_value=SimpleNamespace(time=lambda: current_time[0]),
    )
    sleep_mock = mocker.patch("asyncio.sleep", mocker.AsyncMock(side_effect=fake_sleep))

    plex_config = {"url": "http://plex", "token": "123"}
    result = await wait_for_movies_to_be_available(
        plex_config,
        [{"title": "Movie One", "year": 2021}],
        timeout_seconds=30,
        poll_interval_seconds=5,
    )

    assert result is True
    sleep_mock.assert_not_awaited()
    assert section.search.call_count == 3


@pytest.mark.asyncio
async def test_wait_for_movies_to_be_available_accepts_variant_year_match(mocker):
    movie = mocker.Mock()
    movie.title = "Mission: Impossible - Dead Reckoning Part One"
    movie.year = 2023

    section = mocker.Mock()
    section.search.side_effect = [[], [], [movie]]

    plex = mocker.Mock()
    plex.library.section.return_value = section

    mocker.patch("telegram_bot.services.plex_service.create_plex_client", return_value=plex)
    current_time = [0.0]

    async def fake_sleep(seconds):
        current_time[0] += seconds

    mocker.patch(
        "telegram_bot.services.plex_service.asyncio.get_running_loop",
        return_value=SimpleNamespace(time=lambda: current_time[0]),
    )
    sleep_mock = mocker.patch("asyncio.sleep", mocker.AsyncMock(side_effect=fake_sleep))

    plex_config = {"url": "http://plex", "token": "123"}
    result = await wait_for_movies_to_be_available(
        plex_config,
        [{"title": "Dead Reckoning Part One", "year": 2023}],
        timeout_seconds=30,
        poll_interval_seconds=5,
    )

    assert result is True
    sleep_mock.assert_not_awaited()
    assert section.search.call_count == 3


@pytest.mark.asyncio
async def test_wait_for_movies_to_be_available_uses_path_fallback(mocker):
    movie = mocker.Mock()
    movie.title = "Mission: Impossible - Dead Reckoning Part One"
    movie.year = 2023
    part = mocker.Mock(
        file="/mnt/movies/Mission Impossible/07 - Mission Impossible - Dead Reckoning Part One (2023).mp4"
    )
    movie.media = [mocker.Mock(parts=[part])]

    section = mocker.Mock()
    section.search.side_effect = [[], [], []]
    section.all.return_value = [movie]

    plex = mocker.Mock()
    plex.library.section.return_value = section

    mocker.patch("telegram_bot.services.plex_service.create_plex_client", return_value=plex)
    current_time = [0.0]

    async def fake_sleep(seconds):
        current_time[0] += seconds

    mocker.patch(
        "telegram_bot.services.plex_service.asyncio.get_running_loop",
        return_value=SimpleNamespace(time=lambda: current_time[0]),
    )
    sleep_mock = mocker.patch("asyncio.sleep", mocker.AsyncMock(side_effect=fake_sleep))

    plex_config = {"url": "http://plex", "token": "123"}
    result = await wait_for_movies_to_be_available(
        plex_config,
        [
            {
                "title": "Dead Reckoning Part One",
                "year": 2023,
                "destination_path": "/mnt/movies/Mission Impossible/07 - Mission Impossible - Dead Reckoning Part One (2023).mp4",
            }
        ],
        timeout_seconds=30,
        poll_interval_seconds=5,
    )

    assert result is True
    sleep_mock.assert_not_awaited()
    section.all.assert_called_once()


@pytest.mark.asyncio
async def test_wait_for_movies_to_be_available_times_out(mocker):
    section = mocker.Mock()
    section.search.return_value = []

    plex = mocker.Mock()
    plex.library.section.return_value = section

    mocker.patch("telegram_bot.services.plex_service.create_plex_client", return_value=plex)
    current_time = [0.0]

    async def fake_sleep(seconds):
        current_time[0] += seconds

    mocker.patch(
        "telegram_bot.services.plex_service.asyncio.get_running_loop",
        return_value=SimpleNamespace(time=lambda: current_time[0]),
    )
    sleep_mock = mocker.patch("asyncio.sleep", mocker.AsyncMock(side_effect=fake_sleep))

    plex_config = {"url": "http://plex", "token": "123"}
    result = await wait_for_movies_to_be_available(
        plex_config,
        [{"title": "Movie One", "year": 2021}],
        timeout_seconds=1,
        poll_interval_seconds=5,
    )

    assert result is False
    sleep_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_existing_episodes_for_season_supports_suffix_season_directory(tmp_path):
    season_dir = tmp_path / "Rick and Morty" / "Season 01 (2013-2014)"
    season_dir.mkdir(parents=True)
    (season_dir / "s01e01 - Pilot.mp4").write_text("data", encoding="utf-8")
    (season_dir / "s01e02 - Lawnmower Dog.mp4").write_text("data", encoding="utf-8")
    (season_dir / "notes.txt").write_text("ignore", encoding="utf-8")

    context = Mock()
    context.bot_data = {
        "SAVE_PATHS": {
            "tv_shows": str(tmp_path),
            "default": str(tmp_path),
        }
    }

    existing = await get_existing_episodes_for_season(
        context,
        show_title="Rick and Morty",
        season=1,
    )

    assert existing == {1, 2}


@pytest.mark.asyncio
async def test_get_existing_episodes_for_season_supports_root_episode_files_and_show_name_variants(
    tmp_path,
):
    show_dir = tmp_path / "Daredevil - Born Again"
    show_dir.mkdir(parents=True)
    (show_dir / "s1e01 - Heavens Half Hour.mkv").write_text("data", encoding="utf-8")
    (show_dir / "s01e02 - Optics.mkv").write_text("data", encoding="utf-8")
    (show_dir / "s02e01 - Wrong Season.mkv").write_text("data", encoding="utf-8")
    (show_dir / "README.txt").write_text("ignore", encoding="utf-8")

    context = Mock()
    context.bot_data = {
        "SAVE_PATHS": {
            "tv_shows": str(tmp_path),
            "default": str(tmp_path),
        }
    }

    existing = await get_existing_episodes_for_season(
        context,
        show_title="Daredevil: Born Again",
        season=1,
    )

    assert existing == {1, 2}


@pytest.mark.asyncio
async def test_get_existing_episodes_for_season_avoids_parent_series_false_match(tmp_path):
    old_show_season = tmp_path / "Daredevil" / "Season 01"
    old_show_season.mkdir(parents=True)
    (old_show_season / "s01e01 - Into the Ring.mkv").write_text("data", encoding="utf-8")
    (old_show_season / "s01e02 - Cut Man.mkv").write_text("data", encoding="utf-8")

    context = Mock()
    context.bot_data = {
        "SAVE_PATHS": {
            "tv_shows": str(tmp_path),
            "default": str(tmp_path),
        }
    }

    existing = await get_existing_episodes_for_season(
        context,
        show_title="Daredevil: Born Again",
        season=1,
    )

    assert existing == set()
