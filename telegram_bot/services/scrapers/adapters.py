# telegram_bot/services/scrapers/adapters.py

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx


def create_async_client(
    *,
    timeout: int | float = 30,
    follow_redirects: bool = True,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects)


async def fetch_page(
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: int | float = 30,
    follow_redirects: bool = True,
) -> httpx.Response:
    async with create_async_client(timeout=timeout, follow_redirects=follow_redirects) as client:
        return await client.get(url, params=params, headers=headers)


def read_text(path: str | Path, *, encoding: str = "utf-8") -> str:
    return Path(path).read_text(encoding=encoding)
