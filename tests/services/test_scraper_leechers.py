import pytest
import urllib.parse
from unittest.mock import AsyncMock
from bs4 import BeautifulSoup
from telegram_bot.services.scraping_service import _coerce_swarm_counts
from telegram_bot.services.generic_torrent_scraper import (
    GenericTorrentScraper,
    TorrentData,
)


def test_coerce_swarm_counts_valid():
    data = {"title": "Test", "seeders": 10, "leechers": 5}
    result = _coerce_swarm_counts(data)
    assert result["seeders"] == 10
    assert result["leechers"] == 5


def test_coerce_swarm_counts_missing_leechers():
    data = {"title": "Test", "seeders": 10}
    result = _coerce_swarm_counts(data)
    assert result["seeders"] == 10
    assert result["leechers"] == 0
    assert "leechers" in result


def test_coerce_swarm_counts_strings():
    data = {"title": "Test", "seeders": "10", "leechers": "5"}
    result = _coerce_swarm_counts(data)
    assert result["seeders"] == 10
    assert result["leechers"] == 5


def test_coerce_swarm_counts_negative():
    data = {"title": "Test", "seeders": -5, "leechers": -1}
    result = _coerce_swarm_counts(data)
    assert result["seeders"] == 0
    assert result["leechers"] == 0


def test_coerce_swarm_counts_garbage():
    data = {"title": "Test", "seeders": "abc", "leechers": None}
    result = _coerce_swarm_counts(data)
    assert result["seeders"] == 0
    assert result["leechers"] == 0


@pytest.mark.asyncio
async def test_generic_scraper_fetches_details_for_leechers():
    # Setup config with leecher selector in details
    config = {
        "site_name": "TestSite",
        "base_url": "https://example.com",
        "search_path": "/search/{query}",
        "category_mapping": {"movie": "movies"},
        "results_page_selectors": {
            "results_container": "table",
            "result_row": "tr",
            "name": "a.title",
            "magnet": "a.magnet",
            "seeders": "td.seed",
            # No leechers in results
            "leechers": None,
            "size": "td.size",
            "uploader": "td.uploader",
        },
        "details_page_selectors": {
            "magnet_url": "a.detail-magnet",
            "leechers": "span.leechers-count",
        },
        "matching": {},
    }

    scraper = GenericTorrentScraper(config)

    # Mock item with 0 leechers and a details link
    item = TorrentData(
        name="Test Movie",
        magnet_url="magnet:?...",  # Magnet exists, but leechers missing
        seeders=10,
        leechers=0,
        details_link="/details/123",
    )

    # Mock _fetch_page to return details page HTML
    scraper._fetch_page = AsyncMock(
        return_value='<html><body><span class="leechers-count">42</span></body></html>'
    )

    await scraper._resolve_magnets([item])
    assert item.leechers == 42
    scraper._fetch_page.assert_called_once()


@pytest.mark.asyncio
async def test_generic_scraper_populates_info_url():
    # Setup config
    config = {
        "site_name": "TestSite",
        "base_url": "https://example.com",
        "search_path": "/search/{query}",
        "category_mapping": {"movie": "movies"},
        "results_page_selectors": {
            "results_container": "table",
            "result_row": "tr",
            "name": "a.title",
            "magnet": "a.magnet",  # Assume this points to detail page
            "seeders": "td.seed",
            "leechers": "td.leech",
            "size": "td.size",
            "uploader": "td.uploader",
        },
        "details_page_selectors": {
            "magnet_url": "a.detail-magnet",
        },
        "matching": {},
    }

    scraper = GenericTorrentScraper(config)

    # Mock row
    html = """
    <table>
        <tr>
            <td><a class="title">Test Movie</a></td>
            <td><a class="magnet" href="/torrent/123">Link</a></td>
            <td class="seed">10</td>
            <td class="leech">5</td>
            <td class="size">1.0 GB</td>
            <td class="uploader">User</td>
        </tr>
    </table>
    """

    soup = BeautifulSoup(html, "lxml")
    row = soup.find("tr")
    assert row is not None

    item = scraper._extract_data_from_row(row)
    assert item is not None
    assert item.details_link == "/torrent/123"

    # Verify absolute URL construction
    info_url = urllib.parse.urljoin(scraper.base_url, item.details_link)
    assert info_url == "https://example.com/torrent/123"
