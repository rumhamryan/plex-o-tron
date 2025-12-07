import pytest
from unittest.mock import AsyncMock

from telegram_bot.services.generic_torrent_scraper import GenericTorrentScraper


@pytest.mark.asyncio
async def test_two_stage_filtering_prefers_precise_single_token_match(mocker):
    site_config = {
        "site_name": "TestSite",
        "base_url": "https://example.com",
        "search_path": "/search/{query}/{category}/{page}/",
        "category_mapping": {"movie": "movies"},
        "results_page_selectors": {
            "result_row": "tr",
            "name": "td.name a",
            "magnet": "td.name a",
            "seeders": "td.seeds",
            "leechers": "td.leeches",
            "size": "td.size",
        },
        "matching": {"fuzz_scorer": "ratio", "fuzz_threshold": 40},
    }

    search_html = (
        "<table>"
        "<tr>"
        '<td class="name"><a href="magnet:?xt=1">Dune Part Two 2024 1080p</a></td>'
        '<td class="seeds">10</td><td class="leeches">1</td><td class="size">1 GB</td>'
        "</tr>"
        "<tr>"
        '<td class="name"><a href="magnet:?xt=2">Dune Part Two 2024 720p</a></td>'
        '<td class="seeds">8</td><td class="leeches">1</td><td class="size">900 MB</td>'
        "</tr>"
        "<tr>"
        '<td class="name"><a href="magnet:?xt=3">Dune 1984 1080p</a></td>'
        '<td class="seeds">5</td><td class="leeches">1</td><td class="size">1.2 GB</td>'
        "</tr>"
        "</table>"
    )

    fetch_mock = AsyncMock(return_value=search_html)
    mocker.patch.object(GenericTorrentScraper, "_fetch_page", fetch_mock)

    scraper = GenericTorrentScraper(site_config)
    results = await scraper.search("Dune", "movie")

    # With the updated precision logic, a single-token query like "Dune"
    # prefers exact token-equivalent titles over broader ones like
    # "Dune Part Two". Therefore, only the base title match remains.
    assert len(results) == 1
    assert "Dune 1984" in results[0]['name']
