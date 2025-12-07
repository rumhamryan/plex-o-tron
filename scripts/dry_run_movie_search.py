"""
Quick dry-run script to validate movie search precision with Wikipedia disambiguation.

Run:
    uv run scripts/dry_run_movie_search.py [--res 1080p|720p|2160p] [titles...]

This does not start the bot or download anything. It queries Wikipedia to
resolve movie years and then runs the orchestrated search against configured
sites (YTS, 1337x) using a fake context carrying SEARCH_CONFIG.
"""

from __future__ import annotations

import asyncio
import argparse
import logging
import re
from typing import Any

from telegram_bot.services.scraping_service import fetch_movie_years_from_wikipedia
from telegram_bot.services.search_logic import orchestrate_searches


class _FakeContext:
    def __init__(self, search_config: dict[str, Any]) -> None:
        # The orchestrator and scrapers read SEARCH_CONFIG from bot_data
        self.bot_data: dict[str, Any] = {"SEARCH_CONFIG": search_config}


SEARCH_CONFIG: dict[str, Any] = {
    "websites": {
        "movies": [
            {
                "name": "YTS.mx",
                "enabled": True,
                "search_url": "https://yts.mx/browse-movies/{query}/all/all/0/latest/0/all",
            },
            {
                "name": "1337x",
                "enabled": True,
                "search_url": "https://1337x.to/category-search/{query}/Movies/1/",
            },
        ],
        "tv": [
            {
                "name": "1337x",
                "enabled": True,
                "search_url": "https://1337x.to/category-search/{query}/TV/1/",
            },
        ],
    },
    "preferences": {
        "movies": {
            "resolutions": {"2160p": 5, "4k": 5, "1080p": 4, "720p": 1},
            "codecs": {"x265": 4, "hevc": 4, "x264": 1, "h264": 1},
            "uploaders": {"QxR": 5, "YTS": 4},
        },
        "tv": {
            "resolutions": {"1080p": 5, "720p": 1},
            "codecs": {"x265": 4, "hevc": 4, "x264": 1, "h264": 1},
            "uploaders": {"EZTV": 5, "MeGusta": 5},
        },
    },
}


def _filter_by_resolution(results: list[dict[str, Any]], resolution: str) -> list[dict[str, Any]]:
    res = resolution.lower()
    if res == "2160p":
        pats = ("2160p", "4k")
    elif res == "1080p":
        pats = ("1080p",)
    elif res == "720p":
        pats = ("720p",)
    else:
        pats = (res,)
    out: list[dict[str, Any]] = []
    for r in results:
        t = str(r.get("title", "")).lower()
        if any(p in t for p in pats):
            out.append(r)
    return out


async def _run_for_title(title: str, *, resolution: str = "1080p") -> None:
    print("\n===", title, "===")
    # Separate an explicit year if provided in the query
    m = re.search(r"\b(19\d{2}|20\d{2})\b", title)
    base = title
    explicit_year: str | None = None
    if m:
        explicit_year = m.group(1)
        base = title[: m.start()].strip()

    # Wikipedia disambiguation
    years, corrected = await fetch_movie_years_from_wikipedia(base)
    print(f"WIKI -> years={years} corrected={corrected}")
    base_for_search = corrected or base

    # If an explicit year was provided, force that single path
    candidate_years: list[str | None]
    if explicit_year:
        candidate_years = [explicit_year]
    elif years:
        # Convert to strings for readability and stable ordering
        candidate_years = [str(y) for y in years]
    else:
        candidate_years = [None]

    ctx = _FakeContext(SEARCH_CONFIG)
    for y in candidate_years:
        print(f"Using search base='{base_for_search}' year={y} res={resolution}")
        results = await orchestrate_searches(
            base_for_search, "movie", ctx, year=y, resolution=resolution
        )
        filtered = _filter_by_resolution(results, resolution)

        if not filtered:
            print("No results (after filtering by resolution)")
            continue

        print("Top results:")
        for r in filtered[:3]:
            print(
                f"- {r.get('title')} | seeders={r.get('seeders')} | "
                f"size={r.get('size_gb'):.2f}GB | codec={r.get('codec')} | src={r.get('source')}"
            )


async def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run movie search with Wikipedia disambiguation and site orchestration"
        )
    )
    parser.add_argument(
        "titles",
        nargs="*",
        help="Movie titles to query (e.g., 'Blade Runner 1982')",
    )
    parser.add_argument(
        "--res",
        default="1080p",
        choices=["720p", "1080p", "2160p", "4k"],
        help="Desired resolution filter",
    )
    args = parser.parse_args(argv)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    titles = args.titles or ["Dune", "Spider-Man", "The Thing 1982"]
    # Normalize 4k to 2160p for filtering logic
    resolution = "2160p" if args.res.lower() == "4k" else args.res
    for t in titles:
        try:
            await _run_for_title(t, resolution=resolution)
        except Exception as e:  # noqa: BLE001
            print(f"Error for '{t}': {e}")


if __name__ == "__main__":
    asyncio.run(main())
