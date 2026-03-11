# telegram_bot/services/search_logic/adapters.py

from __future__ import annotations

import os
from collections.abc import Iterator


def path_exists(path: str) -> bool:
    return os.path.exists(path)


def is_dir(path: str) -> bool:
    return os.path.isdir(path)


def list_dir(path: str) -> list[str]:
    return list(os.listdir(path))


def walk_dir(path: str) -> Iterator[tuple[str, list[str], list[str]]]:
    return os.walk(path)


def join_path(*parts: str) -> str:
    return os.path.join(*parts)
