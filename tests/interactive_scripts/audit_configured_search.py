import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from _configured_context import (
    load_configured_context,
    get_configured_providers,
    print_configured_providers,
)
from telegram_bot.services.search_logic import orchestrate_searches


def _print_results(results: list[dict]) -> None:
    if not results:
        print("No viable results returned.")
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


async def main() -> None:
    print(
        "Configured search audit. This uses config.ini providers/preferences via orchestrate_searches."
    )
    print("Enter 'exit' to quit at any time.")
    context = load_configured_context()

    while True:
        media_type = input("\nEnter media type ('movie' or 'tv'): ").strip().lower()
        if media_type in {"exit", "quit", "q"}:
            return
        if media_type not in {"movie", "tv"}:
            print("Invalid media type. Please enter 'movie' or 'tv'.")
            continue

        print_configured_providers(get_configured_providers(context, media_type))

        query = input("Enter search query: ").strip()
        if query.lower() in {"exit", "quit", "q"}:
            return
        if not query:
            print("Query cannot be empty.")
            continue

        kwargs: dict[str, object] = {}
        if media_type == "movie":
            year = input("Enter year (optional): ").strip()
            if year.lower() in {"exit", "quit", "q"}:
                return
            if year:
                kwargs["year"] = year

        resolution = input(
            "Enter resolution filter for scrapers that support it (optional): "
        ).strip()
        if resolution.lower() in {"exit", "quit", "q"}:
            return
        if resolution:
            kwargs["resolution"] = resolution

        min_seeders = input("Enter min seeders for final audit output (default 20): ").strip()
        if min_seeders.lower() in {"exit", "quit", "q"}:
            return
        if min_seeders:
            kwargs["min_seeders"] = int(min_seeders)

        print(f"\nRunning configured search for '{query}' ({media_type})...")
        results = await orchestrate_searches(query, media_type, context, **kwargs)
        _print_results(results)


if __name__ == "__main__":
    asyncio.run(main())
