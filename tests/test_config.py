import sys
from pathlib import Path

import pytest

from telegram_bot.config import get_configuration, resolve_scraper_max_torrent_size_gib

sys.path.append(str(Path(__file__).resolve().parent.parent))


def test_get_configuration_happy_path(mocker):
    config_data = """
[telegram]
bot_token=TEST_TOKEN
allowed_user_ids=1,2

[host]
default_save_path=/downloads
scraper_max_torrent_size_gib=22

[plex]
plex_url=http://example.com
plex_token=PLEX_TOKEN

[search]
websites=["site1"]
preferences={"category": "movie"}

[tmdb]
access_token=TMDB_TEST_ACCESS_TOKEN
region=ca
"""
    mocker.patch("builtins.open", mocker.mock_open(read_data=config_data))
    mocker.patch("os.path.exists", return_value=True)
    mocker.patch("os.makedirs")

    (
        token,
        paths,
        allowed_ids,
        plex_config,
        search_config,
        tmdb_config,
        runtime_limits,
    ) = get_configuration()

    assert token == "TEST_TOKEN"
    assert paths == {
        "default": "/downloads",
        "movies": "/downloads",
        "tv_shows": "/downloads",
    }
    assert allowed_ids == [1, 2]
    assert plex_config == {"url": "http://example.com", "token": "PLEX_TOKEN"}
    assert search_config == {
        "websites": ["site1"],
        "preferences": {"category": "movie"},
    }
    assert tmdb_config == {"access_token": "TMDB_TEST_ACCESS_TOKEN", "region": "CA"}
    assert runtime_limits == {"scraper_max_torrent_size_gib": 22.0}


def test_resolve_scraper_max_torrent_size_gib_caps_requested_limit():
    bot_data = {"SCRAPER_MAX_TORRENT_SIZE_GIB": 22.0}

    assert resolve_scraper_max_torrent_size_gib(bot_data, None) == 22.0
    assert resolve_scraper_max_torrent_size_gib(bot_data, 10) == 10.0
    assert resolve_scraper_max_torrent_size_gib(bot_data, 44) == 22.0
    assert resolve_scraper_max_torrent_size_gib(bot_data, "invalid") is None
    assert resolve_scraper_max_torrent_size_gib(bot_data, 0) is None


def test_get_configuration_tmdb_api_key_only(mocker):
    config_data = """
[telegram]
bot_token=TEST_TOKEN

[host]
default_save_path=/downloads
scraper_max_torrent_size_gib=22

[tmdb]
api_key=TMDB_TEST_API_KEY
"""
    mocker.patch("builtins.open", mocker.mock_open(read_data=config_data))
    mocker.patch("os.path.exists", return_value=True)
    mocker.patch("os.makedirs")

    _, _, _, _, _, tmdb_config, _ = get_configuration()
    assert tmdb_config == {"api_key": "TMDB_TEST_API_KEY", "region": "US"}


def test_get_configuration_loads_search_providers(mocker):
    config_data = """
[telegram]
bot_token=TEST_TOKEN

[host]
default_save_path=/downloads
scraper_max_torrent_size_gib=22

[search]
providers=[
    {
        "name": "Prowlarr",
        "type": "torznab",
        "enabled": true,
        "search_url": "http://127.0.0.1:9696/1/api?apikey=KEY&t={type}&q={query}&cat={category}"
    }
]
"""
    mocker.patch("builtins.open", mocker.mock_open(read_data=config_data))
    mocker.patch("os.path.exists", return_value=True)
    mocker.patch("os.makedirs")

    _, _, _, _, search_config, _, _ = get_configuration()

    assert search_config["providers"] == [
        {
            "name": "Prowlarr",
            "type": "torznab",
            "enabled": True,
            "search_url": (
                "http://127.0.0.1:9696/1/api?apikey=KEY&t={type}&q={query}&cat={category}"
            ),
        }
    ]


def test_get_configuration_missing_file(mocker):
    mocker.patch("os.path.exists", return_value=False)
    with pytest.raises(SystemExit):
        get_configuration()


def test_get_configuration_missing_token(mocker):
    config_data = """
[telegram]
bot_token=PLACE_TOKEN_HERE

[host]
default_save_path=/downloads
scraper_max_torrent_size_gib=22
"""
    mocker.patch("builtins.open", mocker.mock_open(read_data=config_data))
    mocker.patch("os.path.exists", return_value=True)
    with pytest.raises(SystemExit):
        get_configuration()


def test_get_configuration_missing_default_path(mocker):
    config_data = """
[telegram]
bot_token=TEST_TOKEN
"""
    mocker.patch("builtins.open", mocker.mock_open(read_data=config_data))
    mocker.patch("os.path.exists", return_value=True)
    with pytest.raises(ValueError):
        get_configuration()


def test_get_configuration_missing_scraper_size_limit(mocker):
    config_data = """
[telegram]
bot_token=TEST_TOKEN

[host]
default_save_path=/downloads
"""
    mocker.patch("builtins.open", mocker.mock_open(read_data=config_data))
    mocker.patch("os.path.exists", return_value=True)
    with pytest.raises(ValueError):
        get_configuration()


def test_get_configuration_invalid_search_json(mocker):
    config_data = """
[telegram]
bot_token=TEST_TOKEN

[host]
default_save_path=/downloads
scraper_max_torrent_size_gib=22

[search]
websites={invalid_json}
"""
    mocker.patch("builtins.open", mocker.mock_open(read_data=config_data))
    mocker.patch("os.path.exists", return_value=True)
    with pytest.raises(ValueError):
        get_configuration()
