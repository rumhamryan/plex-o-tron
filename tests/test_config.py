import os
import sys
import configparser

# Ensure the project root is on sys.path to allow importing telegram_bot
PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from telegram_bot.config import _load_plex_config


def test_load_plex_config_ignores_placeholder():
    config = configparser.ConfigParser()
    config.add_section('plex')
    config.set('plex', 'plex_url', 'http://example.com')
    config.set('plex', 'plex_token', 'YOUR_PLEX_TOKEN_HERE')
    assert _load_plex_config(config) == {}
