# telegram_bot/services/download_manager/adapters.py

from __future__ import annotations

import os

import httpx


def path_exists(path: str) -> bool:
    return os.path.exists(path)


def remove_file(path: str) -> None:
    os.remove(path)


def list_dir(path: str) -> list[str]:
    return list(os.listdir(path))


def join_path(*parts: str) -> str:
    return os.path.join(*parts)


async def fetch_url(
    url: str,
    *,
    timeout: int = 30,
    follow_redirects: bool = True,
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects) as client:
        return await client.get(url)
