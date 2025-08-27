from bs4 import BeautifulSoup

from telegram_bot.services.generic_torrent_scraper import GenericTorrentScraper


def _build_scraper() -> GenericTorrentScraper:
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
            "uploader": "td.uploader a",
        },
    }
    return GenericTorrentScraper(site_config)


def test_extract_data_from_row() -> None:
    scraper = _build_scraper()
    row_html = (
        "<tr>"
        '<td class="name"><a href="magnet:?xt=1">Example</a></td>'
        '<td class="seeds">5</td>'
        '<td class="leeches">2</td>'
        '<td class="size">1 GB</td>'
        '<td class="uploader"><a>Uploader</a></td>'
        "</tr>"
    )
    row = BeautifulSoup(row_html, "lxml").select_one("tr")

    data = scraper._extract_data_from_row(row)  # type: ignore[arg-type]
    assert data is not None
    assert data.name == "Example"
    assert data.seeders == 5
    assert data.uploader == "Uploader"

    malformed = BeautifulSoup("<tr><td></td></tr>", "lxml").select_one("tr")
    assert scraper._extract_data_from_row(malformed) is None  # type: ignore[arg-type]


def test_parse_and_select_top_results() -> None:
    scraper = _build_scraper()
    html = (
        "<tbody>"
        "<tr>"
        '<td class="name"><a href="magnet:?a">A</a></td>'
        '<td class="seeds">1</td><td class="leeches">0</td><td class="size">1 GB</td>'
        '<td class="uploader"><a>U</a></td>'
        "</tr>"
        "<tr>"
        '<td class="name"><a href="magnet:?b">B</a></td>'
        '<td class="seeds">5</td><td class="leeches">0</td><td class="size">1 GB</td>'
        '<td class="uploader"><a>U</a></td>'
        "</tr>"
        "<tr>"
        '<td class="name"><a href="magnet:?c">C</a></td>'
        '<td class="seeds">3</td><td class="leeches">0</td><td class="size">1 GB</td>'
        '<td class="uploader"><a>U</a></td>'
        "</tr>"
        "</tbody>"
    )
    search_area = BeautifulSoup(html, "lxml")

    top_results = scraper._parse_and_select_top_results(search_area, limit=2)
    assert len(top_results) == 2
    assert [r.name for r in top_results] == ["B", "C"]
    assert top_results[0].seeders == 5
    assert top_results[1].seeders == 3
