from __future__ import annotations

import logging
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest

from telegram_bot.services.tracking import tmdb_release_service


class _FakeResponse:
    def __init__(self, payload: Any, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP status {self.status_code}")


class _FakeAsyncClient:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def get(self, url: str, *, params=None, headers=None) -> _FakeResponse:
        if not self._responses:
            raise RuntimeError(f"Unexpected extra request: {url}")
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_resolve_tmdb_availability_logs_when_credentials_missing(mocker, caplog):
    mocker.patch.dict(
        "os.environ",
        {
            "TMDB_ACCESS_TOKEN": "",
            "TMDB_BEARER_TOKEN": "",
            "TMDB_API_KEY": "",
            "TMDB_REGION": "US",
        },
    )
    caplog.set_level(logging.INFO)

    resolved_date, resolved_source = await tmdb_release_service.resolve_tmdb_availability(
        "Project Hail Mary",
        year=2026,
    )

    assert resolved_date is None
    assert resolved_source is None
    assert "TMDB availability skipped" in caplog.text


@pytest.mark.asyncio
async def test_resolve_tmdb_inferred_year_logs_when_credentials_missing(mocker, caplog):
    mocker.patch.dict(
        "os.environ",
        {
            "TMDB_ACCESS_TOKEN": "",
            "TMDB_BEARER_TOKEN": "",
            "TMDB_API_KEY": "",
            "TMDB_REGION": "US",
        },
    )
    caplog.set_level(logging.INFO)

    inferred_year = await tmdb_release_service.resolve_tmdb_inferred_year(
        "Project Hail Mary",
        year=2026,
    )

    assert inferred_year is None
    assert "TMDB year inference skipped" in caplog.text


def test_extract_tmdb_earliest_streaming_date_prefers_region_then_global():
    payload = {
        "results": [
            {
                "iso_3166_1": "GB",
                "release_dates": [
                    {"type": 4, "release_date": "2026-07-10T00:00:00.000Z"},
                ],
            },
            {
                "iso_3166_1": "US",
                "release_dates": [
                    {"type": 5, "release_date": "2026-05-01T00:00:00.000Z"},
                    {"type": 4, "release_date": "2026-06-01T00:00:00.000Z"},
                ],
            },
        ]
    }

    resolved = tmdb_release_service._extract_tmdb_earliest_streaming_date(payload, region="US")

    assert resolved is not None
    assert resolved.isoformat() == "2026-06-01"


def test_extract_tmdb_web_earliest_streaming_date_prefers_region_then_global():
    html = """
    <section class="panel">
      <table class="card releases">
        <thead>
          <tr><th colspan="5"><h2 id="GB" class="release">United Kingdom</h2></th></tr>
        </thead>
        <tbody>
          <tr><td>04/10/2026</td><td>15</td><td>Digital</td><td></td><td></td></tr>
        </tbody>
      </table>
      <table class="card releases">
        <thead>
          <tr><th colspan="5"><h2 id="US" class="release">United States</h2></th></tr>
        </thead>
        <tbody>
          <tr><td>02/27/2026</td><td>R</td><td>Theatrical</td><td></td><td></td></tr>
          <tr><td>03/31/2026</td><td>R</td><td>Digital</td><td></td><td></td></tr>
        </tbody>
      </table>
    </section>
    """

    us_date = tmdb_release_service._extract_tmdb_web_earliest_streaming_date(html, region="US")
    ca_date = tmdb_release_service._extract_tmdb_web_earliest_streaming_date(html, region="CA")

    assert us_date == date(2026, 3, 31)
    assert ca_date == date(2026, 3, 31)


@pytest.mark.asyncio
async def test_resolve_tmdb_streaming_release_date_logs_when_credentials_missing(mocker, caplog):
    mocker.patch.dict(
        "os.environ",
        {
            "TMDB_ACCESS_TOKEN": "",
            "TMDB_BEARER_TOKEN": "",
            "TMDB_API_KEY": "",
            "TMDB_REGION": "US",
        },
    )
    caplog.set_level(logging.INFO)

    resolved_date = await tmdb_release_service.resolve_tmdb_streaming_release_date(
        "Project Hail Mary",
        year=2026,
    )

    assert resolved_date is None
    assert "TMDB streaming lookup skipped" in caplog.text


@pytest.mark.asyncio
async def test_resolve_tmdb_streaming_release_date_uses_webpage_fallback_when_api_has_no_digital(
    mocker,
):
    mocker.patch.dict(
        "os.environ",
        {
            "TMDB_ACCESS_TOKEN": "",
            "TMDB_BEARER_TOKEN": "",
            "TMDB_API_KEY": "tmdb-test-key",
            "TMDB_REGION": "US",
        },
    )
    fake_client = _FakeAsyncClient(
        [
            _FakeResponse(
                {
                    "results": [
                        {
                            "id": 1159559,
                            "title": "Scream 7",
                            "release_date": "2026-02-25",
                        }
                    ]
                }
            ),
            _FakeResponse(
                {
                    "results": [
                        {
                            "iso_3166_1": "US",
                            "release_dates": [
                                {"type": 3, "release_date": "2026-02-27T00:00:00.000Z"},
                            ],
                        }
                    ]
                }
            ),
        ]
    )
    mocker.patch(
        "telegram_bot.services.tracking.tmdb_release_service.httpx.AsyncClient",
        return_value=fake_client,
    )
    fallback_mock = mocker.patch(
        "telegram_bot.services.tracking.tmdb_release_service._resolve_tmdb_streaming_date_from_release_page",
        new=AsyncMock(return_value=date(2026, 3, 31)),
    )

    resolved_date = await tmdb_release_service.resolve_tmdb_streaming_release_date(
        "Scream 7",
        year=2026,
    )

    assert resolved_date == date(2026, 3, 31)
    fallback_mock.assert_awaited_once_with(1159559, region="US")
