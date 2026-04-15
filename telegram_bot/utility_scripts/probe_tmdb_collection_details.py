from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import date
from typing import Any, cast

from telegram_bot.config import get_configuration
from telegram_bot.services.tmdb_collection_service import (
    TmdbCollectionLookup,
    resolve_tmdb_collection_from_movie,
)


def _apply_tmdb_runtime_env_from_config() -> dict[str, str]:
    """Loads TMDB credentials from config.ini [tmdb] for this probe run."""
    _, _, _, _, _, tmdb_config, _ = get_configuration()
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
            "Probe TMDB collection lookup from a movie title/year. "
            "Credentials are loaded from config.ini [tmdb]."
        )
    )
    parser.add_argument("--title", required=True, help="Movie title to probe.")
    parser.add_argument("--year", type=int, default=None, help="Movie year (optional).")
    parser.add_argument(
        "--today",
        default=None,
        help="Reference date in YYYY-MM-DD format used for release status classification.",
    )
    parser.add_argument(
        "--max-movies",
        type=int,
        default=None,
        help="Optional movie-list cap in output for large collections.",
    )
    return parser.parse_args()


def _parse_today(value: str | None) -> date:
    if not value:
        return date.today()
    return date.fromisoformat(value)


def _apply_movie_output_limit(
    lookup_result: TmdbCollectionLookup,
    *,
    max_movies: int | None,
) -> TmdbCollectionLookup:
    if max_movies is None or max_movies < 1:
        return lookup_result

    collection = lookup_result.get("collection")
    if not isinstance(collection, dict):
        return lookup_result

    movies = collection.get("movies")
    if not isinstance(movies, list):
        return lookup_result
    if len(movies) <= max_movies:
        return lookup_result

    limited_collection = dict(collection)
    limited_collection["movies"] = movies[:max_movies]

    limited_lookup = dict(lookup_result)
    limited_lookup["collection"] = limited_collection
    return cast(TmdbCollectionLookup, limited_lookup)


async def _run_probe(
    title: str,
    *,
    year: int | None,
    today_value: date,
    max_movies: int | None,
) -> dict[str, Any]:
    lookup_result = await resolve_tmdb_collection_from_movie(
        title,
        year=year,
        reference_day=today_value,
    )
    limited_lookup = _apply_movie_output_limit(lookup_result, max_movies=max_movies)
    return {
        "title": title,
        "year": year,
        "today": today_value.isoformat(),
        "lookup": limited_lookup,
        "max_movies": max_movies,
    }


def main() -> None:
    args = _parse_args()
    today_value = _parse_today(args.today)
    tmdb_config = _apply_tmdb_runtime_env_from_config()

    result = asyncio.run(
        _run_probe(
            args.title,
            year=args.year,
            today_value=today_value,
            max_movies=args.max_movies,
        )
    )
    output = {
        "tmdb_config_present": bool(tmdb_config),
        "tmdb_region": tmdb_config.get("region"),
        "probe": result,
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
