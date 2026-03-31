from __future__ import annotations

import logging

import pytest

from telegram_bot.services.tracking import tmdb_release_service


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
