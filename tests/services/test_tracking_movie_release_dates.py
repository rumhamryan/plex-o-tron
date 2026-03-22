from __future__ import annotations

import json
from datetime import date
from unittest.mock import AsyncMock

import pytest

from telegram_bot.services.tracking import movie_release_dates


def test_extract_earliest_availability_from_html_ignores_theatrical_release_rows():
    html = """
    <html><body>
      <table class="infobox vevent">
        <tr>
          <th>Release dates</th>
          <td>March 1, 2026 (2026-03-01) (United States)</td>
        </tr>
      </table>
    </body></html>
    """

    resolved_date, resolved_source = movie_release_dates._extract_earliest_availability_from_html(
        html
    )

    assert resolved_date is None
    assert resolved_source is None


def test_extract_earliest_availability_from_html_prefers_earliest_streaming_or_physical():
    html = """
    <html><body>
      <table class="infobox vevent">
        <tr>
          <th>Digital release</th>
          <td>May 20, 2026 (2026-05-20)</td>
        </tr>
        <tr>
          <th>Home media</th>
          <td>June 10, 2026 (2026-06-10)</td>
        </tr>
      </table>
    </body></html>
    """

    resolved_date, resolved_source = movie_release_dates._extract_earliest_availability_from_html(
        html
    )

    assert resolved_date == date(2026, 5, 20)
    assert resolved_source == "streaming"


def test_extract_tmdb_earliest_availability_uses_digital_physical_types_only():
    payload = {
        "results": [
            {
                "iso_3166_1": "US",
                "release_dates": [
                    {"type": 3, "release_date": "2026-04-01T00:00:00.000Z"},  # theatrical
                    {"type": 4, "release_date": "2026-05-20T00:00:00.000Z"},  # digital
                    {"type": 5, "release_date": "2026-06-10T00:00:00.000Z"},  # physical
                ],
            }
        ]
    }

    resolved_date, resolved_source = movie_release_dates._extract_tmdb_earliest_availability(
        payload,
        region="US",
    )

    assert resolved_date == date(2026, 5, 20)
    assert resolved_source == "streaming"


@pytest.mark.asyncio
async def test_resolve_movie_tracking_target_uses_tmdb_when_wikipedia_has_no_availability(mocker):
    wiki_html = """
    <html><body>
      <table class="infobox vevent">
        <tr>
          <th>Release dates</th>
          <td>March 1, 2026 (2026-03-01) (United States)</td>
        </tr>
      </table>
    </body></html>
    """
    mocker.patch(
        "telegram_bot.services.tracking.movie_release_dates._resolve_movie_page_html",
        AsyncMock(return_value=(wiki_html, "Future Movie")),
    )
    tmdb_mock = mocker.patch(
        "telegram_bot.services.tracking.movie_release_dates._resolve_tmdb_availability",
        AsyncMock(return_value=(date(2026, 5, 20), "streaming")),
    )

    resolved = await movie_release_dates.resolve_movie_tracking_target(
        "Future Movie",
        year=2026,
        today=date(2026, 3, 21),
    )

    assert tmdb_mock.await_count >= 1
    assert resolved["release_date_status"] == "confirmed"
    assert resolved["availability_date"] == date(2026, 5, 20)
    assert resolved["availability_source"] == "streaming"
    assert resolved["is_released"] is False


@pytest.mark.asyncio
async def test_resolve_movie_tracking_target_uses_wikipedia_when_tmdb_unavailable(mocker):
    wiki_html = """
    <html><body>
      <table class="infobox vevent">
        <tr>
          <th>Home media</th>
          <td>June 10, 2026 (2026-06-10)</td>
        </tr>
      </table>
    </body></html>
    """
    mocker.patch(
        "telegram_bot.services.tracking.movie_release_dates._resolve_movie_page_html",
        AsyncMock(return_value=(wiki_html, "Future Movie")),
    )
    mocker.patch(
        "telegram_bot.services.tracking.movie_release_dates._resolve_tmdb_availability",
        AsyncMock(return_value=(None, None)),
    )

    resolved = await movie_release_dates.resolve_movie_tracking_target(
        "Future Movie",
        year=2026,
        today=date(2026, 3, 21),
    )

    assert resolved["release_date_status"] == "confirmed"
    assert resolved["availability_date"] == date(2026, 6, 10)
    assert resolved["availability_source"] == "physical"
    assert resolved["is_released"] is False


@pytest.mark.asyncio
async def test_resolve_movie_tracking_target_uses_manual_override_without_tmdb(mocker, tmp_path):
    overrides_path = tmp_path / "tracking_release_overrides.json"
    overrides_path.write_text(
        json.dumps(
            {
                "movies": [
                    {
                        "title": "Future Movie",
                        "year": 2026,
                        "availability_date": "2026-04-07",
                        "availability_source": "streaming",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    mocker.patch.dict(
        "os.environ",
        {"TRACKING_RELEASE_OVERRIDES_FILE": str(overrides_path)},
    )
    mocker.patch(
        "telegram_bot.services.tracking.movie_release_dates._resolve_movie_page_html",
        AsyncMock(return_value=(None, "Future Movie")),
    )
    tmdb_mock = mocker.patch(
        "telegram_bot.services.tracking.movie_release_dates._resolve_tmdb_availability",
        AsyncMock(return_value=(None, None)),
    )

    resolved = await movie_release_dates.resolve_movie_tracking_target(
        "Future Movie",
        year=2026,
        today=date(2026, 3, 21),
    )

    assert tmdb_mock.await_count == 0
    assert resolved["release_date_status"] == "confirmed"
    assert resolved["availability_date"] == date(2026, 4, 7)
    assert resolved["availability_source"] == "streaming"
    assert resolved["is_released"] is False


@pytest.mark.asyncio
async def test_resolve_movie_tracking_target_uses_manual_title_only_override(mocker, tmp_path):
    overrides_path = tmp_path / "tracking_release_overrides.json"
    overrides_path.write_text(
        json.dumps(
            {
                "movies": [
                    {
                        "title": "Future Movie",
                        "availability_date": "2026-04-21",
                        "availability_source": "physical",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    mocker.patch.dict(
        "os.environ",
        {"TRACKING_RELEASE_OVERRIDES_FILE": str(overrides_path)},
    )
    mocker.patch(
        "telegram_bot.services.tracking.movie_release_dates._resolve_movie_page_html",
        AsyncMock(return_value=(None, "Future Movie")),
    )
    tmdb_mock = mocker.patch(
        "telegram_bot.services.tracking.movie_release_dates._resolve_tmdb_availability",
        AsyncMock(return_value=(None, None)),
    )

    resolved = await movie_release_dates.resolve_movie_tracking_target(
        "Future Movie",
        year=2031,
        today=date(2026, 3, 21),
    )

    assert tmdb_mock.await_count == 0
    assert resolved["release_date_status"] == "confirmed"
    assert resolved["availability_date"] == date(2026, 4, 21)
    assert resolved["availability_source"] == "physical"
    assert resolved["is_released"] is False


@pytest.mark.asyncio
async def test_resolve_movie_tracking_target_unknown_year_uses_tmdb_inferred_year_for_release_state(
    mocker,
):
    mocker.patch(
        "telegram_bot.services.tracking.movie_release_dates._resolve_movie_page_html",
        AsyncMock(return_value=(None, "Project Hailmary")),
    )
    mocker.patch(
        "telegram_bot.services.tracking.movie_release_dates._resolve_tmdb_availability",
        AsyncMock(return_value=(None, None)),
    )
    infer_mock = mocker.patch(
        "telegram_bot.services.tracking.movie_release_dates._resolve_tmdb_inferred_year",
        AsyncMock(return_value=2026),
    )

    resolved = await movie_release_dates.resolve_movie_tracking_target(
        "Project Hailmary",
        year=None,
        today=date(2026, 3, 21),
    )

    assert infer_mock.await_count >= 1
    assert resolved["year"] == 2026
    assert resolved["release_date_status"] == "unknown"
    assert resolved["availability_date"] is None
    assert resolved["is_released"] is False


@pytest.mark.asyncio
async def test_resolve_movie_tracking_target_unknown_year_with_old_tmdb_year_is_released(mocker):
    mocker.patch(
        "telegram_bot.services.tracking.movie_release_dates._resolve_movie_page_html",
        AsyncMock(return_value=(None, "Older Film")),
    )
    mocker.patch(
        "telegram_bot.services.tracking.movie_release_dates._resolve_tmdb_availability",
        AsyncMock(return_value=(None, None)),
    )
    mocker.patch(
        "telegram_bot.services.tracking.movie_release_dates._resolve_tmdb_inferred_year",
        AsyncMock(return_value=2019),
    )

    resolved = await movie_release_dates.resolve_movie_tracking_target(
        "Older Film",
        year=None,
        today=date(2026, 3, 21),
    )

    assert resolved["year"] == 2019
    assert resolved["release_date_status"] == "unknown"
    assert resolved["availability_date"] is None
    assert resolved["is_released"] is True


@pytest.mark.asyncio
async def test_find_movie_tracking_candidates_requires_wikipedia_match(mocker):
    mocker.patch(
        "telegram_bot.services.tracking.movie_release_dates.scraping_service.fetch_movie_years_from_wikipedia",
        AsyncMock(return_value=([], None)),
    )
    resolve_mock = mocker.patch(
        "telegram_bot.services.tracking.movie_release_dates.resolve_movie_tracking_target",
        AsyncMock(),
    )

    candidates = await movie_release_dates.find_movie_tracking_candidates("User Input")

    assert candidates == []
    assert resolve_mock.await_count == 0


@pytest.mark.asyncio
async def test_find_movie_tracking_candidates_uses_wikipedia_canonical_when_year_missing(mocker):
    mocker.patch(
        "telegram_bot.services.tracking.movie_release_dates.scraping_service.fetch_movie_years_from_wikipedia",
        AsyncMock(return_value=([], "Project Hail Mary")),
    )
    resolve_mock = mocker.patch(
        "telegram_bot.services.tracking.movie_release_dates.resolve_movie_tracking_target",
        AsyncMock(
            return_value={
                "title": "Project Hail Mary",
                "canonical_title": "Project Hail Mary",
                "year": None,
                "is_released": False,
                "release_date_status": "unknown",
                "availability_date": None,
                "availability_source": None,
            }
        ),
    )

    candidates = await movie_release_dates.find_movie_tracking_candidates("Project Hailmary")

    assert resolve_mock.await_count == 1
    assert resolve_mock.await_args_list[0].args[0] == "Project Hail Mary"
    assert len(candidates) == 1
    assert candidates[0]["canonical_title"] == "Project Hail Mary"
