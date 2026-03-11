from __future__ import annotations

from typing import Any

from telegram_bot.services.interfaces import (
    PlexClient,
    PlexLibrary,
    PlexLibrarySection,
    PlexMediaItem,
    ScraperFunction,
    TorrentSession,
)


class DummyMediaItem:
    title = "Item"
    year: int | None = 2020

    def add_collection(self, name: str) -> None:
        return None

    addCollection = add_collection


class DummyLibrarySection:
    def search(self, **params: Any) -> list[PlexMediaItem]:
        return [DummyMediaItem()]

    def update(self) -> None:
        return None


class DummyLibrary:
    def section(self, name: str) -> PlexLibrarySection:
        return DummyLibrarySection()


class DummyPlexClient:
    version = "1.0"
    platform = "dummy"
    library = DummyLibrary()


class DummyTorrentSession:
    def add_torrent(self, params: Any) -> object:
        return object()

    def remove_torrent(self, handle: Any, options: Any | None = None) -> None:
        return None


async def dummy_scraper(
    search_query: str, media_type: str, site_url: str, context: Any, **extra_kwargs: Any
) -> list[dict[str, Any]]:
    return []


def test_plex_client_protocol_runtime_check() -> None:
    assert isinstance(DummyPlexClient(), PlexClient)
    assert isinstance(DummyPlexClient().library, PlexLibrary)
    assert isinstance(DummyPlexClient().library.section("Movies"), PlexLibrarySection)


def test_torrent_session_protocol_runtime_check() -> None:
    assert isinstance(DummyTorrentSession(), TorrentSession)


def test_scraper_function_protocol_runtime_check() -> None:
    assert isinstance(dummy_scraper, ScraperFunction)
