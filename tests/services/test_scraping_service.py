# tests/services/test_scraping_service.py

import sys
from pathlib import Path
import logging
import pytest
from unittest.mock import Mock, AsyncMock
import wikipedia
from bs4 import BeautifulSoup
from telegram_bot.services import scraping_service
from telegram_bot.services.scrapers import wikipedia as wiki_module
from telegram_bot.services.scrapers.wikipedia import franchise as wiki_franchise_module

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))


# Ensure per-test isolation by clearing any module-level caches used by the
# scraping_service. This prevents earlier tests from influencing later ones
# (e.g., Wikipedia title caches across the same show/season).
@pytest.fixture(autouse=True)
def _clear_wiki_caches():
    try:
        scraping_service._WIKI_TITLES_CACHE.clear()  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        scraping_service._WIKI_SOUP_CACHE.clear()  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        scraping_service._WIKI_MOVIE_CACHE.clear()  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        scraping_service._WIKI_FRANCHISE_CACHE.clear()  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        scraping_service.clear_wiki_cache()
    except Exception:
        pass


def test_wiki_cache_evicts_and_expires():
    class FakeClock:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = FakeClock()
    cache = scraping_service.WikiCache(max_entries=2, ttl=10, clock=clock)
    cache.set(("a",), "alpha")
    clock.value += 5
    assert cache.get(("a",)) == "alpha"
    clock.value += 6
    assert cache.get(("a",)) is scraping_service.WikiCache.MISS

    cache.set(("b",), "bravo")
    cache.set(("c",), "charlie")
    cache.set(("d",), "delta")
    assert cache.get(("b",)) is scraping_service.WikiCache.MISS
    assert cache.get(("c",)) == "charlie"


@pytest.mark.asyncio
async def test_fetch_movie_years_uses_cache(mocker):
    mock_call = mocker.patch(
        "telegram_bot.services.scraping_service._raw_fetch_movie_years",
        new=AsyncMock(return_value=([1999], None)),
    )
    await scraping_service.fetch_movie_years_from_wikipedia("The Matrix")
    await scraping_service.fetch_movie_years_from_wikipedia("The Matrix")
    assert mock_call.await_count == 1


@pytest.mark.asyncio
async def test_fetch_movie_years_negative_cache(mocker):
    mock_call = mocker.patch(
        "telegram_bot.services.scraping_service._raw_fetch_movie_years",
        new=AsyncMock(return_value=([], None)),
    )
    await scraping_service.fetch_movie_years_from_wikipedia("Made Up Title")
    await scraping_service.fetch_movie_years_from_wikipedia("Made Up Title")
    assert mock_call.await_count == 1


@pytest.mark.asyncio
async def test_fetch_movie_franchise_details_caches_results(mocker):
    mock_call = mocker.patch(
        "telegram_bot.services.scraping_service._raw_fetch_franchise_details",
        new=AsyncMock(return_value=("Saga", [{"title": "Movie One", "year": 2001}])),
    )
    result = await scraping_service.fetch_movie_franchise_details("Saga")
    assert result[0] == "Saga"
    await scraping_service.fetch_movie_franchise_details("Saga")
    assert mock_call.await_count == 1


class DummyResponse:
    def __init__(self, text="", json_data=None, status_code=200):
        self.text = text
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class DummyClient:
    def __init__(self, responses):
        self._responses = responses
        self._index = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def get(self, url, *args, **kwargs):
        response = self._responses[self._index]
        self._index += 1
        return response


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


WRONG_HEADER_HTML = """
<h3>Overview</h3>
<table class="wikitable">
<tr><th>No.</th><th>No. in season</th><th>Title</th></tr>
<tr><td>1</td><td>1</td><td>"Pilot"</td></tr>
</table>
"""

VARIED_COLUMNS_HTML = """
<table class="wikitable">
<tr><th>Season</th><th>No.</th><th>Title</th></tr>
<tr><td>1</td><td>1</td><td><i>Pilot</i></td></tr>
</table>
"""

TWO_COLUMN_HTML = """
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

FRANCHISE_INFOBOX_HTML = """
<table class="infobox vevent">
  <tr><th scope="row" class="infobox-label">Film(s)</th><td class="infobox-data">
    <div class="plainlist">
      <ul>
        <li><i><a title="The Equalizer (film)">The Equalizer</a></i> (2014)</li>
        <li><i><a title="The Equalizer 2">The Equalizer 2</a></i> (2018)</li>
        <li><i><a title="The Equalizer 3">The Equalizer 3</a></i> (2023)</li>
      </ul>
    </div>
  </td></tr>
  <tr><th scope="row" class="infobox-label">Television series</th><td class="infobox-data">
    <div class="plainlist">
      <ul>
        <li><i><a title="The Equalizer (1985 TV series)">The Equalizer</a></i> (1985-1989)</li>
        <li><i><a title="The Equalizer (2021 TV series)">The Equalizer</a></i> (2021-2025)</li>
      </ul>
    </div>
  </td></tr>
  <tr><th scope="row" class="infobox-label">Soundtrack(s)</th><td class="infobox-data">
    <div class="plainlist">
      <ul>
        <li><i><a title="The Equalizer (soundtrack)">The Equalizer</a></i></li>
      </ul>
    </div>
  </td></tr>
</table>
"""

TRON_STYLE_INFOBOX_HTML = """
<table class="infobox vevent">
  <tr>
    <th scope="row" class="infobox-label">Film(s)</th>
    <td class="infobox-data">
      <a href="/wiki/Tron_(film)" title="Tron (film)">Tron</a> (1982)
      <br/>
      <a href="/wiki/Tron:_Legacy" title="Tron: Legacy">Tron: Legacy</a> (2010)
      <br/>
      <a href="/wiki/Tron:_Ares" title="Tron: Ares">Tron: Ares</a> (2025)
    </td>
  </tr>
  <tr>
    <th scope="row" class="infobox-label">Short film(s)</th>
    <td class="infobox-data">
      <a href="/wiki/The_Ghost_in_the_Machine_(film)" title="The Ghost in the Machine (film)">
        The Ghost in the Machine
      </a> (2010)
    </td>
  </tr>
  <tr>
    <th scope="row" class="infobox-label">Animated series</th>
    <td class="infobox-data">
      <a href="/wiki/Tron:_Uprising" title="Tron: Uprising">Tron: Uprising</a> (2012-2013)
    </td>
  </tr>
</table>
"""

FILM_SERIES_SECTION_HTML = """
<div class="mw-heading mw-heading2"><h2 id="Film_series">Film series</h2></div>
<div class="mw-heading mw-heading3"><h3 id="Movie_1"><i>Movie One</i> (2001)</h3></div>
<p>Some film production notes.</p>
<div class="mw-heading mw-heading3"><h3 id="Movie_2"><i>Movie Two</i> (2004)</h3></div>
<p>More release information.</p>
<div class="mw-heading mw-heading3"><h3 id="Development">Development</h3></div>
<p>Background text without a release year.</p>
<div class="mw-heading mw-heading3"><h3 id="Movie_3"><i>Movie Three</i> (2008)</h3></div>
<div class="mw-heading mw-heading2"><h2 id="Literature">Literature</h2></div>
"""

FRANCHISE_SCORING_HTML = (
    """
<h1>The Equalizer</h1>
<p>The Equalizer is an American thriller franchise.</p>
<div class="mw-heading mw-heading2"><h2 id="Film_series">Film series</h2></div>
"""
    + FRANCHISE_INFOBOX_HTML
)

SOUNDTRACK_SCORING_HTML = """
<h1>The Equalizer (soundtrack)</h1>
<p>The Equalizer soundtrack album features music from the film score and songs.</p>
<div class="mw-heading mw-heading2"><h2 id="Track_listing">Track listing</h2></div>
<div class="mw-heading mw-heading2"><h2 id="Discography">Discography</h2></div>
"""

SOUNDTRACK_WITH_FILM_TABLE_HTML = """
<h1>The Equalizer Ranking Test (soundtrack)</h1>
<p>The soundtrack album and film score includes songs from the series discography.</p>
<table class="wikitable">
  <tr><th>Film</th><th>Release date</th></tr>
  <tr><td>The Equalizer</td><td>September 24, 2014</td></tr>
  <tr><td>The Equalizer 2</td><td>July 20, 2018</td></tr>
  <tr><td>The Equalizer 3</td><td>September 1, 2023</td></tr>
</table>
"""

NAVBOX_FILMS_HTML = """
<table class="nowraplinks hlist navbox-inner">
  <tr>
    <th scope="row" class="navbox-group">TV series</th>
    <td class="navbox-list-with-group navbox-list">
      <div><ul><li>The Equalizer (1985 TV series)</li><li>The Equalizer (2021 TV series)</li></ul></div>
    </td>
  </tr>
  <tr>
    <th scope="row" class="navbox-group">Films</th>
    <td class="navbox-list-with-group navbox-list">
      <div>
        <ul>
          <li><i>The Equalizer</i></li>
          <li><i>The Equalizer 2</i></li>
          <li><i>The Equalizer 3</i></li>
        </ul>
      </div>
    </td>
  </tr>
  <tr>
    <th scope="row" class="navbox-group">Soundtracks</th>
    <td class="navbox-list-with-group navbox-list">
      <div>
        <ul>
          <li><i>The Equalizer (soundtrack)</i></li>
          <li><i>The Equalizer 2 (soundtrack)</i></li>
          <li><i>The Equalizer 3 (soundtrack)</i></li>
        </ul>
      </div>
    </td>
  </tr>
</table>
"""

NAVBOX_SOUNDTRACKS_ONLY_HTML = """
<table class="nowraplinks hlist navbox-inner">
  <tr>
    <th scope="row" class="navbox-group">Soundtracks</th>
    <td class="navbox-list-with-group navbox-list">
      <div>
        <ul>
          <li><i>The Equalizer (soundtrack)</i></li>
          <li><i>The Equalizer 2 (soundtrack)</i></li>
          <li><i>The Equalizer 3 (soundtrack)</i></li>
        </ul>
      </div>
    </td>
  </tr>
</table>
"""


def test_extract_movies_from_table_allows_duplicate_titles():
    html = """
    <table class="wikitable">
        <tr><th>Title</th><th>Release date</th></tr>
        <tr><td>Dune</td><td>December 14, 1984</td></tr>
        <tr><td>Dune</td><td>October 22, 2021</td></tr>
        <tr><td>Dune: Part Two</td><td>March 1, 2024</td></tr>
    </table>
    """
    soup = BeautifulSoup(html, "html.parser")
    movies = wiki_module._extract_movies_from_table(soup.find("table"))
    assert len(movies) == 3
    dune_titles = [entry["title"] for entry in movies if "Dune" == entry["title"]]
    assert len(dune_titles) == 2
    assert all(entry.get("release_date") for entry in movies)


def test_extract_movies_from_table_rejects_tv_season_box_sets():
    html = """
    <table class="wikitable">
        <tr><th>Title</th><th>Release date</th></tr>
        <tr><td>The First Season</td><td>September 23, 2008</td></tr>
        <tr><td>The Second Season</td><td>September 1, 2009</td></tr>
        <tr><td>The Third Season</td><td>September 7, 2010</td></tr>
    </table>
    """
    soup = BeautifulSoup(html, "html.parser")
    movies = wiki_module._extract_movies_from_table(soup.find("table"))
    assert movies == []


def test_extract_movies_from_infobox_parses_films_row_only():
    soup = BeautifulSoup(FRANCHISE_INFOBOX_HTML, "html.parser")

    movies = wiki_module._extract_movies_from_infobox(soup)

    assert [movie["title"] for movie in movies] == [
        "The Equalizer",
        "The Equalizer 2",
        "The Equalizer 3",
    ]
    assert [movie["year"] for movie in movies] == [2014, 2018, 2023]


def test_extract_movies_from_infobox_keeps_year_only_entries_without_fabricated_release_dates():
    soup = BeautifulSoup(FRANCHISE_INFOBOX_HTML, "html.parser")

    movies = wiki_module._extract_movies_from_infobox(soup)

    assert [movie["release_date"] for movie in movies] == [None, None, None]


def test_extract_movies_from_infobox_handles_inline_link_entries_with_dates():
    soup = BeautifulSoup(TRON_STYLE_INFOBOX_HTML, "html.parser")

    movies = wiki_module._extract_movies_from_infobox(soup)

    assert [movie["title"] for movie in movies] == ["Tron", "Tron: Legacy", "Tron: Ares"]
    assert [movie["year"] for movie in movies] == [1982, 2010, 2025]
    assert [movie["release_date"] for movie in movies] == [None, None, None]


def test_extract_movies_from_table_preserves_real_release_dates_exactly():
    html = """
    <table class="wikitable">
        <tr><th>Title</th><th>Release date</th></tr>
        <tr><td>Movie One</td><td>September 24, 2014</td></tr>
        <tr><td>Movie Two</td><td>July 20, 2018</td></tr>
    </table>
    """
    soup = BeautifulSoup(html, "html.parser")

    movies = wiki_module._extract_movies_from_table(soup.find("table"))

    assert [movie["release_date"] for movie in movies] == ["2014-09-24", "2018-07-20"]


def test_extract_movies_from_film_series_section_parses_movie_headings():
    soup = BeautifulSoup(FILM_SERIES_SECTION_HTML, "html.parser")

    movies = wiki_module._extract_movies_from_film_series_section(soup)

    assert [movie["title"] for movie in movies] == ["Movie One", "Movie Two", "Movie Three"]
    assert [movie["year"] for movie in movies] == [2001, 2004, 2008]


def test_extract_movies_from_navbox_films_uses_only_films_row():
    soup = BeautifulSoup(NAVBOX_FILMS_HTML, "html.parser")

    movies = wiki_module._extract_movies_from_navbox_films(soup)

    assert [movie["title"] for movie in movies] == [
        "The Equalizer",
        "The Equalizer 2",
        "The Equalizer 3",
    ]
    assert all("soundtrack" not in movie["title"].casefold() for movie in movies)


def test_extract_movies_from_navbox_films_ignores_soundtracks_only_row():
    soup = BeautifulSoup(NAVBOX_SOUNDTRACKS_ONLY_HTML, "html.parser")

    movies = wiki_module._extract_movies_from_navbox_films(soup)

    assert movies == []


def test_score_franchise_candidate_prefers_franchise_page_over_soundtrack_page():
    franchise_soup = BeautifulSoup(FRANCHISE_SCORING_HTML, "html.parser")
    soundtrack_soup = BeautifulSoup(SOUNDTRACK_SCORING_HTML, "html.parser")
    movies = wiki_module._extract_movies_from_infobox(
        BeautifulSoup(FRANCHISE_INFOBOX_HTML, "html.parser")
    )

    franchise_score = wiki_module._score_franchise_candidate(
        candidate_title="The Equalizer",
        resolved_title="The Equalizer (film series)",
        soup=franchise_soup,
        movies=movies,
        source_kind="infobox",
    )
    soundtrack_score = wiki_module._score_franchise_candidate(
        candidate_title="The Equalizer (soundtrack)",
        resolved_title="The Equalizer (soundtrack)",
        soup=soundtrack_soup,
        movies=movies,
        source_kind="generic",
    )

    assert franchise_score["score"] > soundtrack_score["score"]
    assert "title:film series" in franchise_score["signals"]["positive"]
    assert "title:soundtrack" in soundtrack_score["signals"]["negative"]


def test_score_franchise_candidate_rewards_structured_and_dated_evidence():
    soup = BeautifulSoup(FRANCHISE_SCORING_HTML, "html.parser")
    movies = wiki_module._extract_movies_from_infobox(
        BeautifulSoup(FRANCHISE_INFOBOX_HTML, "html.parser")
    )

    score = wiki_module._score_franchise_candidate(
        candidate_title="The Equalizer",
        resolved_title="The Equalizer (film series)",
        soup=soup,
        movies=movies,
        source_kind="infobox",
    )

    assert "source:infobox" in score["signals"]["positive"]
    assert "infobox:films_field" in score["signals"]["positive"]
    assert "movies:count=3" in score["signals"]["positive"]
    assert "movies:dated=3" in score["signals"]["positive"]
    assert score["score"] > 0


def test_score_franchise_candidate_rewards_dated_movies_over_title_only_entries():
    soup = BeautifulSoup(FRANCHISE_SCORING_HTML, "html.parser")
    dated_movies = [
        {"title": "Movie One", "year": 2001, "release_date": None},
        {"title": "Movie Two", "year": 2004, "release_date": None},
        {"title": "Movie Three", "year": 2008, "release_date": None},
    ]
    title_only_movies = [
        {"title": "Movie One", "year": None, "release_date": None},
        {"title": "Movie Two", "year": None, "release_date": None},
        {"title": "Movie Three", "year": None, "release_date": None},
    ]

    dated_score = wiki_module._score_franchise_candidate(
        candidate_title="The Equalizer",
        resolved_title="The Equalizer (film series)",
        soup=soup,
        movies=dated_movies,
        source_kind="infobox",
    )
    title_only_score = wiki_module._score_franchise_candidate(
        candidate_title="The Equalizer",
        resolved_title="The Equalizer (film series)",
        soup=soup,
        movies=title_only_movies,
        source_kind="infobox",
    )

    assert dated_score["score"] > title_only_score["score"]
    assert "movies:dated=3" in dated_score["signals"]["positive"]
    assert "movies:dated=3" not in title_only_score["signals"]["positive"]


def test_rank_franchise_search_candidates_prefers_franchise_variants_and_filters_noise():
    ranked = wiki_franchise_module._rank_franchise_search_candidates(
        "The Equalizer",
        [
            "The Equalizer (2014 film)",
            "The Equalizer (film series)",
            "The Equalizer franchise",
            "The Equalizer soundtrack",
            "Equalizer",
            "The Equalizer anthology collection",
        ],
    )

    ranked_titles = [candidate["title"] for candidate in ranked]
    assert set(ranked_titles[:2]) == {
        "The Equalizer (film series)",
        "The Equalizer franchise",
    }
    assert "The Equalizer (2014 film)" not in ranked_titles
    assert "The Equalizer soundtrack" not in ranked_titles
    assert "Equalizer" not in ranked_titles


@pytest.mark.asyncio
async def test_fetch_movie_franchise_details_caps_page_resolution_to_top_candidates(mocker):
    candidates = [f"Movie Saga anthology volume {idx}" for idx in range(1, 11)]

    search_mock = mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise.wikipedia.search",
        side_effect=[candidates, [], [], []],
    )
    resolve_mock = mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise._resolve_franchise_candidate",
        new=AsyncMock(return_value=None),
    )

    result = await wiki_module.fetch_movie_franchise_details_from_wikipedia("Movie Saga")

    assert result is None
    assert search_mock.call_count == 4
    assert resolve_mock.await_count == 6
    assert [call.args[0] for call in resolve_mock.await_args_list] == candidates[:6]


@pytest.mark.asyncio
async def test_fetch_movie_franchise_details_accepts_infobox_only_page(mocker):
    page = mocker.Mock()
    page.title = "Equalizer Infobox Test"

    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise.wikipedia.search",
        return_value=["Equalizer Infobox Test"],
    )
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise._resolve_franchise_candidate",
        new=AsyncMock(return_value=page),
    )
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise._fetch_html_from_page",
        new=AsyncMock(return_value=FRANCHISE_INFOBOX_HTML),
    )

    result = await wiki_module.fetch_movie_franchise_details_from_wikipedia(
        "Equalizer Infobox Test"
    )

    assert result is not None
    franchise_name, movies = result
    assert franchise_name == "Equalizer Infobox Test"
    assert [movie["title"] for movie in movies] == [
        "The Equalizer",
        "The Equalizer 2",
        "The Equalizer 3",
    ]


@pytest.mark.asyncio
async def test_fetch_movie_franchise_details_accepts_inline_infobox_page(mocker):
    page = mocker.Mock()
    page.title = "Tron (franchise)"

    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise.wikipedia.search",
        return_value=["Tron (franchise)"],
    )
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise._resolve_franchise_candidate",
        new=AsyncMock(return_value=page),
    )
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise._fetch_html_from_page",
        new=AsyncMock(return_value=TRON_STYLE_INFOBOX_HTML),
    )

    result = await wiki_module.fetch_movie_franchise_details_from_wikipedia("Tron")

    assert result is not None
    franchise_name, movies = result
    assert franchise_name == "Tron (franchise)"
    assert [movie["title"] for movie in movies] == ["Tron", "Tron: Legacy", "Tron: Ares"]
    assert [movie["year"] for movie in movies] == [1982, 2010, 2025]


@pytest.mark.asyncio
async def test_fetch_movie_franchise_details_prefers_higher_scoring_later_candidate(mocker):
    franchise_page = mocker.Mock()
    franchise_page.title = "The Equalizer Ranking Test (film series)"

    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise.wikipedia.search",
        return_value=[
            "The Equalizer Ranking Test (soundtrack)",
            "The Equalizer Ranking Test (film series)",
        ],
    )
    resolve_mock = mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise._resolve_franchise_candidate",
        new=AsyncMock(return_value=franchise_page),
    )
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise._fetch_html_from_page",
        new=AsyncMock(return_value=FRANCHISE_SCORING_HTML),
    )

    result = await wiki_module.fetch_movie_franchise_details_from_wikipedia(
        "The Equalizer Ranking Test"
    )

    assert result is not None
    franchise_name, movies = result
    assert franchise_name == "The Equalizer Ranking Test (film series)"
    resolve_mock.assert_awaited_once_with("The Equalizer Ranking Test (film series)")
    assert [movie["title"] for movie in movies] == [
        "The Equalizer",
        "The Equalizer 2",
        "The Equalizer 3",
    ]


@pytest.mark.asyncio
async def test_fetch_movie_franchise_details_returns_none_for_low_confidence_soundtrack_page(
    mocker,
):
    soundtrack_page = mocker.Mock()
    soundtrack_page.title = "Soundtrack Confidence Test (soundtrack)"

    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise.wikipedia.search",
        return_value=["Soundtrack Confidence Test (soundtrack)"],
    )
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise._resolve_franchise_candidate",
        new=AsyncMock(return_value=soundtrack_page),
    )
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise._fetch_html_from_page",
        new=AsyncMock(return_value=SOUNDTRACK_WITH_FILM_TABLE_HTML),
    )

    result = await wiki_module.fetch_movie_franchise_details_from_wikipedia(
        "Soundtrack Confidence Test"
    )

    assert result is None


@pytest.mark.asyncio
async def test_fetch_movie_franchise_details_accepts_film_series_section_only_page(mocker):
    page = mocker.Mock()
    page.title = "Movie Saga"

    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise.wikipedia.search",
        return_value=["Movie Saga"],
    )
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise._resolve_franchise_candidate",
        new=AsyncMock(return_value=page),
    )
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise._fetch_html_from_page",
        new=AsyncMock(return_value=FILM_SERIES_SECTION_HTML),
    )

    result = await wiki_module.fetch_movie_franchise_details_from_wikipedia("Movie Saga")

    assert result is not None
    franchise_name, movies = result
    assert franchise_name == "Movie Saga"
    assert [movie["title"] for movie in movies] == ["Movie One", "Movie Two", "Movie Three"]


@pytest.mark.asyncio
async def test_fetch_movie_franchise_details_accepts_navbox_films_only_page(mocker):
    page = mocker.Mock()
    page.title = "Equalizer Navbox Test"

    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise.wikipedia.search",
        return_value=["Equalizer Navbox Test"],
    )
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise._resolve_franchise_candidate",
        new=AsyncMock(return_value=page),
    )
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise._fetch_html_from_page",
        new=AsyncMock(return_value=NAVBOX_FILMS_HTML),
    )

    result = await wiki_module.fetch_movie_franchise_details_from_wikipedia("Equalizer Navbox Test")

    assert result is not None
    franchise_name, movies = result
    assert franchise_name == "Equalizer Navbox Test"
    assert [movie["title"] for movie in movies] == [
        "The Equalizer",
        "The Equalizer 2",
        "The Equalizer 3",
    ]


@pytest.mark.asyncio
async def test_fetch_movie_franchise_details_skips_tv_season_release_tables(mocker):
    film_page = mocker.Mock()
    film_page.title = "The Equalizer (film series)"

    movie_html = """
    <table class="wikitable">
        <tr><th>Film</th><th>Release date</th></tr>
        <tr><td>The Equalizer</td><td>September 24, 2014</td></tr>
        <tr><td>The Equalizer 2</td><td>July 20, 2018</td></tr>
        <tr><td>The Equalizer 3</td><td>September 1, 2023</td></tr>
    </table>
    """
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise.wikipedia.search",
        return_value=["The Equalizer (TV series)", "The Equalizer (film series)"],
    )
    resolve_mock = mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise._resolve_franchise_candidate",
        new=AsyncMock(return_value=film_page),
    )
    mocker.patch(
        "telegram_bot.services.scrapers.wikipedia.franchise._fetch_html_from_page",
        new=AsyncMock(return_value=movie_html),
    )

    result = await wiki_module.fetch_movie_franchise_details_from_wikipedia("The Equalizer")

    assert result is not None
    resolve_mock.assert_awaited_once_with("The Equalizer (film series)")
    franchise_name, movies = result
    assert franchise_name == "The Equalizer (film series)"
    assert [movie["title"] for movie in movies] == [
        "The Equalizer",
        "The Equalizer 2",
        "The Equalizer 3",
    ]


OVERVIEW_ONGOING_ONLY_HTML = """
<h2>Series overview</h2>
<table class="wikitable">
<tr><th>Season</th><th>Episodes</th><th>Originally aired</th></tr>
<tr><td>27</td><td>10</td><td>2024–present</td></tr>
</table>
"""


FUTURE_AND_TBA_HTML = """
<h3>Season 5</h3>
<table class="wikitable">
<tr><th>No.</th><th>Title</th><th>Original air date</th></tr>
<tr><td>1</td><td>"Released Episode"</td><td>January 1, 2000</td></tr>
<tr><td>2</td><td>"Future Episode"</td><td>January 1, 3000</td></tr>
<tr><td>3</td><td>"TBA Episode"</td><td>TBA</td></tr>
<tr><td>4</td><td>"NA Episode"</td><td>N/A</td></tr>
</table>
"""


@pytest.mark.asyncio
async def test_fetch_season_episode_count_filters_unreleased(mocker):
    mock_page = mocker.Mock()
    mock_page.url = "http://example.com"
    mock_page.html.return_value = FUTURE_AND_TBA_HTML
    mocker.patch("wikipedia.page", return_value=mock_page)
    mocker.patch("wikipedia.search", return_value=["Show"])

    # Should return 1 because only the first episode is released
    count = await scraping_service.fetch_season_episode_count_from_wikipedia("Show", 5)
    assert count == 1


@pytest.mark.asyncio
async def test_fetch_episode_title_dedicated_page(mocker):
    mock_page = mocker.Mock()
    mock_page.title = "Show"
    mock_page.url = "http://example.com"
    mock_page.html.return_value = DEDICATED_HTML
    mocker.patch("wikipedia.search", return_value=["Show"])
    mocker.patch("wikipedia.page", return_value=mock_page)

    title, corrected = await scraping_service.fetch_episode_title_from_wikipedia("Show", 1, 1)
    assert title == "Pilot"
    assert corrected is None


@pytest.mark.asyncio
async def test_fetch_episode_title_strips_miniseries_suffix(mocker):
    mock_main_page = mocker.Mock()
    mock_main_page.title = "Show (miniseries)"
    mock_main_page.url = "http://example.com/show"

    mock_list_page = mocker.Mock()
    mock_list_page.url = "http://example.com/list"
    mock_list_page.html.return_value = DEDICATED_HTML

    mocker.patch("wikipedia.search", return_value=["Show (miniseries)"])
    page_patch = mocker.patch("wikipedia.page", side_effect=[mock_main_page, mock_list_page])

    title, corrected = await scraping_service.fetch_episode_title_from_wikipedia("Show", 1, 1)

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
    mock_list_page.html.return_value = DEDICATED_HTML

    mocker.patch("wikipedia.search", return_value=["Show (TV series)"])
    page_patch = mocker.patch("wikipedia.page", side_effect=[mock_main_page, mock_list_page])

    title, corrected = await scraping_service.fetch_episode_title_from_wikipedia("Show", 1, 1)

    assert title == "Pilot"
    assert corrected is None
    assert page_patch.call_args_list[1].args[0] == "List of Show episodes"


@pytest.mark.asyncio
async def test_fetch_episode_title_embedded_page(mocker):
    mock_main_page = mocker.Mock()
    mock_main_page.title = "Show"
    mock_main_page.url = "http://example.com/main"
    mock_main_page.html.return_value = SIMPLE_EMBEDDED_HTML

    mocker.patch("wikipedia.search", return_value=["Show"])
    mocker.patch(
        "wikipedia.page",
        side_effect=[mock_main_page, wikipedia.exceptions.PageError("no list")],
    )

    title, _ = await scraping_service.fetch_episode_title_from_wikipedia("Show", 1, 1)
    assert title == "Pilot"


@pytest.mark.asyncio
async def test_fetch_episode_title_not_found(mocker):
    mock_page = mocker.Mock()
    mock_page.url = "http://example.com"
    mock_page.html.return_value = NO_EPISODE_HTML
    mocker.patch("wikipedia.search", return_value=["Show"])
    mocker.patch("wikipedia.page", return_value=mock_page)

    title, _ = await scraping_service.fetch_episode_title_from_wikipedia("Show", 1, 1)
    assert title is None


@pytest.mark.asyncio
async def test_fetch_season_episode_count(mocker):
    mock_page = mocker.Mock()
    mock_page.url = "http://example.com"
    mock_page.html.return_value = SEASON_OVERVIEW_HTML
    mocker.patch("wikipedia.page", return_value=mock_page)

    count = await scraping_service.fetch_season_episode_count_from_wikipedia("Show", 2)
    assert count == 8


@pytest.mark.asyncio
async def test_fetch_season_episode_count_prefers_titles_over_overview(mocker):
    mock_page = mocker.Mock()
    mock_page.url = "http://example.com"
    mock_page.html.return_value = DEDICATED_WITH_OVERVIEW_ONGOING_HTML
    mocker.patch("wikipedia.page", return_value=mock_page)

    # Should return the enumerated title count (4), not the overview's 10
    count = await scraping_service.fetch_season_episode_count_from_wikipedia("Show", 27)
    assert count == 4


@pytest.mark.asyncio
async def test_fetch_season_episode_count_skips_ongoing_overview(mocker):
    mock_page = mocker.Mock()
    mock_page.url = "http://example.com"
    mock_page.html.return_value = OVERVIEW_ONGOING_ONLY_HTML
    mocker.patch("wikipedia.page", return_value=mock_page)

    # No titles are present and overview is marked ongoing -> expect None
    count = await scraping_service.fetch_season_episode_count_from_wikipedia("Show", 27)
    assert count is None


@pytest.mark.asyncio
async def test_scrape_1337x_parses_results(mocker):
    # This is the response for the initial search results page
    search_html = """
    <table class="table-list"><tbody>
    <tr>
      <td class="name">
        <a href="/cat">Movies</a>
        <a href="/torrent/1/Sample.Movie.2023.1080p.x265/">Sample.Movie.2023.1080p.x265</a>
      </td>
      <td class="seeds">25</td>
      <td class="leeches">0</td>
      <td class="size">1.5 GB</td>
      <td class="uploader"><a>Anonymous</a></td>
    </tr>
    </tbody></table>
    """

    # This is the required second response for the torrent detail page
    detail_html = """
    <div>
      <a class="btn-magnet" href="magnet:?xt=urn:btih:FAKEHASH">Magnet Download</a>
    </div>
    """

    # The mock client now has TWO responses to give, and they will have a default status_code of 200
    responses = [DummyResponse(text=search_html), DummyResponse(text=detail_html)]
    mocker.patch(
        "telegram_bot.services.scrapers.adapters.create_async_client",
        return_value=DummyClient(responses),
    )

    context = Mock()
    context.bot_data = {
        "SEARCH_CONFIG": {
            "preferences": {
                "movies": {
                    "codecs": {"x265": 5},
                    "resolutions": {"1080p": 3},
                    "uploaders": {"Anonymous": 2},
                }
            }
        }
    }

    results = await scraping_service.scrape_1337x(
        "Sample Movie 2023",
        "movie",
        "https://1337x.to/search/{query}/1/",
        context,
        base_query_for_filter="Sample Movie",
    )

    assert len(results) == 1
    assert results[0]["title"] == "Sample.Movie.2023.1080p.x265"
    assert results[0]["page_url"].startswith("magnet:")
    assert results[0]["source"] == "1337x"


@pytest.mark.asyncio
async def test_scrape_1337x_no_results(mocker):
    html = "<html><body>No results</body></html>"
    responses = [DummyResponse(text=html)]
    mocker.patch(
        "telegram_bot.services.scrapers.adapters.create_async_client",
        return_value=DummyClient(responses),
    )

    context = Mock()
    context.bot_data = {
        "SEARCH_CONFIG": {
            "preferences": {
                "movies": {
                    "codecs": {},
                    "resolutions": {},
                    "uploaders": {},
                }
            }
        }
    }

    results = await scraping_service.scrape_1337x(
        "Sample",
        "movie",
        "https://1337x.to/search/{query}/1/",
        context,  # Pass the mock object here
        base_query_for_filter="Sample Movie",
    )

    assert results == []


@pytest.mark.asyncio
async def test_scrape_1337x_fuzzy_filter(mocker):
    """Non-matching titles should be filtered out when fuzzy filter is enabled."""

    search_html = """
    <table class="table-list"><tbody>
    <tr>
      <td class="name">
        <a href="/cat">Movies</a>
        <a href="/torrent/1/Sample.Movie.2023.1080p.x265/">Sample.Movie.2023.1080p.x265</a>
      </td>
      <td class="seeds">25</td>
      <td class="leeches">0</td>
      <td class="size">1.5 GB</td>
      <td class="uploader"><a>Anonymous</a></td>
    </tr>
    <tr>
      <td class="name">
        <a href="/cat">Movies</a>
        <a href="/torrent/2/Unrelated.File.2023.1080p.x265/">Unrelated.File.2023.1080p.x265</a>
      </td>
      <td class="seeds">20</td>
      <td class="leeches">0</td>
      <td class="size">1.0 GB</td>
      <td class="uploader"><a>Anonymous</a></td>
    </tr>
    </tbody></table>
    """

    detail_good = """
    <div><a class="btn-magnet" href="magnet:?xt=urn:btih:GOOD">Magnet</a></div>
    """

    responses = [
        DummyResponse(text=search_html),
        DummyResponse(text=detail_good),
    ]
    client = DummyClient(responses)
    mocker.patch("telegram_bot.services.scrapers.adapters.create_async_client", return_value=client)

    context = Mock()
    context.bot_data = {
        "SEARCH_CONFIG": {
            "preferences": {
                "movies": {
                    "codecs": {"x265": 5},
                    "resolutions": {"1080p": 3},
                    "uploaders": {"Anonymous": 2},
                }
            }
        }
    }

    results = await scraping_service.scrape_1337x(
        "Sample Movie 2023",
        "movie",
        "https://1337x.to/search/{query}/1/",
        context,
        base_query_for_filter="Sample Movie",
    )

    assert len(results) == 1
    assert results[0]["title"] == "Sample.Movie.2023.1080p.x265"
    # Only the search page and one detail page should have been requested
    assert client._index == 2


@pytest.mark.asyncio
async def test_scrape_1337x_passes_limit(mocker):
    """scrape_1337x should forward the limit argument to the scraper."""

    mock_search = AsyncMock(return_value=[])
    mocker.patch.object(scraping_service.GenericTorrentScraper, "search", mock_search)

    context = Mock()
    context.bot_data = {"SEARCH_CONFIG": {"preferences": {"movies": {"codecs": {"x": 1}}}}}

    await scraping_service.scrape_1337x(
        "query", "movie", "https://example.com/{query}", context, limit=7
    )

    mock_search.assert_awaited_once()
    assert mock_search.call_args.kwargs["limit"] == 7


@pytest.mark.asyncio
async def test_scrape_eztv_parses_results(mocker):
    """EZTV YAML scraper parses results and resolves magnet links."""

    search_html = """
    <table>
      <tr class="forum_header_border" name="hover">
        <td>Example Show</td>
        <td>
          <a class="epinfo" href="/episodes/12345">
            Example.Show.S01E01.1080p.WEB.x264
          </a>
        </td>
        <td class="forum_thread_post_end">150</td>
        <td>1.4 GB</td>
        <td>0</td>
        <td>SceneGroup</td>
      </tr>
    </table>
    """
    detail_html = """
    <div>
      <a href="magnet:?xt=urn:btih:EZTVHASH&dn=Example+Show">Magnet</a>
    </div>
    """

    fetch_mock = mocker.patch(
        "telegram_bot.services.generic_torrent_scraper.GenericTorrentScraper._fetch_page",
        new_callable=AsyncMock,
        side_effect=[search_html, detail_html],
    )

    context = Mock()
    context.bot_data = {
        "SEARCH_CONFIG": {
            "preferences": {
                "tv": {
                    "codecs": {"x264": 2},
                    "resolutions": {"1080p": 3},
                    "uploaders": {},
                }
            }
        }
    }

    results = await scraping_service.scrape_yaml_site(
        "Example Show S01E01",
        "tv",
        "https://eztvx.to/search/{query}",
        context,
        site_name="eztv",
        base_query_for_filter="Example Show S01E01",
    )

    assert fetch_mock.await_count == 2
    assert len(results) == 1
    entry = results[0]
    assert entry["source"] == "eztv"
    assert entry["seeders"] == 150
    assert entry["page_url"].startswith("magnet:?xt=urn:btih:EZTVHASH")
    assert entry["matched_video_formats"] == []
    assert entry["matched_audio_formats"] == []
    assert entry["matched_audio_channels"] == []
    assert entry["is_gold_av"] is False
    assert entry["is_silver_av"] is False
    assert entry["has_video_match"] is False
    assert entry["has_audio_match"] is False


@pytest.mark.asyncio
async def test_scrape_yts_parses_results(mocker):
    search_html = """
    <div class="browse-movie-wrap">
      <a class="browse-movie-title" href="https://yts.lt/movies/test-movie">Test Movie</a>
      <div class="browse-movie-year">2023</div>
    </div>
    """
    movie_html = '<div id="movie-info" data-movie-id="1234"></div>'
    api_json = {
        "status": "ok",
        "data": {
            "movie": {
                "title_long": "Test Movie (2023)",
                "year": 2023,
                "torrents": [
                    {
                        "quality": "1080p",
                        "type": "WEB",
                        "size_bytes": 1024**3,
                        "hash": "abcdef",
                        "seeds": 25,
                    }
                ],
            }
        },
    }
    responses = [
        DummyResponse(text=search_html),
        DummyResponse(text=movie_html),
        DummyResponse(json_data=api_json),
    ]
    mocker.patch(
        "telegram_bot.services.scrapers.adapters.create_async_client",
        return_value=DummyClient(responses),
    )

    context = Mock()
    context.bot_data = {
        "SEARCH_CONFIG": {
            "preferences": {
                "movies": {
                    "codecs": {"x264": 5},
                    "resolutions": {"1080p": 3},
                    "uploaders": {"YTS": 2},
                }
            }
        }
    }

    results = await scraping_service.scrape_yts(
        "Test Movie",
        "movie",
        "https://yts.lt/browse-movies/{query}",
        context,  # Pass the mock object here
        year="2023",
        resolution="1080p",
    )
    assert len(results) == 1
    assert results[0]["source"] == "yts.lt"
    assert results[0]["seeders"] == 25
    assert results[0]["matched_video_formats"] == []
    assert results[0]["matched_audio_formats"] == []
    assert results[0]["matched_audio_channels"] == []
    assert results[0]["is_gold_av"] is False
    assert results[0]["is_silver_av"] is False
    assert results[0]["has_video_match"] is False
    assert results[0]["has_audio_match"] is False


@pytest.mark.asyncio
async def test_scrape_yts_retries_on_validation_failure(caplog, mocker):
    """YTS scraper retries when API validation fails."""
    search_html = """
    <div class="browse-movie-wrap">
      <a class="browse-movie-title" href="https://yts.lt/movies/test-movie">Test Movie</a>
      <div class="browse-movie-year">2023</div>
    </div>
    """
    movie_html = '<div id="movie-info" data-movie-id="1234"></div>'
    bad_api_json = {
        "status": "ok",
        "data": {
            "movie": {
                "title_long": "Test Movie (2023)",
                "year": 2023,
                "torrents": [],  # Missing torrents triggers retry
            }
        },
    }
    good_api_json = {
        "status": "ok",
        "data": {
            "movie": {
                "title_long": "Test Movie (2023)",
                "year": 2023,
                "torrents": [
                    {
                        "quality": "1080p",
                        "type": "WEB",
                        "size_bytes": 1024**3,
                        "hash": "abcdef",
                        "seeds": 25,
                    }
                ],
            }
        },
    }
    responses = [
        DummyResponse(text=search_html),
        DummyResponse(text=movie_html),
        DummyResponse(json_data=bad_api_json),
        DummyResponse(json_data=good_api_json),
    ]
    mocker.patch(
        "telegram_bot.services.scrapers.adapters.create_async_client",
        return_value=DummyClient(responses),
    )
    mocker.patch("asyncio.sleep", new=AsyncMock())

    context = Mock()
    context.bot_data = {
        "SEARCH_CONFIG": {
            "preferences": {
                "movies": {
                    "codecs": {"x264": 5},
                    "resolutions": {"1080p": 3},
                    "uploaders": {"YTS": 2},
                }
            }
        }
    }

    with caplog.at_level(logging.DEBUG):
        results = await scraping_service.scrape_yts(
            "Test Movie",
            "movie",
            "https://yts.lt/browse-movies/{query}",
            context,
            year="2023",
            resolution="1080p",
        )

    assert len(results) == 1
    assert any("attempt 1 failed validation" in m for m in caplog.messages)
    assert any("attempt 2 succeeded" in m for m in caplog.messages)


@pytest.mark.asyncio
async def test_scrape_yts_paginates_browse_pages_to_find_year(mocker):
    """When page 1 has no matching year, the scraper paginates to find older films."""
    # Page 1: no matching movies for the given year
    search_html_p1 = """
    <div class="browse-movie-wrap">
      <a class="browse-movie-title" href="https://yts.lt/movies/alien-xyz">Alien Something</a>
      <div class="browse-movie-year">2003</div>
    </div>
    """
    # Page 2: contains the correct 1979 entry
    search_html_p2 = """
    <div class="browse-movie-wrap">
      <a class="browse-movie-title" href="https://yts.lt/movies/alien-1979">Alien</a>
      <div class="browse-movie-year">1979</div>
    </div>
    """
    movie_html = '<div id="movie-info" data-movie-id="1234"></div>'
    api_json = {
        "status": "ok",
        "data": {
            "movie": {
                "title_long": "Alien (1979)",
                "year": 1979,
                "torrents": [
                    {
                        "quality": "1080p",
                        "type": "WEB",
                        "size_bytes": 1024**3,
                        "hash": "abcdef",
                        "seeds": 25,
                    }
                ],
            }
        },
    }

    responses = [
        DummyResponse(text=search_html_p1),  # browse page 1
        DummyResponse(text=search_html_p2),  # browse page 2
        DummyResponse(text=movie_html),  # movie page
        DummyResponse(json_data=api_json),  # details API
    ]
    mocker.patch(
        "telegram_bot.services.scrapers.adapters.create_async_client",
        return_value=DummyClient(responses),
    )

    context = Mock()
    context.bot_data = {
        "SEARCH_CONFIG": {
            "preferences": {
                "movies": {
                    "codecs": {"x264": 5},
                    "resolutions": {"1080p": 3},
                    "uploaders": {"YTS": 2},
                }
            }
        }
    }

    results = await scraping_service.scrape_yts(
        "Alien",
        "movie",
        "https://yts.lt/browse-movies/{query}",
        context,
        year="1979",
        resolution="1080p",
    )

    assert len(results) == 1
    assert results[0]["source"] == "yts.lt"
    assert results[0]["seeders"] == 25


@pytest.mark.asyncio
async def test_scrape_yts_api_fallback_relaxes_quality(mocker):
    """API fallback tries again without quality when the first pass returns 0."""
    # No browse matches -> triggers API fallback
    search_html = """
    <div class="other"></div>
    """
    # Attempt 1 (year+quality): 0 movies
    api_empty = {"status": "ok", "data": {"movie_count": 0}}
    # Attempt 2 (year only): has one movie with 1080p torrent
    api_with_movie = {
        "status": "ok",
        "data": {
            "movies": [
                {
                    "title_long": "Test Movie (1979)",
                    "year": 1979,
                    "torrents": [
                        {
                            "quality": "1080p",
                            "type": "WEB",
                            "size_bytes": 1024**3,
                            "hash": "abcdef",
                            "seeds": 25,
                        }
                    ],
                }
            ]
        },
    }

    responses = [
        DummyResponse(text=search_html),  # browse page 1 (no choices)
        DummyResponse(text=search_html),  # browse page 2 (still no choices)
        DummyResponse(text=search_html),  # browse page 3
        DummyResponse(text=search_html),  # browse page 4
        DummyResponse(text=search_html),  # browse page 5
        DummyResponse(json_data=api_empty),  # API attempt 1 (year+quality)
        DummyResponse(json_data=api_with_movie),  # API attempt 2 (year only)
    ]
    mocker.patch(
        "telegram_bot.services.scrapers.adapters.create_async_client",
        return_value=DummyClient(responses),
    )

    context = Mock()
    context.bot_data = {
        "SEARCH_CONFIG": {
            "preferences": {
                "movies": {
                    "codecs": {"x264": 5},
                    "resolutions": {"1080p": 3},
                    "uploaders": {"YTS": 2},
                }
            }
        }
    }

    results = await scraping_service.scrape_yts(
        "Test Movie",
        "movie",
        "https://yts.lt/browse-movies/{query}",
        context,
        year="1979",
        resolution="1080p",
    )

    assert len(results) == 1
    assert results[0]["seeders"] == 25
    assert results[0]["source"] == "yts.lt"


@pytest.mark.asyncio
async def test_scrape_yts_api_fallback_relaxes_year(mocker):
    """API fallback eventually drops year param and filters locally by year."""
    # No browse matches -> triggers API fallback
    search_html = '<div class="other"></div>'
    api_empty = {"status": "ok", "data": {"movie_count": 0}}
    # Attempt 3 (no year param): include multiple years, only target year remains
    api_all_years = {
        "status": "ok",
        "data": {
            "movies": [
                {
                    "title_long": "Alien (1979)",
                    "year": 1979,
                    "torrents": [
                        {
                            "quality": "1080p",
                            "type": "WEB",
                            "size_bytes": 1024**3,
                            "hash": "abcd11",
                            "seeds": 25,
                        }
                    ],
                },
                {
                    "title_long": "Alien (2012)",
                    "year": 2012,
                    "torrents": [
                        {
                            "quality": "1080p",
                            "type": "WEB",
                            "size_bytes": 1024**3,
                            "hash": "efgh22",
                            "seeds": 20,
                        }
                    ],
                },
            ]
        },
    }

    responses = [
        DummyResponse(text=search_html),  # browse page 1
        DummyResponse(text=search_html),  # browse page 2
        DummyResponse(text=search_html),  # browse page 3
        DummyResponse(text=search_html),  # browse page 4
        DummyResponse(text=search_html),  # browse page 5
        DummyResponse(json_data=api_empty),  # API attempt 1 (year+quality)
        DummyResponse(json_data=api_empty),  # API attempt 2 (year only)
        DummyResponse(json_data=api_all_years),  # API attempt 3 (no year param)
    ]
    mocker.patch(
        "telegram_bot.services.scrapers.adapters.create_async_client",
        return_value=DummyClient(responses),
    )

    context = Mock()
    context.bot_data = {
        "SEARCH_CONFIG": {
            "preferences": {
                "movies": {
                    "codecs": {"x264": 5},
                    "resolutions": {"1080p": 3},
                    "uploaders": {"YTS": 2},
                }
            }
        }
    }

    results = await scraping_service.scrape_yts(
        "Alien",
        "movie",
        "https://yts.lt/browse-movies/{query}",
        context,
        year="1979",
        resolution="1080p",
    )

    # Only the 1979 entry should remain after local filtering
    assert len(results) == 1
    assert results[0]["title"].startswith("Alien (1979)") or "(1979)" in results[0]["title"]
    assert results[0]["source"] == "yts.lt"


@pytest.mark.asyncio
async def test_scrape_yts_token_gate_avoids_near_homonyms(mocker):
    """With a year present, token gate avoids false matches like 'The Dunes' for 'Dune'."""
    # Page 1 contains 'The Dunes' (fails token gate), then pages 2-5 empty -> fallback
    browse_dunes = """
    <div class="browse-movie-wrap">
      <a class="browse-movie-title" href="https://yts.lt/movies/the-dunes-1979">The Dunes</a>
      <div class="browse-movie-year">1979</div>
    </div>
    """
    browse_empty = '<div class="other"></div>'

    api_with_movie = {
        "status": "ok",
        "data": {
            "movies": [
                {
                    "title_long": "Dune (1979)",
                    "year": 1979,
                    "torrents": [
                        {
                            "quality": "1080p",
                            "type": "WEB",
                            "size_bytes": 1024**3,
                            "hash": "aaaaaa",
                            "seeds": 25,
                        }
                    ],
                }
            ]
        },
    }

    responses = [
        DummyResponse(text=browse_dunes),  # page 1 (gated out)
        DummyResponse(text=browse_empty),  # page 2
        DummyResponse(text=browse_empty),  # page 3
        DummyResponse(text=browse_empty),  # page 4
        DummyResponse(text=browse_empty),  # page 5
        DummyResponse(json_data=api_with_movie),  # API fallback
    ]
    mocker.patch(
        "telegram_bot.services.scrapers.adapters.create_async_client",
        return_value=DummyClient(responses),
    )

    context = Mock()
    context.bot_data = {
        "SEARCH_CONFIG": {
            "preferences": {
                "movies": {
                    "codecs": {"x264": 5},
                    "resolutions": {"1080p": 3},
                    "uploaders": {"YTS": 2},
                }
            }
        }
    }

    results = await scraping_service.scrape_yts(
        "Dune",
        "movie",
        "https://yts.lt/browse-movies/{query}",
        context,
        year="1979",
        resolution="1080p",
    )

    assert len(results) == 1
    assert results[0]["source"] == "yts.lt"


def test_strategy_find_direct_links_magnet():
    html = '<a href="magnet:?xt=urn:btih:123">Magnet</a>'
    soup = BeautifulSoup(html, "html.parser")
    links = scraping_service._strategy_find_direct_links(soup)
    assert links == {"magnet:?xt=urn:btih:123"}


def test_strategy_find_direct_links_torrent():
    html = '<a href="https://example.com/file.torrent">Download</a>'
    soup = BeautifulSoup(html, "html.parser")
    links = scraping_service._strategy_find_direct_links(soup)
    assert links == {"https://example.com/file.torrent"}


def test_strategy_find_direct_links_none():
    html = '<a href="/other">Link</a>'
    soup = BeautifulSoup(html, "html.parser")
    links = scraping_service._strategy_find_direct_links(soup)
    assert links == set()


def test_strategy_contextual_search_keyword():
    html = '<a href="/download/123">Download Torrent</a>'
    soup = BeautifulSoup(html, "html.parser")
    links = scraping_service._strategy_contextual_search(soup, "Query")
    assert "/download/123" in links


def test_strategy_contextual_search_query_match():
    html = '<a href="/details.php?id=456">My Show S01E01 1080p</a>'
    soup = BeautifulSoup(html, "html.parser")
    links = scraping_service._strategy_contextual_search(soup, "My Show")
    assert "/details.php?id=456" in links


def test_strategy_contextual_search_unrelated_keyword():
    html = '<a href="/about">About our download policy</a>'
    soup = BeautifulSoup(html, "html.parser")
    links = scraping_service._strategy_contextual_search(soup, "My Show")
    assert "/about" in links


def test_strategy_find_in_tables_single_match():
    html = '<table><tr><td>My Show</td><td><a href="/dl">Download</a></td></tr></table>'
    soup = BeautifulSoup(html, "html.parser")
    results = scraping_service._strategy_find_in_tables(soup, "My Show")
    assert "/dl" in results


def test_strategy_find_in_tables_multiple_matches():
    html = """
    <table>
      <tr><td>My Show S01E01</td><td><a href="/e1">DL</a></td></tr>
      <tr><td>My Show S01E02</td><td><a href="/e2">DL</a></td></tr>
    </table>
    """
    soup = BeautifulSoup(html, "html.parser")
    results = scraping_service._strategy_find_in_tables(soup, "My Show")
    assert {"/e1", "/e2"}.issubset(results.keys())


def test_strategy_find_in_tables_ignores_unrelated_tables():
    html = """
    <table><tr><td>Other</td><td><a href="/x">X</a></td></tr></table>
    <table><tr><td>My Show</td><td><a href="/dl">Download</a></td></tr></table>
    """
    soup = BeautifulSoup(html, "html.parser")
    results = scraping_service._strategy_find_in_tables(soup, "My Show")
    assert "/dl" in results and "/x" not in results


def test_score_candidate_links_prefers_magnet():
    html = (
        '<div><a href="magnet:?xt=urn:btih:1">Magnet</a></div>'
        '<div><a href="/context">Download Torrent</a></div>'
        '<table><tr><td>My Show</td><td><a href="/table">Link</a></td></tr></table>'
    )
    soup = BeautifulSoup(html, "html.parser")
    links = {"magnet:?xt=urn:btih:1", "/context", "/table"}
    table_links = {"/table": 80.0}
    best = scraping_service._score_candidate_links(links, "My Show", table_links, soup)
    assert best == "magnet:?xt=urn:btih:1"


def test_score_candidate_links_penalizes_ads():
    html = (
        '<div class="ad"><a href="/bad">My Show 1080p</a></div>'
        '<div><a href="/good">My Show 1080p</a></div>'
    )
    soup = BeautifulSoup(html, "html.parser")
    links = {"/bad", "/good"}
    best = scraping_service._score_candidate_links(links, "My Show", {}, soup)
    assert best == "/good"


def test_score_candidate_links_prefers_better_match():
    html = (
        '<div><a href="/high">My Show Episode</a></div><div><a href="/low">Another Show</a></div>'
    )
    soup = BeautifulSoup(html, "html.parser")
    links = {"/high", "/low"}
    best = scraping_service._score_candidate_links(links, "My Show Episode", {}, soup)
    assert best == "/high"
