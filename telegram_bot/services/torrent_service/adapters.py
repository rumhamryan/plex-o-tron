# telegram_bot/services/torrent_service/adapters.py

from __future__ import annotations

import os

import httpx


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as file_handle:
        file_handle.write(data)


async def fetch_url(
    url: str,
    *,
    timeout: int = 30,
    follow_redirects: bool = True,
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects) as client:
        return await client.get(url)
