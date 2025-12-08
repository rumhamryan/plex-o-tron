from unittest.mock import AsyncMock

import pytest

from telegram_bot.services.generic_torrent_scraper import GenericTorrentScraper


@pytest.mark.asyncio
async def test_search_parses_magnet_link_from_detail_page(mocker):
    """Ensure magnet links are extracted when only available on detail pages."""

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
        "details_page_selectors": {"magnet_url": "a[href^='magnet:']"},
    }

    search_html = (
        "<table><tr>"
        '<td class="name"><a href="/torrent/1">Example</a></td>'
        '<td class="seeds">10</td>'
        '<td class="leeches">1</td>'
        '<td class="size">1 GB</td>'
        "</tr></table>"
    )
    detail_html = (
        "<html><body>"
        '<a href="magnet:?xt=urn:btih:abcdef&dn=Example">Magnet</a>'
        "</body></html>"
    )

    fetch_mock = AsyncMock(side_effect=[search_html, detail_html])
    mocker.patch.object(GenericTorrentScraper, "_fetch_page", fetch_mock)

    scraper = GenericTorrentScraper(site_config)
    results = await scraper.search("Example", "movie")

    assert len(results) == 1
    assert results[0].magnet_url.startswith("magnet:?xt=urn:btih:abcdef")
