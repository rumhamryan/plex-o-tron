from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest

from telegram_bot.services.tracking import tv_next_episode


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


def _patch_async_client(mocker, responses: list[_FakeResponse]) -> AsyncMock:
    client = AsyncMock()
    client.get = AsyncMock(side_effect=responses)

    client_cm = AsyncMock()
    client_cm.__aenter__.return_value = client
    client_cm.__aexit__.return_value = False

    mocker.patch(
        "telegram_bot.services.tracking.tv_next_episode.httpx.AsyncClient",
        return_value=client_cm,
    )
    return client


@pytest.mark.asyncio
async def test_find_tv_tracking_candidates_uses_details_next_episode_air_date_when_search_omits_it(
    mocker,
):
    mocker.patch(
        "telegram_bot.services.tracking.tv_next_episode._get_tmdb_auth",
        return_value=({}, {"api_key": "dummy"}),
    )
    client = _patch_async_client(
        mocker,
        responses=[
            _FakeResponse(
                {
                    "results": [
                        {
                            "id": 60625,
                            "name": "Rick and Morty",
                            "first_air_date": "2013-12-02",
                        }
                    ]
                }
            ),
            _FakeResponse(
                {
                    "id": 60625,
                    "name": "Rick and Morty",
                    "next_episode_to_air": {"air_date": "2026-05-31"},
                }
            ),
        ],
    )

    candidates = await tv_next_episode.find_tv_tracking_candidates("Rick and Morty", limit=1)

    assert len(candidates) == 1
    assert candidates[0]["canonical_title"] == "Rick and Morty"
    assert candidates[0]["first_air_date"] == date(2013, 12, 2)
    assert candidates[0]["next_air_date"] == date(2026, 5, 31)
    assert client.get.await_count == 2
    assert client.get.await_args_list[1].args[0].endswith("/tv/60625")


@pytest.mark.asyncio
async def test_find_tv_tracking_candidates_skips_details_lookup_when_search_has_next_air_date(
    mocker,
):
    mocker.patch(
        "telegram_bot.services.tracking.tv_next_episode._get_tmdb_auth",
        return_value=({}, {"api_key": "dummy"}),
    )
    client = _patch_async_client(
        mocker,
        responses=[
            _FakeResponse(
                {
                    "results": [
                        {
                            "id": 60625,
                            "name": "Rick and Morty",
                            "first_air_date": "2013-12-02",
                            "next_air_date": "2026-06-14",
                        }
                    ]
                }
            )
        ],
    )

    candidates = await tv_next_episode.find_tv_tracking_candidates("Rick and Morty", limit=1)

    assert len(candidates) == 1
    assert candidates[0]["next_air_date"] == date(2026, 6, 14)
    assert client.get.await_count == 1
