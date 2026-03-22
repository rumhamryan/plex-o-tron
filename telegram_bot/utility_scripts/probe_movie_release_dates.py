from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import date

from telegram_bot.config import get_configuration
from telegram_bot.services.tracking.movie_release_dates import (
    _resolve_tmdb_availability,
    resolve_movie_tracking_target,
)


def _apply_tmdb_runtime_env_from_config() -> dict[str, str]:
    """Loads TMDB credentials from config.ini [tmdb] for this probe run."""
    _, _, _, _, _, tmdb_config = get_configuration()
    if "access_token" in tmdb_config:
        os.environ["TMDB_ACCESS_TOKEN"] = tmdb_config["access_token"]
    if "api_key" in tmdb_config:
        os.environ["TMDB_API_KEY"] = tmdb_config["api_key"]
    if "region" in tmdb_config:
        os.environ["TMDB_REGION"] = tmdb_config["region"]
    return tmdb_config


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Probe movie release availability resolution for a title/year and print "
            "TMDB non-theatrical data plus final resolver output. "
            "Credentials are read from config.ini [tmdb]."
        )
    )
    parser.add_argument("--title", required=True, help="Movie title to probe.")
    parser.add_argument("--year", type=int, default=None, help="Movie year (optional).")
    parser.add_argument(
        "--today",
        default=None,
        help="Reference date in YYYY-MM-DD format (defaults to today).",
    )
    return parser.parse_args()


def _parse_today(value: str | None) -> date:
    if not value:
        return date.today()
    return date.fromisoformat(value)


async def _run_probe(title: str, *, year: int | None, today_value: date) -> dict[str, object]:
    tmdb_date, tmdb_source = await _resolve_tmdb_availability(title, year=year)
    resolved = await resolve_movie_tracking_target(title, year=year, today=today_value)
    resolved_availability_date = resolved.get("availability_date")
    return {
        "title": title,
        "year": year,
        "today": today_value.isoformat(),
        "tmdb_non_theatrical": {
            "availability_date": tmdb_date.isoformat() if tmdb_date else None,
            "availability_source": tmdb_source,
        },
        "resolved": {
            "canonical_title": resolved.get("canonical_title"),
            "release_date_status": resolved.get("release_date_status"),
            "availability_date": (
                resolved_availability_date.isoformat()
                if isinstance(resolved_availability_date, date)
                else None
            ),
            "availability_source": resolved.get("availability_source"),
            "is_released": resolved.get("is_released"),
        },
    }


def main() -> None:
    args = _parse_args()
    today_value = _parse_today(args.today)
    tmdb_config = _apply_tmdb_runtime_env_from_config()

    result = asyncio.run(_run_probe(args.title, year=args.year, today_value=today_value))
    output = {
        "tmdb_config_present": bool(tmdb_config),
        "tmdb_region": tmdb_config.get("region"),
        "probe": result,
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
