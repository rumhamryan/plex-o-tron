import asyncio
import time

import pytest
from unittest.mock import AsyncMock

from telegram_bot.services.generic_torrent_scraper import GenericTorrentScraper


@pytest.mark.asyncio
async def test_search_fetches_detail_pages_concurrently(mocker):
    """Detail pages should be fetched in parallel using one HTTP client."""
    site_config = {
        "site_name": "TestSite",
        "base_url": "https://example.com",
        "search_path": "/search/{query}/{category}/{page}/",
        "category_mapping": {"movie": "movies"},
        "results_page_selectors": {
            "rows": "tr",
            "name": "td.name a",
            "details_page_link": "td.name a",
            "seeds": "td.seeds",
            "leechers": "td.leeches",
            "size": "td.size",
        },
        "details_page_selectors": {"magnet_url": "a[href^='magnet:']"},
    }

    search_html = (
        "<table>"
        "<tr>"
        '<td class="name"><a href="/torrent/1">Example</a></td>'
        '<td class="seeds">10</td>'
        '<td class="leeches">1</td>'
        '<td class="size">1 GB</td>'
        "</tr>"
        "<tr>"
        '<td class="name"><a href="/torrent/2">Example</a></td>'
        '<td class="seeds">5</td>'
        '<td class="leeches">1</td>'
        '<td class="size">1 GB</td>'
        "</tr>"
        "</table>"
    )

    detail_template = "<a href='magnet:?xt=urn:btih:{id}'>Magnet</a>"
    clients_seen: set[int] = set()

    async def fetch_side_effect(url: str, client=None):
        clients_seen.add(id(client))
        if url.endswith("/search/Example/movies/1/"):
            return search_html
        await asyncio.sleep(0.1)
        torrent_id = url.rsplit("/", 1)[-1]
        return detail_template.format(id=torrent_id)

    fetch_mock = AsyncMock(side_effect=fetch_side_effect)
    mocker.patch.object(GenericTorrentScraper, "_fetch_page", fetch_mock)

    scraper = GenericTorrentScraper(site_config)

    start = time.perf_counter()
    results = await scraper.search("Example", "movie")
    elapsed = time.perf_counter() - start

    assert len(results) == 2
    # Two detail pages each sleep for 0.1s; with concurrency the total should be <0.19s
    assert elapsed < 0.19
    # All fetches should reuse the same HTTP client instance
    assert len(clients_seen) == 1
