from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram_bot.config import ALLOWED_EXTENSIONS, get_configuration
from telegram_bot.services.plex_service import (
    ensure_collection_contains_movies,
    wait_for_movies_to_be_available,
)
from telegram_bot.utils import parse_torrent_name, sanitize_collection_name

_MOVIE_YEAR_PATTERN = re.compile(r"^(?P<title>.+?)\s*[\[(](?P<year>(?:18|19|20|21)\d{2})[)\]]\s*$")
_ENTRY_PREFIX_PATTERN = re.compile(r"^\s*\d+\s*[-._:]\s*")


@dataclass(frozen=True)
class MovieCandidate:
    title: str
    year: int | None
    destination_path: str


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create or update a Plex movie collection from a local folder. "
            "Plex credentials are read from config.ini [plex]."
        )
    )
    parser.add_argument(
        "--folder",
        required=True,
        help="Collection folder containing movie files and/or movie directories.",
    )
    parser.add_argument(
        "--collection-name",
        default=None,
        help="Optional Plex collection name override. Defaults to the folder name.",
    )
    parser.add_argument(
        "--recursive",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Recursively scan nested subdirectories under --folder.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print discovered movies without changing Plex collections.",
    )
    parser.add_argument(
        "--wait-for-index",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Wait for Plex indexing before collection tagging (default: enabled).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=120,
        help="Timeout used when waiting for Plex indexing.",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=5,
        help="Polling interval used when waiting for Plex indexing.",
    )
    return parser.parse_args()


def _coerce_year(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if 1800 <= parsed <= 2199:
        return parsed
    return None


def _clean_entry_title(raw_title: str) -> str:
    cleaned = _ENTRY_PREFIX_PATTERN.sub("", raw_title).strip()
    return re.sub(r"\s+", " ", cleaned)


def _extract_movie_from_entry_name(entry_name: str, destination_path: str) -> MovieCandidate | None:
    base_name = entry_name
    suffix = Path(entry_name).suffix
    if suffix:
        base_name = Path(entry_name).stem
    base_name = _clean_entry_title(base_name)
    if not base_name:
        return None

    year_match = _MOVIE_YEAR_PATTERN.match(base_name)
    if year_match:
        title = _clean_entry_title(year_match.group("title"))
        year = _coerce_year(year_match.group("year"))
        if title:
            return MovieCandidate(title=title, year=year, destination_path=destination_path)

    parsed = parse_torrent_name(base_name)
    parsed_type = str(parsed.get("type") or "").strip().casefold()
    if parsed_type and parsed_type != "movie":
        return None

    raw_title = str(parsed.get("title") or "").strip()
    if not raw_title:
        raw_title = base_name
    title = _clean_entry_title(raw_title)
    if not title:
        return None

    return MovieCandidate(
        title=title,
        year=_coerce_year(parsed.get("year")),
        destination_path=destination_path,
    )


def _should_consider_path(path: Path) -> bool:
    if path.name.startswith("."):
        return False
    if path.is_dir():
        return True
    if path.is_file() and path.suffix.lower() in set(ALLOWED_EXTENSIONS):
        return True
    return False


def build_collection_movies_from_folder(
    folder_path: str,
    *,
    recursive: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    folder = Path(os.path.abspath(os.path.expanduser(folder_path)))
    if not folder.exists():
        raise ValueError(f"Folder does not exist: {folder}")
    if not folder.is_dir():
        raise ValueError(f"Path is not a directory: {folder}")

    warnings: list[str] = []
    discovered: list[MovieCandidate] = []

    def _visit_entry(entry: Path) -> None:
        if not _should_consider_path(entry):
            return
        movie = _extract_movie_from_entry_name(entry.name, str(entry.resolve()))
        if movie is None:
            warnings.append(f"Skipped non-movie or unparseable entry: {entry}")
            return
        discovered.append(movie)

    if recursive:
        for current_root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            root_path = Path(current_root)
            for name in dirs:
                _visit_entry(root_path / name)
            for name in files:
                _visit_entry(root_path / name)
    else:
        for entry in sorted(folder.iterdir(), key=lambda candidate: candidate.name.casefold()):
            _visit_entry(entry)

    deduped: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, int | None]] = set()
    for movie in sorted(discovered, key=lambda item: (item.title.casefold(), item.year or 0)):
        key = (movie.title.casefold(), movie.year)
        if key in seen_keys:
            warnings.append(
                "Skipped duplicate movie candidate based on normalized title/year: "
                f"{movie.title} ({movie.year if movie.year is not None else 'unknown'})"
            )
            continue
        seen_keys.add(key)
        deduped.append(
            {
                "title": movie.title,
                "year": movie.year,
                "destination_path": movie.destination_path,
            }
        )
    return deduped, warnings


def resolve_collection_name(folder_path: str, raw_collection_name: str | None) -> str:
    if isinstance(raw_collection_name, str) and raw_collection_name.strip():
        source_name = raw_collection_name.strip()
    else:
        source_name = Path(folder_path).name.strip()
    return sanitize_collection_name(source_name)


def _validate_plex_config(plex_config: dict[str, str]) -> None:
    token = str(plex_config.get("token") or "").strip()
    if not plex_config.get("url"):
        raise ValueError("Plex URL is missing in config.ini [plex].")
    if not token:
        raise ValueError("Plex token is missing in config.ini [plex].")
    if token.upper() == "PLEX_TOKEN":
        raise ValueError("Plex token placeholder found in config.ini [plex].")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    _, _, _, plex_config, _, _, _ = get_configuration()
    if not plex_config:
        raise ValueError("Plex configuration is missing from config.ini [plex].")
    _validate_plex_config(plex_config)

    folder_path = os.path.abspath(os.path.expanduser(args.folder))
    collection_name = resolve_collection_name(folder_path, args.collection_name)
    movies, warnings = build_collection_movies_from_folder(folder_path, recursive=args.recursive)
    if not movies:
        raise ValueError(
            "No movie candidates were found in the target folder. Check naming and file extensions."
        )

    summary: dict[str, Any] = {
        "folder": folder_path,
        "collection_name": collection_name,
        "dry_run": bool(args.dry_run),
        "recursive": bool(args.recursive),
        "movie_count": len(movies),
        "movies": movies,
        "warnings": warnings,
    }

    if args.dry_run:
        return summary

    waited_for_index = False
    indexed_before_timeout = None
    if args.wait_for_index:
        waited_for_index = True
        indexed_before_timeout = await wait_for_movies_to_be_available(
            plex_config,
            movies,
            timeout_seconds=max(int(args.timeout_seconds), 1),
            poll_interval_seconds=max(int(args.poll_interval_seconds), 1),
        )

    added = await ensure_collection_contains_movies(
        plex_config,
        collection_name,
        movies,
    )
    summary["waited_for_index"] = waited_for_index
    summary["indexed_before_timeout"] = indexed_before_timeout
    summary["added_to_collection"] = added
    summary["added_count"] = len(added)
    summary["unmatched_count"] = max(len(movies) - len(added), 0)
    return summary


def main() -> None:
    args = _parse_args()
    result = asyncio.run(_run(args))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
