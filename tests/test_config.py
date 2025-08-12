import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

import pytest

from telegram_bot.config import get_configuration


def test_get_configuration_happy_path(mocker):
    config_data = """
[telegram]
bot_token=TEST_TOKEN
allowed_user_ids=1,2

[host]
default_save_path=/downloads

[plex]
plex_url=http://example.com
plex_token=PLEX_TOKEN

[search]
websites=["site1"]
preferences={"category": "movie"}
"""
    mocker.patch("builtins.open", mocker.mock_open(read_data=config_data))
    mocker.patch("os.path.exists", return_value=True)
    mocker.patch("os.makedirs")

    token, paths, allowed_ids, plex_config, search_config = get_configuration()

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


def test_get_configuration_invalid_search_json(mocker):
    config_data = """
[telegram]
bot_token=TEST_TOKEN

[host]
default_save_path=/downloads

[search]
websites={invalid_json}
"""
    mocker.patch("builtins.open", mocker.mock_open(read_data=config_data))
    mocker.patch("os.path.exists", return_value=True)
    with pytest.raises(ValueError):
        get_configuration()
