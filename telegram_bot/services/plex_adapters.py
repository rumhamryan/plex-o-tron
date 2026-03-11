# telegram_bot/services/plex_adapters.py

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence

from plexapi.server import PlexServer

from .interfaces import PlexClient, PlexClientFactory


def create_plex_client(
    url: str,
    token: str,
    plex_client_factory: PlexClientFactory | None = None,
) -> PlexClient:
    factory = plex_client_factory or PlexServer
    return factory(url, token)


def run_subprocess(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True)


def abs_path(path: str) -> str:
    return os.path.abspath(path)


def dir_name(path: str) -> str:
    return os.path.dirname(path)


def join_path(*parts: str) -> str:
    return os.path.join(*parts)


def path_exists(path: str) -> bool:
    return os.path.exists(path)


def is_dir(path: str) -> bool:
    return os.path.isdir(path)


def list_dir(path: str) -> list[str]:
    return list(os.listdir(path))
