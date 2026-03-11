import yaml
from pathlib import Path

from telegram_bot.services import generic_torrent_scraper


def test_load_site_config_uses_cache(tmp_path, monkeypatch):
    """Loading the same config twice should hit the cache after the first read."""
    config_path = tmp_path / "site.yaml"
    config_data = {
        "site_name": "TestSite",
        "base_url": "https://example.com",
        "search_path": "/{query}",
        "category_mapping": {"movie": "Movies"},
        "results_page_selectors": {"rows": "tr"},
    }
    config_path.write_text(yaml.safe_dump(config_data), encoding="utf-8")

    # Ensure cache is empty before test
    generic_torrent_scraper._config_cache.clear()

    original_open = Path.open
    call_count = {"count": 0}

    def counting_open(self, *args, **kwargs):
        call_count["count"] += 1
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)

    first = generic_torrent_scraper.load_site_config(config_path)
    second = generic_torrent_scraper.load_site_config(config_path)

    assert first is second
    assert call_count["count"] == 1
