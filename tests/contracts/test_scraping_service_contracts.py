from telegram_bot.services.scraping_service import _coerce_swarm_counts


def test_scraping_service_contracts():
    result = _coerce_swarm_counts({"seeders": "10", "leechers": "2"})
    assert result["seeders"] == 10
    assert result["leechers"] == 2

    result = _coerce_swarm_counts({"seeders": None})
    assert result["seeders"] == 0
    assert result["leechers"] == 0
