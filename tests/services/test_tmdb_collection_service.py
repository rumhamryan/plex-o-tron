from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from telegram_bot.services import tmdb_collection_service


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
async def test_resolve_tmdb_collection_from_movie_returns_credentials_missing(mocker):
    mocker.patch.dict(
        "os.environ",
        {
            "TMDB_ACCESS_TOKEN": "",
            "TMDB_BEARER_TOKEN": "",
            "TMDB_API_KEY": "",
            "TMDB_REGION": "US",
        },
    )

    result = await tmdb_collection_service.resolve_tmdb_collection_from_movie("Batman Begins")

    assert result["status"] == "credentials_missing"
    assert result["matched_movie"] is None
    assert result["collection"] is None


def test_choose_tmdb_search_result_prefers_exact_match_with_collection_signal():
    raw_results = [
        {
            "id": 1,
            "title": "Batman Begins",
            "release_date": "2005-06-10",
            "popularity": 20,
            "belongs_to_collection": {"id": 123},
        },
        {
            "id": 2,
            "title": "Batman",
            "release_date": "1989-06-23",
            "popularity": 95,
            "belongs_to_collection": {"id": 999},
        },
    ]

    selected = tmdb_collection_service._choose_tmdb_search_result(
        "Batman Begins",
        year=2005,
        raw_results=raw_results,
    )

    assert isinstance(selected, dict)
    assert selected["id"] == 1


def test_build_collection_summary_sorts_and_classifies_releases():
    payload = {
        "id": 100,
        "name": "Sample Collection",
        "parts": [
            {"id": 2, "title": "Future Film", "release_date": "2027-01-01"},
            {"id": 4, "title": "No Date Film"},
            {"id": 1, "title": "Past Film", "release_date": "2020-05-01"},
            {"id": 3, "title": "Current Year Unknown Day", "release_date": "2026-00-00"},
        ],
    }

    summary = tmdb_collection_service._build_collection_summary(
        payload,
        reference_day=date(2026, 3, 22),
    )

    assert summary is not None
    assert [movie["title"] for movie in summary["movies"]] == [
        "Past Film",
        "Future Film",
        "Current Year Unknown Day",
        "No Date Film",
    ]
    assert summary["released_count"] == 1
    assert summary["upcoming_count"] == 1
    assert summary["unknown_count"] == 2


@pytest.mark.asyncio
async def test_resolve_tmdb_collection_from_movie_returns_movie_without_collection(mocker):
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
                            "id": 10,
                            "title": "Standalone Film",
                            "release_date": "2024-01-01",
                        }
                    ]
                }
            ),
            _FakeResponse(
                {
                    "id": 10,
                    "title": "Standalone Film",
                    "release_date": "2024-01-01",
                    "belongs_to_collection": None,
                }
            ),
        ]
    )
    mocker.patch(
        "telegram_bot.services.tmdb_collection_service.httpx.AsyncClient",
        return_value=fake_client,
    )

    result = await tmdb_collection_service.resolve_tmdb_collection_from_movie(
        "Standalone Film",
        year=2024,
    )

    assert result["status"] == "movie_without_collection"
    assert result["matched_movie"] is not None
    assert result["matched_movie"]["title"] == "Standalone Film"
    assert result["collection"] is None


@pytest.mark.asyncio
async def test_resolve_tmdb_collection_from_movie_successfully_returns_collection_details(mocker):
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
                            "id": 11,
                            "title": "Saga Part One",
                            "release_date": "2022-04-01",
                        }
                    ]
                }
            ),
            _FakeResponse(
                {
                    "id": 11,
                    "title": "Saga Part One",
                    "release_date": "2022-04-01",
                    "belongs_to_collection": {"id": 777},
                }
            ),
            _FakeResponse(
                {
                    "id": 777,
                    "name": "Saga Collection",
                    "parts": [
                        {"id": 12, "title": "Saga Part Two", "release_date": "2026-12-01"},
                        {"id": 11, "title": "Saga Part One", "release_date": "2022-04-01"},
                    ],
                }
            ),
        ]
    )
    mocker.patch(
        "telegram_bot.services.tmdb_collection_service.httpx.AsyncClient",
        return_value=fake_client,
    )

    result = await tmdb_collection_service.resolve_tmdb_collection_from_movie(
        "Saga Part One",
        year=2022,
        reference_day=date(2026, 3, 22),
    )

    assert result["status"] == "ok"
    assert result["matched_movie"] is not None
    collection = result["collection"]
    assert collection is not None
    assert collection["name"] == "Saga Collection"
    assert collection["movie_count"] == 2
    assert collection["released_count"] == 1
    assert collection["upcoming_count"] == 1
    assert [movie["title"] for movie in collection["movies"]] == ["Saga Part One", "Saga Part Two"]
