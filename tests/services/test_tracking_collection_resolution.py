from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest

from telegram_bot.services.tracking import collection_resolution


@pytest.mark.asyncio
async def test_resolve_collection_tracking_candidates_returns_none_when_no_match(mocker):
    mocker.patch(
        "telegram_bot.services.tracking.collection_resolution.scraping_service.fetch_movie_franchise_details",
        AsyncMock(return_value=None),
    )

    resolved = await collection_resolution.resolve_collection_tracking_candidates(
        "Unknown Collection"
    )

    assert resolved is None


@pytest.mark.asyncio
async def test_resolve_collection_tracking_candidates_filters_streaming_released_and_past_unknown(
    mocker,
):
    mocker.patch(
        "telegram_bot.services.tracking.collection_resolution.scraping_service.fetch_movie_franchise_details",
        AsyncMock(
            return_value=(
                "Avatar Franchise",
                [
                    {"title": "Avatar (2009 film)", "year": 2009},
                    {"title": "Avatar 3", "year": 2027},
                    {"title": "Avatar 4", "year": 2029},
                    {"title": "Avatar Legacy", "year": 2015},
                    {"title": "Avatar 3", "year": 2027},
                ],
            )
        ),
    )
    streaming_mock = mocker.patch(
        "telegram_bot.services.tracking.collection_resolution.tmdb_release_service.resolve_tmdb_streaming_release_date",
        AsyncMock(
            side_effect=[
                date(2009, 4, 22),
                date(2027, 12, 18),
                None,
                None,
            ]
        ),
    )

    resolved = await collection_resolution.resolve_collection_tracking_candidates(
        "Avatar Collection",
        today=date(2026, 3, 31),
    )

    assert resolved is not None
    assert resolved["collection_name"] == "Avatar"
    assert resolved["total_titles"] == 4
    assert resolved["skipped_released_streaming"] == 1
    assert resolved["skipped_past_year_unknown_streaming"] == 1
    assert len(resolved["candidates"]) == 2
    assert streaming_mock.await_count == 4

    first, second = resolved["candidates"]
    assert first["canonical_title"] == "Avatar 3"
    assert first["availability_date"] == date(2027, 12, 18)
    assert first["availability_source"] == "streaming"
    assert second["canonical_title"] == "Avatar 4"
    assert second["availability_date"] is None
    assert second["availability_source"] is None
