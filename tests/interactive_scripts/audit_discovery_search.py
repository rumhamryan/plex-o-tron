from __future__ import annotations

import asyncio
import getpass
import os
import sys
import urllib.parse
from typing import Any, cast

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from telegram_bot.config import get_configuration
from telegram_bot.services.discovery import DiscoveryOrchestrator, DiscoveryRequest
from telegram_bot.services.discovery.schemas import MediaType

DEFAULT_TORZNAB_URL = os.environ.get("PROWLARR_TORZNAB_URL", "")
DEFAULT_TORZNAB_PROVIDERS: list[dict[str, Any]] = []
DEFAULT_PROWLARR_INDEXER_IDS = [1, 3, 6, 7, 9, 10]


def _redact_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    params = []
    for key, param_value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if key.casefold() in {"apikey", "api_key", "key", "token"}:
            params.append((key, "<redacted>"))
        else:
            params.append((key, param_value))
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(params),
            parsed.fragment,
        )
    )


def _configured_torznab_providers(
    search_config: dict[str, Any],
    media_type: str,
) -> list[dict[str, Any]]:
    websites = search_config.get("websites", {})
    if not isinstance(websites, dict):
        return []

    config_key = "movies" if media_type == "movie" else "tv"
    possible_groups = [websites.get("providers", []), websites.get(config_key, [])]
    providers: list[dict[str, Any]] = []
    for group in possible_groups:
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            if str(item.get("type", "")).casefold() != "torznab":
                continue
            providers.append(dict(item))
    return providers


def _prompt_manual_provider() -> dict[str, Any] | None:
    print("\nNo configured Torznab providers were found for this media type.")
    print("Paste a Prowlarr Torznab URL to test one provider, or press Enter to cancel.")
    print("Expected shape:")
    print("  http://127.0.0.1:9696/1/api?apikey=KEY&t={type}&q={query}&cat={category}")
    raw_url = getpass.getpass("Torznab URL: ").strip()
    if not raw_url:
        return None

    name = input("Provider name [Manual Torznab]: ").strip() or "Manual Torznab"
    return {
        "name": name,
        "type": "torznab",
        "enabled": True,
        "search_url": raw_url,
        "categories": {"movie": "2000", "tv": "5000"},
        "timeout_seconds": 30,
    }


def _default_torznab_providers() -> list[dict[str, Any]]:
    providers = [dict(provider) for provider in DEFAULT_TORZNAB_PROVIDERS]
    if DEFAULT_TORZNAB_URL.strip():
        providers.extend(_derive_prowlarr_indexer_providers(DEFAULT_TORZNAB_URL.strip()))
    return providers


def _derive_prowlarr_indexer_providers(template_url: str) -> list[dict[str, Any]]:
    parsed = urllib.parse.urlsplit(template_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 2 or path_parts[-1] != "api":
        return [_provider_config("Default Torznab", template_url)]

    derived_providers: list[dict[str, Any]] = []
    for indexer_id in _prowlarr_indexer_ids():
        path_parts[-2] = str(indexer_id)
        search_url = urllib.parse.urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                "/" + "/".join(path_parts),
                parsed.query,
                parsed.fragment,
            )
        )
        derived_providers.append(_provider_config(f"Prowlarr Indexer {indexer_id}", search_url))
    return derived_providers


def _prowlarr_indexer_ids() -> list[int]:
    raw_value = os.environ.get("PROWLARR_INDEXER_IDS", "").strip()
    if not raw_value:
        return list(DEFAULT_PROWLARR_INDEXER_IDS)

    indexer_ids: list[int] = []
    for item in raw_value.split(","):
        try:
            indexer_id = int(item.strip())
        except ValueError:
            continue
        if indexer_id > 0:
            indexer_ids.append(indexer_id)
    return indexer_ids or list(DEFAULT_PROWLARR_INDEXER_IDS)


def _provider_config(name: str, search_url: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": "torznab",
        "enabled": True,
        "search_url": search_url,
        "categories": {"movie": "2000", "tv": "5000"},
        "timeout_seconds": 30,
    }


def _print_providers(providers: list[dict[str, Any]]) -> None:
    print("\nDiscovery providers:")
    for index, provider in enumerate(providers, start=1):
        url = str(provider.get("search_url", ""))
        print(f"  {index}. {provider.get('name', '<unnamed>')} -> {_redact_url(url)}")


def _print_results(results: list[dict[str, Any]]) -> None:
    if not results:
        print("\nNo viable results returned.")
        return

    for index, item in enumerate(results, start=1):
        print(f"\nResult {index}:")
        for field in (
            "title",
            "source",
            "info_url",
            "score",
            "seeders",
            "leechers",
            "size_gib",
            "codec",
            "year",
            "matched_video_formats",
            "matched_audio_formats",
            "matched_audio_channels",
        ):
            print(f"  {field}: {item.get(field)}")


def _print_provider_stats(orchestrator: DiscoveryOrchestrator) -> None:
    print("\nProvider stats:")
    for stats in orchestrator.last_provider_stats.values():
        if stats.status in {"failed", "skipped"}:
            message = f"{stats.error_type or 'UnknownError'}"
            if stats.error_message:
                message = f"{message}: {stats.error_message}"
            print(f"  {stats.provider_name}: {stats.status} ({message})")
            continue

        print(
            "  "
            f"{stats.provider_name}: "
            f"{stats.raw_count} raw, "
            f"{stats.deduplicated_count} deduped, "
            f"{stats.filtered_count} filtered, "
            f"{stats.scored_count} viable"
        )
        print(
            "    dropped: "
            f"duplicates={stats.dropped_duplicate_count}, "
            f"low_seeders={stats.dropped_low_seeders_count}, "
            f"too_large={stats.dropped_too_large_count}, "
            f"screener={stats.dropped_screener_count}, "
            f"low_score={stats.dropped_low_score_count}"
        )
        if stats.raw_samples:
            print("    raw samples:")
            for sample in stats.raw_samples[:3]:
                print(
                    "      "
                    f"S:{sample.get('seeders')} "
                    f"L:{sample.get('leechers')} "
                    f"{sample.get('size_gib')} GiB | "
                    f"{sample.get('title')}"
                )
                raw_attrs = sample.get("raw_attrs")
                if raw_attrs:
                    interesting_attrs = {
                        key: raw_attrs.get(key)
                        for key in (
                            "seeders",
                            "seeds",
                            "peers",
                            "leechers",
                            "grabs",
                            "downloads",
                            "size",
                        )
                        if key in raw_attrs
                    }
                    print(f"        attrs: {interesting_attrs}")


def _prompt_optional_int(prompt: str) -> int | None:
    raw_value = input(prompt).strip()
    if raw_value.lower() in {"exit", "quit", "q"}:
        raise KeyboardInterrupt
    if not raw_value:
        return None
    try:
        parsed = int(raw_value)
    except ValueError:
        print("Invalid integer; ignoring value.")
        return None
    return parsed if parsed >= 0 else None


async def main() -> None:
    print("Discovery search audit. This exercises the new Torznab discovery pipeline.")
    print("Enter 'exit' to quit at any prompt.")

    os.chdir(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
    _, _, _, _, search_config, _, runtime_limits = get_configuration()

    while True:
        try:
            media_type = input("\nEnter media type ('movie' or 'tv'): ").strip().lower()
            if media_type in {"exit", "quit", "q"}:
                return
            if media_type not in {"movie", "tv"}:
                print("Invalid media type. Please enter 'movie' or 'tv'.")
                continue

            providers = _configured_torznab_providers(search_config, media_type)
            if not providers:
                providers = _default_torznab_providers()
            if not providers:
                manual_provider = _prompt_manual_provider()
                if manual_provider is None:
                    continue
                providers = [manual_provider]

            _print_providers(providers)

            query = input("Enter search query: ").strip()
            if query.lower() in {"exit", "quit", "q"}:
                return
            if not query:
                print("Query cannot be empty.")
                continue

            year: str | int | None = None
            if media_type == "movie":
                raw_year = input("Enter year (optional): ").strip()
                if raw_year.lower() in {"exit", "quit", "q"}:
                    return
                year = raw_year or None

            min_seeders = _prompt_optional_int("Enter min seeders (default 20): ")
            request = DiscoveryRequest(
                query=query,
                media_type=cast(MediaType, media_type),
                year=year,
                min_seeders=20 if min_seeders is None else min_seeders,
                max_size_gib=runtime_limits["scraper_max_torrent_size_gib"],
            )
            preferences = search_config.get("preferences", {})
            orchestrator = DiscoveryOrchestrator(providers, preferences=preferences)

            print(f"\nRunning discovery search for {query!r} ({media_type})...")
            results = await orchestrator.search(request)
            _print_provider_stats(orchestrator)
            _print_results(results)
        except KeyboardInterrupt:
            print("\nExiting.")
            return


if __name__ == "__main__":
    asyncio.run(main())
