import sys
from pathlib import Path
import pytest
import wikipedia
from telegram_bot.services.scrapers import wikipedia_scraper

sys.path.append(str(Path(__file__).resolve().parent.parent.parent.parent))


# Ensure per-test isolation by clearing any module-level caches used by the
# wikipedia_scraper. This prevents earlier tests from influencing later ones.
@pytest.fixture(autouse=True)
def _clear_wiki_caches():
    try:
        wikipedia_scraper._WIKI_TITLES_CACHE.clear()  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        wikipedia_scraper._WIKI_SOUP_CACHE.clear()  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        wikipedia_scraper._WIKI_MOVIE_CACHE.clear()  # type: ignore[attr-defined]
    except Exception:
        pass


DEDICATED_HTML = """
<table class="wikitable">
<tr><td><a title="Show season 1">Season 1</a></td></tr>
</table>
<h3>Season 1</h3>
<table class="wikitable">
<tr><th>No.</th><th>No. in season</th><th>Title</th></tr>
<tr><td>1</td><td>1</td><td>"Pilot"</td></tr>
</table>
"""

EMBEDDED_HTML = """
<table class="wikitable">
<tr><th>Info</th><th>Title</th></tr>
<tr><td>1 1</td><td>"Pilot"</td></tr>
</table>
"""

SIMPLE_EMBEDDED_HTML = """
<h3>Episodes</h3>
<table class="wikitable">
<tr><th>No.</th><th>Title</th></tr>
<tr><td>1</td><td>"Pilot"</td></tr>
</table>
"""

NO_EPISODE_HTML = """
<table class="wikitable">
<tr><th>Info</th><th>Title</th></tr>
<tr><td>2 5</td><td>"Other"</td></tr>
</table>
"""

SEASON_OVERVIEW_HTML = """
<table class="wikitable">
<tr><th>Season</th><th>Episodes</th></tr>
<tr><td>1</td><td>10</td></tr>
<tr><td>2</td><td>8</td></tr>
</table>
"""

DEDICATED_WITH_OVERVIEW_ONGOING_HTML = """
<h3>Season 27</h3>
<table class="wikitable">
<tr><th>No. in season</th><th>Title</th></tr>
<tr><td>1</td><td><i>Ep1</i></td></tr>
<tr><td>2</td><td><i>Ep2</i></td></tr>
<tr><td>3</td><td><i>Ep3</i></td></tr>
<tr><td>4</td><td><i>Ep4</i></td></tr>
</table>

<h2>Series overview</h2>
<table class="wikitable">
<tr><th>Season</th><th>Episodes</th><th>Last aired</th></tr>
<tr><td>27</td><td>10</td><td>present</td></tr>
</table>
"""

OVERVIEW_ONGOING_ONLY_HTML = """
<h2>Series overview</h2>
<table class="wikitable">
<tr><th>Season</th><th>Episodes</th><th>Originally aired</th></tr>
<tr><td>27</td><td>10</td><td>2024–present</td></tr>
</table>
"""


@pytest.mark.asyncio
async def test_fetch_episode_title_dedicated_page(mocker):
    mock_page = mocker.Mock()
    mock_page.title = "Show"
    mock_page.url = "http://example.com"
    mocker.patch("wikipedia.search", return_value=["Show"])
    mocker.patch("wikipedia.page", return_value=mock_page)
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia_scraper._get_page_html",
        return_value=DEDICATED_HTML,
    )

    title, corrected = await wikipedia_scraper.fetch_episode_title_from_wikipedia(
        "Show", 1, 1
    )
    assert title == "Pilot"
    assert corrected is None


@pytest.mark.asyncio
async def test_fetch_episode_title_strips_miniseries_suffix(mocker):
    mock_main_page = mocker.Mock()
    mock_main_page.title = "Show (miniseries)"
    mock_main_page.url = "http://example.com/show"

    mock_list_page = mocker.Mock()
    mock_list_page.url = "http://example.com/list"

    mocker.patch("wikipedia.search", return_value=["Show (miniseries)"])
    page_patch = mocker.patch(
        "wikipedia.page", side_effect=[mock_main_page, mock_list_page]
    )
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia_scraper._get_page_html",
        return_value=DEDICATED_HTML,
    )

    title, corrected = await wikipedia_scraper.fetch_episode_title_from_wikipedia(
        "Show", 1, 1
    )

    assert title == "Pilot"
    assert corrected is None
    assert page_patch.call_args_list[1].args[0] == "List of Show episodes"


@pytest.mark.asyncio
async def test_fetch_episode_title_strips_tv_series_suffix(mocker):
    mock_main_page = mocker.Mock()
    mock_main_page.title = "Show (TV series)"
    mock_main_page.url = "http://example.com/show"

    mock_list_page = mocker.Mock()
    mock_list_page.url = "http://example.com/list"

    mocker.patch("wikipedia.search", return_value=["Show (TV series)"])
    page_patch = mocker.patch(
        "wikipedia.page", side_effect=[mock_main_page, mock_list_page]
    )
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia_scraper._get_page_html",
        return_value=DEDICATED_HTML,
    )

    title, corrected = await wikipedia_scraper.fetch_episode_title_from_wikipedia(
        "Show", 1, 1
    )

    assert title == "Pilot"
    assert corrected is None
    assert page_patch.call_args_list[1].args[0] == "List of Show episodes"


@pytest.mark.asyncio
async def test_fetch_episode_title_embedded_page(mocker):
    mock_main_page = mocker.Mock()
    mock_main_page.title = "Show"
    mock_main_page.url = "http://example.com/main"

    mocker.patch("wikipedia.search", return_value=["Show"])
    mocker.patch(
        "wikipedia.page",
        side_effect=[mock_main_page, wikipedia.exceptions.PageError("no list")],
    )
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia_scraper._get_page_html",
        return_value=SIMPLE_EMBEDDED_HTML,
    )

    title, _ = await wikipedia_scraper.fetch_episode_title_from_wikipedia("Show", 1, 1)
    assert title == "Pilot"


@pytest.mark.asyncio
async def test_fetch_episode_title_not_found(mocker):
    mock_page = mocker.Mock()
    mock_page.url = "http://example.com"
    mocker.patch("wikipedia.search", return_value=["Show"])
    mocker.patch("wikipedia.page", return_value=mock_page)
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia_scraper._get_page_html",
        return_value=NO_EPISODE_HTML,
    )

    title, _ = await wikipedia_scraper.fetch_episode_title_from_wikipedia("Show", 1, 1)
    assert title is None


@pytest.mark.asyncio
async def test_fetch_season_episode_count(mocker):
    mock_page = mocker.Mock()
    mock_page.url = "http://example.com"
    mocker.patch("wikipedia.page", return_value=mock_page)
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia_scraper._get_page_html",
        return_value=SEASON_OVERVIEW_HTML,
    )

    count = await wikipedia_scraper.fetch_season_episode_count_from_wikipedia("Show", 2)
    assert count == 8


@pytest.mark.asyncio
async def test_fetch_season_episode_count_prefers_titles_over_overview(mocker):
    mock_page = mocker.Mock()
    mock_page.url = "http://example.com"
    mocker.patch("wikipedia.page", return_value=mock_page)
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia_scraper._get_page_html",
        return_value=DEDICATED_WITH_OVERVIEW_ONGOING_HTML,
    )

    # Should return the enumerated title count (4), not the overview's 10
    count = await wikipedia_scraper.fetch_season_episode_count_from_wikipedia(
        "Show", 27
    )
    assert count == 4


@pytest.mark.asyncio
async def test_fetch_season_episode_count_skips_ongoing_overview(mocker):
    mock_page = mocker.Mock()
    mock_page.url = "http://example.com"
    mocker.patch("wikipedia.page", return_value=mock_page)
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia_scraper._get_page_html",
        return_value=OVERVIEW_ONGOING_ONLY_HTML,
    )

    # No titles are present and overview is marked ongoing -> expect None
    count = await wikipedia_scraper.fetch_season_episode_count_from_wikipedia(
        "Show", 27
    )
    assert count is None
