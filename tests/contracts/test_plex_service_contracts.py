from telegram_bot.services.plex_service import _has_valid_plex_token


def test_plex_service_contracts_token_check():
    assert _has_valid_plex_token({"token": "PLEX_TOKEN"}) is False
    assert _has_valid_plex_token({"token": ""}) is False
    assert _has_valid_plex_token({"token": "real-token"}) is True
