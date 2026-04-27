from __future__ import annotations

import pytest

from telegram_bot.services.discovery import CircuitBreaker, DiscoveryRequest, DiscoveryResult
from telegram_bot.services.discovery.exceptions import ProviderSearchError
from telegram_bot.services.discovery.orchestrator import DiscoveryOrchestrator, PROVIDER_FACTORY
from telegram_bot.services.discovery.providers.base import BaseProvider


def _result(
    title: str,
    *,
    seeders: int = 25,
    leechers: int = 0,
    size_gib: float = 1.0,
    info_hash: str | None = None,
    magnet_url: str | None = None,
    source: str = "fake",
) -> DiscoveryResult:
    return DiscoveryResult(
        title=title,
        download_url=magnet_url or f"magnet:?xt=urn:btih:{info_hash or 'HASH'}&dn=Example",
        source=source,
        size_bytes=int(size_gib * 1024**3),
        seeders=seeders,
        leechers=leechers,
        info_hash=info_hash,
        magnet_url=magnet_url,
        uploader="trusted",
        codec="x265",
        year=2024,
    )


class FakeProvider(BaseProvider):
    calls: dict[str, int] = {}
    responses: dict[str, list[DiscoveryResult] | Exception] = {}

    async def search(self, request: DiscoveryRequest) -> list[DiscoveryResult]:
        self.calls[self.config.name] = self.calls.get(self.config.name, 0) + 1
        response = self.responses[self.config.name]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(autouse=True)
def _register_fake_provider(monkeypatch):
    FakeProvider.calls = {}
    FakeProvider.responses = {}
    monkeypatch.setitem(PROVIDER_FACTORY, "fake", FakeProvider)


def test_circuit_breaker_marks_provider_offline_until_cooldown_expires() -> None:
    now = 1_000.0
    breaker = CircuitBreaker(clock=lambda: now)

    for _ in range(CircuitBreaker.FAILURE_THRESHOLD):
        breaker.record_failure("indexer")

    assert breaker.is_healthy("indexer") is False

    now += CircuitBreaker.COOLDOWN_SECONDS

    assert breaker.is_healthy("indexer") is True


@pytest.mark.asyncio
async def test_orchestrator_records_provider_failures_and_skips_cooling_provider() -> None:
    FakeProvider.responses = {
        "bad": ProviderSearchError("timeout", provider_name="bad"),
        "good": [_result("Great Movie 1080p x265", source="good")],
    }
    orchestrator = DiscoveryOrchestrator(
        [
            {"name": "bad", "type": "fake", "search_url": "https://bad.example"},
            {"name": "good", "type": "fake", "search_url": "https://good.example"},
        ],
        preferences={"movies": {"codecs": {"x265": 10}, "uploaders": {"trusted": 20}}},
    )
    request = DiscoveryRequest(query="Great Movie", media_type="movie")

    for _ in range(CircuitBreaker.FAILURE_THRESHOLD):
        results = await orchestrator.search(request)
        assert len(results) == 1

    assert FakeProvider.calls["bad"] == CircuitBreaker.FAILURE_THRESHOLD

    results = await orchestrator.search(request)

    assert len(results) == 1
    assert FakeProvider.calls["bad"] == CircuitBreaker.FAILURE_THRESHOLD
    assert FakeProvider.calls["good"] == CircuitBreaker.FAILURE_THRESHOLD + 1


@pytest.mark.asyncio
async def test_orchestrator_deduplicates_by_info_hash_and_keeps_more_seeded_result() -> None:
    FakeProvider.responses = {
        "fake": [
            _result("Movie 1080p x265 low", seeders=20, info_hash="ABC123"),
            _result("Movie 1080p x265 high", seeders=80, info_hash="abc123"),
        ],
    }
    orchestrator = DiscoveryOrchestrator(
        [{"name": "fake", "type": "fake", "search_url": "https://fake.example"}],
        preferences={"codecs": {"x265": 1}, "resolutions": {"1080p": 1}},
    )

    results = await orchestrator.search(DiscoveryRequest(query="Movie", media_type="movie"))

    assert len(results) == 1
    assert results[0]["title"] == "Movie 1080p x265 high"


@pytest.mark.asyncio
async def test_orchestrator_filters_seed_size_and_movie_screeners() -> None:
    FakeProvider.responses = {
        "fake": [
            _result("Movie 1080p x265 low seeds", seeders=4),
            _result("Movie 1080p x265 too large", seeders=30, size_gib=10),
            _result("Movie 1080p WEBSCREENER x265", seeders=30),
            _result("Movie 1080p WEB-DL x265", seeders=30),
        ],
    }
    orchestrator = DiscoveryOrchestrator(
        [{"name": "fake", "type": "fake", "search_url": "https://fake.example"}],
        preferences={"codecs": {"x265": 10}, "resolutions": {"1080p": 5}},
    )
    request = DiscoveryRequest(
        query="Movie",
        media_type="movie",
        min_seeders=20,
        max_size_gib=5,
    )

    results = await orchestrator.search(request)

    assert len(results) == 1
    assert results[0]["title"] == "Movie 1080p WEB-DL x265"


@pytest.mark.asyncio
async def test_orchestrator_scores_with_preferences_and_adds_av_metadata() -> None:
    FakeProvider.responses = {
        "fake": [
            _result("Movie 1080p x264", seeders=30),
            _result("Movie 2160p DoVi Atmos 7.1 x265", seeders=30),
        ],
    }
    orchestrator = DiscoveryOrchestrator(
        [{"name": "fake", "type": "fake", "search_url": "https://fake.example"}],
        preferences={
            "movies": {
                "codecs": {"x265": 4},
                "resolutions": {"2160p": 6},
                "video_formats": {"dolby_vision": 8},
                "audio_formats": {"atmos": 8},
                "audio_channels": {"7.1": 3},
            }
        },
    )

    results = await orchestrator.search(DiscoveryRequest(query="Movie", media_type="movie"))

    assert results[0]["title"] == "Movie 2160p DoVi Atmos 7.1 x265"
    assert results[0]["matched_video_formats"] == ["dolby_vision"]
    assert results[0]["matched_audio_formats"] == ["atmos"]
    assert results[0]["matched_audio_channels"] == ["7.1"]
    assert results[0]["is_gold_av"] is True
