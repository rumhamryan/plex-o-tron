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


def get_configured_sites(context: SimpleNamespace, media_type: str) -> list[dict[str, Any]]:
    config_key = "movies" if media_type == "movie" else "tv"
    websites = context.bot_data.get("SEARCH_CONFIG", {}).get("websites", {})
    sites = websites.get(config_key, [])
    if not isinstance(sites, list):
        return []
    return [site for site in sites if isinstance(site, dict) and site.get("enabled", True)]


def print_configured_sites(sites: list[dict[str, Any]]) -> None:
    if not sites:
        print("No enabled sites are configured for that media type.")
        return
    print("Configured sites:")
    for index, site in enumerate(sites, start=1):
        print(
            f"  {index}. {site.get('name', '<unnamed>')} -> {site.get('search_url', '<missing>')}"
        )


def choose_configured_site(
    context: SimpleNamespace,
    *,
    media_type: str,
    name_contains: str | None = None,
) -> dict[str, Any] | None:
    sites = get_configured_sites(context, media_type)
    if name_contains:
        needle = name_contains.casefold()
        for site in sites:
            name = str(site.get("name", ""))
            if needle in name.casefold():
                return site
        return None

    print_configured_sites(sites)
    if not sites:
        return None

    selection = input("Select configured site number: ").strip()
    if selection.lower() in {"exit", "quit", "q"}:
        return None
    try:
        index = int(selection)
    except ValueError:
        print("Invalid selection.")
        return None
    if index < 1 or index > len(sites):
        print("Selection out of range.")
        return None
    return sites[index - 1]


def canonical_yaml_site_name(configured_name: str) -> str:
    normalized = configured_name.strip().casefold()
    if "eztv" in normalized:
        return "eztv"
    if normalized.startswith("1337x"):
        return "1337x"
    return configured_name.strip()
