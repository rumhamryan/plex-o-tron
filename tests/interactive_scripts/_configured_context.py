from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from telegram_bot.config import SCRAPER_MAX_TORRENT_SIZE_BOT_DATA_KEY, get_configuration

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_configured_context() -> SimpleNamespace:
    """Load the same search config shape the Telegram application stores in bot_data."""
    os.chdir(PROJECT_ROOT)
    _, _, _, _, search_config, _, runtime_limits = get_configuration()
    return SimpleNamespace(
        bot_data={
            "SEARCH_CONFIG": search_config,
            SCRAPER_MAX_TORRENT_SIZE_BOT_DATA_KEY: runtime_limits["scraper_max_torrent_size_gib"],
        }
    )


def get_configured_providers(context: SimpleNamespace, media_type: str) -> list[dict[str, Any]]:
    config_key = "movies" if media_type == "movie" else "tv"
    providers_config = context.bot_data.get("SEARCH_CONFIG", {}).get("providers", [])
    if isinstance(providers_config, list):
        providers = providers_config
    elif isinstance(providers_config, dict):
        providers = [
            *providers_config.get("providers", []),
            *providers_config.get(config_key, []),
        ]
    else:
        return []
    return [
        provider
        for provider in providers
        if isinstance(provider, dict) and provider.get("enabled", True)
    ]


def print_configured_providers(providers: list[dict[str, Any]]) -> None:
    if not providers:
        print("No enabled discovery providers are configured for that media type.")
        return
    print("Configured discovery providers:")
    for index, provider in enumerate(providers, start=1):
        print(
            f"  {index}. {provider.get('name', '<unnamed>')} -> "
            f"{provider.get('search_url', '<missing>')}"
        )


def choose_configured_provider(
    context: SimpleNamespace,
    *,
    media_type: str,
    name_contains: str | None = None,
) -> dict[str, Any] | None:
    providers = get_configured_providers(context, media_type)
    if name_contains:
        needle = name_contains.casefold()
        for provider in providers:
            name = str(provider.get("name", ""))
            if needle in name.casefold():
                return provider
        return None

    print_configured_providers(providers)
    if not providers:
        return None

    selection = input("Select configured provider number: ").strip()
    if selection.lower() in {"exit", "quit", "q"}:
        return None
    try:
        index = int(selection)
    except ValueError:
        print("Invalid selection.")
        return None
    if index < 1 or index > len(providers):
        print("Selection out of range.")
        return None
    return providers[index - 1]
