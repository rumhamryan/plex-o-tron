import pytest

from telegram_bot.services.scrapers.wikipedia import (
    fetch_movie_years_from_wikipedia,
)


class _DummyWikiPage:
    def __init__(self, title: str, html: str = "") -> None:
        self.title = title
        self._html = html

    def html(self) -> str:
        return self._html


@pytest.mark.asyncio
async def test_fetch_movie_years_supports_exact_title_without_film_suffix(mocker):
    mocker.patch(
        "wikipedia.search",
        return_value=["The Matrix", "The Matrix Reloaded"],
    )
    mocker.patch(
        "wikipedia.summary",
        return_value="The Matrix is a 1999 science fiction action film.",
    )

    def _fake_page(title: str, auto_suggest: bool = False, redirect: bool = True):
        if title.endswith("(disambiguation)"):
            html = """
            <html><body>
            <a href="/wiki/The_Matrix" title="The Matrix">The Matrix (1999 film)</a>
            <a href="/wiki/Matrix" title="Matrix">Matrix (mathematics)</a>
            </body></html>
            """
            return _DummyWikiPage(title, html)
        return _DummyWikiPage("The Matrix")

    mocker.patch("wikipedia.page", side_effect=_fake_page)

    years, corrected = await fetch_movie_years_from_wikipedia("The Matrix")

    assert years == [1999]
    assert corrected in (None, "The Matrix")


@pytest.mark.asyncio
async def test_fetch_movie_years_handles_titles_with_punctuation(mocker):
    mocker.patch(
        "wikipedia.search",
        return_value=["Jumanji: The Next Level"],
    )
    mocker.patch(
        "wikipedia.summary",
        return_value="Jumanji: The Next Level is a 2019 fantasy adventure film.",
    )
    mocker.patch(
        "wikipedia.page", return_value=_DummyWikiPage("Jumanji: The Next Level")
    )

    years, corrected = await fetch_movie_years_from_wikipedia("Jumanji The Next Level")

    assert years == [2019]
    assert corrected == "Jumanji: The Next Level"
