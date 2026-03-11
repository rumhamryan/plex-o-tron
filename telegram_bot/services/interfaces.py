# telegram_bot/services/interfaces.py

from __future__ import annotations

from collections.abc import Callable, Coroutine, Sequence
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "PlexClient",
    "PlexClientFactory",
    "PlexLibrary",
    "PlexLibrarySection",
    "PlexMediaItem",
    "ScraperFunction",
    "TorrentSession",
]


@runtime_checkable
class PlexMediaItem(Protocol):
    title: str
    year: int | None

    def addCollection(self, name: str) -> Any: ...


@runtime_checkable
class PlexLibrarySection(Protocol):
    def search(self, **params: Any) -> Sequence[PlexMediaItem]: ...

    def update(self) -> Any: ...


@runtime_checkable
class PlexLibrary(Protocol):
    def section(self, name: str) -> PlexLibrarySection: ...


@runtime_checkable
class PlexClient(Protocol):
    version: str
    platform: str
    library: PlexLibrary


PlexClientFactory = Callable[[str, str], PlexClient]


@runtime_checkable
class TorrentSession(Protocol):
    def add_torrent(self, params: Any) -> Any: ...

    def remove_torrent(self, handle: Any, options: Any | None = None) -> None: ...


@runtime_checkable
class ScraperFunction(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> Coroutine[Any, Any, list[dict[str, Any]]]: ...
