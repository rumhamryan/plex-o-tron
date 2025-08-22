"""Minimal stubs for the :mod:`libtorrent` package used in tests.

These classes only provide the attributes accessed by the application code so
that static type checkers (pyright) can understand the interface.
They are *not* functional implementations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class file_storage:
    """Represents the files within a torrent."""

    def num_files(self) -> int:
        ...

    def file_size(self, index: int) -> int:
        ...

    def file_path(self, index: int) -> str:
        ...


@dataclass
class _InfoHashes:
    """Container for the v1 info hash."""

    v1: str = ""


class torrent_info:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        ...

    def files(self) -> file_storage:
        ...

    def total_size(self) -> int:
        ...

    def name(self) -> str:
        ...

    def info_hashes(self) -> _InfoHashes:
        ...


class add_torrent_params:
    save_path: str
    upload_mode: bool

    def __init__(self) -> None:
        ...


class torrent_status:
    has_metadata: bool
    progress: float
    download_rate: int
    state: Any
    num_peers: int

    def __init__(self) -> None:
        ...


class torrent_handle:
    def status(self) -> torrent_status:
        ...

    def torrent_file(self) -> torrent_info:
        ...

    def is_valid(self) -> bool:
        ...


class session:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        ...

    def add_torrent(self, params: add_torrent_params) -> torrent_handle:
        ...

    def remove_torrent(self, handle: torrent_handle) -> None:
        ...


def parse_magnet_uri(_uri: str) -> add_torrent_params:
    ...


def bdecode(_data: bytes) -> Any:
    ...


def bencode(_data: Any) -> bytes:
    ...


class create_torrent:
    def __init__(self, _ti: torrent_info) -> None:
        ...

    def generate(self) -> Any:
        ...


def add_magnet_uri(*args: Any, **kwargs: Any) -> None:
    ...
