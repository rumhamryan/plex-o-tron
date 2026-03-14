from telegram_bot.services.search_logic import _parse_size_to_gib


def test_search_logic_contracts():
    assert _parse_size_to_gib("1 GB") == 1.0
    assert _parse_size_to_gib("1024 MB") == 1.0
    assert _parse_size_to_gib("") == 0.0
