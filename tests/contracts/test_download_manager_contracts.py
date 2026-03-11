from telegram_bot.utils import sanitize_collection_name


def test_download_manager_contracts_sanitize_collection_name():
    assert sanitize_collection_name("Movie (2024)") == "Movie (2024)"
    assert sanitize_collection_name("Movie: 2024") == "Movie 2024"
