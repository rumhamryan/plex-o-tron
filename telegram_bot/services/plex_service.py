# telegram_bot/services/plex_service.py

import asyncio
import difflib
import os
import platform
import re
import subprocess
from typing import Any, Sequence, Set

from plexapi.exceptions import Unauthorized
from requests import exceptions as requests_exceptions
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from ..config import logger
from .interfaces import PlexClient, PlexClientFactory
from .plex_adapters import (
    abs_path,
    create_plex_client,
    dir_name,
    is_dir,
    join_path,
    list_dir,
    path_exists,
    run_subprocess,
)

__all__ = [
    "get_plex_server_status",
    "restart_plex_server",
    "get_existing_episodes_for_season",
    "wait_for_movies_to_be_available",
    "ensure_collection_contains_movies",
]

PLEX_INDEX_WAIT_TIMEOUT_SECONDS = 120
PLEX_INDEX_POLL_INTERVAL_SECONDS = 5
_PLEX_TITLE_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_ROMAN_NUMERAL_TOKENS = {
    "i": "1",
    "ii": "2",
    "iii": "3",
    "iv": "4",
    "v": "5",
    "vi": "6",
    "vii": "7",
    "viii": "8",
    "ix": "9",
    "x": "10",
}
_NUMBER_WORD_TOKENS = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
}
_PLEX_COLLECTION_STOPWORDS = {"collection", "film", "films", "movie", "movies"}


def _compact_subprocess_output(
    output: str | None,
    *,
    max_lines: int = 12,
    max_chars: int = 1500,
) -> str:
    """Normalize subprocess output for concise user-facing error messages."""
    if not isinstance(output, str):
        return "No script output was captured."

    normalized = output.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return "No script output was captured."

    lines = [line.strip() for line in normalized.split("\n") if line.strip()]
    if not lines:
        return "No script output was captured."

    truncated = False
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True

    summary = "\n".join(lines)
    if len(summary) > max_chars:
        summary = summary[: max_chars - 3].rstrip() + "..."
        truncated = True

    if truncated:
        summary += "\n(output truncated)"
    return summary


def _should_suppress_plex_error(exc: Exception) -> bool:
    """Return True when the exception stems from transient Plex connectivity issues."""
    suppressible = (
        requests_exceptions.RequestException,
        TimeoutError,
        ConnectionError,
        OSError,
    )
    if isinstance(exc, suppressible):
        return True
    message = str(exc).lower()
    return "max retries exceeded" in message or "timed out" in message


def _has_valid_plex_token(plex_config: dict[str, Any] | None) -> bool:
    """Indicates whether the Plex token looks configured."""
    if not plex_config:
        return False
    token = str(plex_config.get("token") or "").strip()
    return bool(token) and token.upper() != "PLEX_TOKEN"


def _normalize_plex_title(value: str) -> str:
    """Normalize common Plex/Wikipedia title variants for matching."""
    raw = (value or "").casefold().replace("&", " and ")
    tokens: list[str] = []
    for token in _PLEX_TITLE_TOKEN_PATTERN.findall(raw):
        if token == "pt":
            token = "part"
        token = _ROMAN_NUMERAL_TOKENS.get(token, token)
        token = _NUMBER_WORD_TOKENS.get(token, token)
        tokens.append(token)
    return " ".join(tokens).strip()


def _normalize_collection_lookup_key(value: str) -> str:
    normalized = _normalize_plex_title(value)
    tokens = [token for token in normalized.split() if token not in _PLEX_COLLECTION_STOPWORDS]
    return " ".join(tokens) if tokens else normalized


def _title_token_overlap_ratio(left: str, right: str) -> float:
    left_tokens = set(_normalize_plex_title(left).split())
    right_tokens = set(_normalize_plex_title(right).split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(min(len(left_tokens), len(right_tokens)), 1)


def _iter_media_title_variants(item: Any) -> list[str]:
    variants: list[str] = []
    for attr_name in ("title", "originalTitle", "titleSort"):
        raw_value = getattr(item, attr_name, None)
        if not isinstance(raw_value, str):
            continue
        cleaned = raw_value.strip()
        if cleaned and cleaned not in variants:
            variants.append(cleaned)
    return variants


def _score_media_title_match(item: Any, expected_title: str, year_value: Any) -> float:
    expected_normalized = _normalize_plex_title(expected_title)
    if not expected_normalized:
        return 0.0

    best_score = 0.0
    for candidate_title in _iter_media_title_variants(item):
        candidate_normalized = _normalize_plex_title(candidate_title)
        if not candidate_normalized:
            continue
        if candidate_normalized == expected_normalized:
            best_score = max(best_score, 100.0)
            continue

        sequence_score = (
            difflib.SequenceMatcher(
                a=expected_normalized,
                b=candidate_normalized,
            ).ratio()
            * 100
        )
        overlap_score = _title_token_overlap_ratio(expected_title, candidate_title) * 100
        containment_score = 0.0
        if (
            expected_normalized in candidate_normalized
            or candidate_normalized in expected_normalized
        ):
            containment_score = 92.0
        best_score = max(best_score, sequence_score, overlap_score, containment_score)

    raw_item_year = getattr(item, "year", None)
    item_year: int | None
    if isinstance(raw_item_year, int):
        item_year = raw_item_year
    else:
        try:
            item_year = int(raw_item_year) if isinstance(raw_item_year, str) else None
        except ValueError:
            item_year = None
    try:
        expected_year = int(year_value) if year_value is not None else None
    except (TypeError, ValueError):
        expected_year = None

    if expected_year is not None and item_year == expected_year:
        best_score += 5.0

    return best_score


def _is_acceptable_media_match(item: Any, expected_title: str, year_value: Any) -> bool:
    score = _score_media_title_match(item, expected_title, year_value)
    has_year = year_value is not None
    threshold = 72.0 if has_year else 85.0
    return score >= threshold


def _dedupe_media_items(items: Sequence[Any]) -> list[Any]:
    deduped: list[Any] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for item in items:
        key = (
            getattr(item, "ratingKey", None),
            getattr(item, "title", None),
            getattr(item, "year", None),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _normalize_media_path(path: str) -> str:
    return os.path.normcase(abs_path(path))


def _iter_media_file_paths(item: Any) -> list[str]:
    paths: list[str] = []

    raw_locations = getattr(item, "locations", None)
    if callable(raw_locations):
        try:
            raw_locations = raw_locations()
        except Exception:
            raw_locations = None

    if isinstance(raw_locations, Sequence) and not isinstance(raw_locations, (str, bytes)):
        for path in raw_locations:
            if isinstance(path, str) and path.strip():
                paths.append(path.strip())

    for media in getattr(item, "media", []) or []:
        for part in getattr(media, "parts", []) or []:
            file_path = getattr(part, "file", None)
            if isinstance(file_path, str) and file_path.strip():
                paths.append(file_path.strip())

    deduped_paths: list[str] = []
    seen: set[str] = set()
    for path in paths:
        normalized = _normalize_media_path(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped_paths.append(path)
    return deduped_paths


def _find_movie_by_path(movies_section: Any, expected_path: str) -> Sequence[Any]:
    normalized_expected = _normalize_media_path(expected_path)
    if not normalized_expected:
        return []

    try:
        items = movies_section.all()
    except Exception:
        return []

    matches: list[Any] = []
    for item in items or []:
        for candidate_path in _iter_media_file_paths(item):
            if _normalize_media_path(candidate_path) == normalized_expected:
                matches.append(item)
                break
    return _dedupe_media_items(matches)


def _resolve_existing_collection_name(movies_section: Any, requested_name: str) -> str:
    """Prefer the requested Plex collection name, renaming a single equivalent alias when safe."""
    normalized_requested = _normalize_collection_lookup_key(requested_name)
    if not normalized_requested:
        return requested_name

    collection_lookup = getattr(movies_section, "collection", None)
    if callable(collection_lookup):
        try:
            collection = collection_lookup(requested_name)
        except Exception:
            collection = None
        if collection is not None:
            resolved_title = str(
                getattr(collection, "title", requested_name) or requested_name
            ).strip()
            return resolved_title or requested_name

    collections_getter = getattr(movies_section, "collections", None)
    if not callable(collections_getter):
        return requested_name

    try:
        collections = collections_getter()
    except Exception:
        return requested_name

    exact_requested_exists = False
    matching_collections: list[Any] = []
    for collection in collections or []:
        title = str(getattr(collection, "title", "") or "").strip()
        if not title:
            continue
        if title == requested_name:
            exact_requested_exists = True
        if _normalize_collection_lookup_key(title) == normalized_requested:
            matching_collections.append(collection)

    if not matching_collections:
        return requested_name

    if exact_requested_exists:
        logger.info(
            "[PLEX] Using existing canonical collection '%s' without renaming alias matches.",
            requested_name,
        )
        return requested_name

    if len(matching_collections) > 1:
        alias_titles = [
            str(getattr(collection, "title", "") or "").strip()
            for collection in matching_collections
        ]
        logger.warning(
            "[PLEX] Found multiple equivalent collections for requested collection '%s': %s. "
            "Keeping the first match without renaming.",
            requested_name,
            ", ".join(title for title in alias_titles if title) or "(unknown titles)",
        )
        first_title = str(
            getattr(matching_collections[0], "title", requested_name) or requested_name
        )
        return first_title.strip() or requested_name

    collection = matching_collections[0]
    existing_title = str(getattr(collection, "title", requested_name) or requested_name).strip()
    if not existing_title or existing_title == requested_name:
        return requested_name

    rename_method = getattr(collection, "editTitle", None)
    if not callable(rename_method):
        logger.info(
            "[PLEX] Reusing existing collection '%s' for requested collection '%s'.",
            existing_title,
            requested_name,
        )
        return existing_title

    try:
        rename_method(requested_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[PLEX] Failed to rename existing collection '%s' to '%s': %s",
            existing_title,
            requested_name,
            exc,
        )
        return existing_title

    logger.info(
        "[PLEX] Renamed existing collection '%s' to requested collection '%s'.",
        existing_title,
        requested_name,
    )
    return requested_name


async def get_plex_server_status(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    plex_client_factory: PlexClientFactory | None = None,
) -> str:
    """
    Checks the connection to the Plex Media Server and returns a formatted status message.
    """
    plex_config = context.bot_data.get("PLEX_CONFIG", {})

    if not plex_config or not _has_valid_plex_token(plex_config):
        return "Plex Status: ⚪️ Not configured. Please add your Plex details to `config.ini`."

    try:
        logger.info(f"Attempting to connect to Plex server at {plex_config.get('url')}...")

        # Run the blocking plexapi call in a separate thread
        plex: PlexClient = await asyncio.to_thread(
            create_plex_client,
            plex_config["url"],
            plex_config["token"],
            plex_client_factory,
        )

        # The connection is successful if no exception was raised.
        # We can fetch info for logging, but it's not needed for the user message.
        server_version = plex.version
        server_platform = plex.platform
        logger.info(
            f"Successfully connected to Plex! Version: {server_version}, Platform: {server_platform}"
        )

        return "Plex Status: ✅ *Connected*"

    except Unauthorized:
        logger.error("Plex authentication failed. The API token is likely incorrect.")
        return (
            "Plex Status: ❌ *Authentication Failed*\n\n"
            "The Plex API token is incorrect\\. Please check your `config\\.ini` file\\."
        )
    except Exception as e:
        logger.error(f"Failed to connect to Plex server: {e}")
        escaped_url = escape_markdown(plex_config.get("url", ""), version=2)
        return (
            f"Plex Status: ❌ *Connection Failed*\n"
            f"Could not connect to the Plex server at `{escaped_url}`\\. "
            f"Please ensure the server is running and accessible\\."
        )


async def restart_plex_server() -> tuple[bool, str]:
    """
    Attempts to restart the Plex server by executing the OS-specific restart script.

    Returns:
        A tuple containing (success_boolean, message_string).
    """
    is_linux = platform.system() == "Linux"
    script_name = "restart_plex.sh" if is_linux else "restart_plex.ps1"

    # Check if script exists in the root or the utility_scripts folder
    script_path = abs_path(script_name)
    if not path_exists(script_path):
        # Fallback to utility_scripts folder
        script_path = join_path(dir_name(__file__), "..", "utility_scripts", script_name)
        script_path = abs_path(script_path)

    if not path_exists(script_path):
        error_msg = f"Wrapper script not found at {script_path}"
        logger.error(f"[PLEX RESTART] {error_msg}")
        return (
            False,
            f"The '{script_name}' script was not found.",
        )

    if is_linux:
        command = ["/usr/bin/sudo", script_path]
    else:
        command = ["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", script_path]

    try:
        logger.info(f"[PLEX RESTART] Executing wrapper script: {' '.join(command)}")

        # Run the blocking subprocess call in a separate thread
        await asyncio.to_thread(run_subprocess, command)

        logger.info("[PLEX RESTART] Script executed successfully.")
        return True, ""

    except subprocess.CalledProcessError as e:
        error_output = e.stderr or e.stdout
        logger.error(f"[PLEX RESTART] Script failed to execute: {error_output}")
        output_summary = _compact_subprocess_output(error_output)
        if is_linux:
            return False, (
                "Restart script failed. This usually means the sudoers rule for "
                "restart_plex.sh is missing or incorrect.\n\n"
                "Script output:\n"
                f"{output_summary}"
            )
        else:
            return False, (
                "Restart script failed. Verify Plex is installed and run the bot with "
                "Administrator privileges when service control is required.\n\n"
                "Script output:\n"
                f"{output_summary}"
            )

    except Exception as e:
        logger.error(f"[PLEX RESTART] An unexpected error occurred: {e}")
        return (
            False,
            f"An unexpected error occurred while attempting Plex restart:\n{e}",
        )


async def get_existing_episodes_for_season(
    context: ContextTypes.DEFAULT_TYPE, show_title: str, season: int
) -> Set[int]:
    """
    Returns a set of episode numbers already present for the given show/season
    by scanning the local media library only (no Plex API calls).
    """
    existing: set[int] = set()

    try:
        save_paths = (context.bot_data or {}).get("SAVE_PATHS", {})
        tv_root = save_paths.get("tv_shows") or save_paths.get("default")
        if not tv_root:
            return existing

        # Mirror media_manager sanitization for show directory name
        invalid_chars = r'<>:"/\\|?*'
        safe_show = "".join(c for c in show_title if c not in invalid_chars)
        season_dir = join_path(tv_root, safe_show, f"Season {int(season):02d}")
        if not is_dir(season_dir):
            return existing

        pat = re.compile(r"(?i)\bS(\d{1,2})E(\d{1,2})\b")
        for fname in list_dir(season_dir):
            m = pat.search(fname)
            if not m:
                continue
            try:
                s_num = int(m.group(1))
                e_num = int(m.group(2))
                if s_num == int(season):
                    existing.add(e_num)
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"Filesystem episode check failed: {e}")

    return existing


async def ensure_collection_contains_movies(
    plex_config: dict[str, str] | None,
    collection_name: str,
    movies: Sequence[dict[str, Any]],
    *,
    plex_client_factory: PlexClientFactory | None = None,
) -> list[str]:
    """
    Adds the provided movies to a Plex collection, returning the matched titles.
    """
    if (
        not plex_config
        or not _has_valid_plex_token(plex_config)
        or not collection_name
        or not movies
    ):
        return []

    try:
        plex: PlexClient = await asyncio.to_thread(
            create_plex_client,
            plex_config["url"],
            plex_config["token"],
            plex_client_factory,
        )
        movies_section = await asyncio.to_thread(plex.library.section, "Movies")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[PLEX] Could not prepare collection '{collection_name}': {exc}")
        return []

    resolved_collection_name = await asyncio.to_thread(
        _resolve_existing_collection_name,
        movies_section,
        collection_name,
    )
    matched_labels: list[str] = []

    for movie in movies:
        title = str(movie.get("title") or "").strip()
        if not title:
            continue
        year_value = movie.get("year")
        expected_path = str(movie.get("destination_path") or movie.get("path") or "").strip()
        matches = await asyncio.to_thread(
            _search_movies_section,
            movies_section,
            title,
            year_value,
        )
        if not matches and expected_path:
            matches = await asyncio.to_thread(
                _find_movie_by_path,
                movies_section,
                expected_path,
            )
        if not matches:
            logger.warning(
                "[PLEX] Could not locate '%s' (%s) when updating collection '%s'.",
                title,
                year_value or "unknown year",
                resolved_collection_name,
            )
            continue

        target = matches[0]
        try:
            await asyncio.to_thread(target.addCollection, resolved_collection_name)
            label = target.title
            target_year = getattr(target, "year", None)
            if target_year:
                label = f"{label} ({target_year})"
            matched_labels.append(label)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[PLEX] Failed to tag '%s' for collection '%s': %s",
                title,
                resolved_collection_name,
                exc,
            )

    return matched_labels


async def wait_for_movies_to_be_available(
    plex_config: dict[str, str] | None,
    movies: Sequence[dict[str, Any]],
    *,
    timeout_seconds: int = PLEX_INDEX_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: int = PLEX_INDEX_POLL_INTERVAL_SECONDS,
    plex_client_factory: PlexClientFactory | None = None,
) -> bool:
    """
    Poll Plex until the provided movies are searchable or the timeout elapses.
    """
    if not plex_config or not _has_valid_plex_token(plex_config) or not movies:
        return False

    expected_movies = [
        {
            "title": str(movie.get("title") or "").strip(),
            "year": movie.get("year"),
            "destination_path": str(
                movie.get("destination_path") or movie.get("path") or ""
            ).strip(),
        }
        for movie in movies
        if str(movie.get("title") or "").strip()
    ]
    if not expected_movies:
        return False

    try:
        plex: PlexClient = await asyncio.to_thread(
            create_plex_client,
            plex_config["url"],
            plex_config["token"],
            plex_client_factory,
        )
        movies_section = await asyncio.to_thread(plex.library.section, "Movies")
    except Exception as exc:  # noqa: BLE001
        logger.error("[PLEX] Could not prepare index wait: %s", exc)
        return False

    deadline = asyncio.get_running_loop().time() + max(timeout_seconds, 1)
    pending_movies = list(expected_movies)

    while pending_movies:
        remaining: list[dict[str, Any]] = []
        for movie in pending_movies:
            movie_title = str(movie.get("title") or "").strip()
            movie_year = movie.get("year")
            expected_path = str(movie.get("destination_path") or "").strip()
            matches = await asyncio.to_thread(
                _search_movies_section,
                movies_section,
                movie_title,
                movie_year,
            )
            if not matches and expected_path:
                matches = await asyncio.to_thread(
                    _find_movie_by_path,
                    movies_section,
                    expected_path,
                )
            if not matches:
                remaining.append(movie)

        if not remaining:
            logger.info("[PLEX] Indexed %d movie(s) before timeout.", len(expected_movies))
            return True

        now = asyncio.get_running_loop().time()
        if now >= deadline:
            logger.info(
                "[PLEX] Timed out waiting for %d/%d movie(s) to become searchable in Plex.",
                len(remaining),
                len(expected_movies),
            )
            return False

        pending_movies = remaining
        await asyncio.sleep(min(poll_interval_seconds, max(deadline - now, 0)))

    return True


def _search_movies_section(
    movies_section: Any,
    title: str,
    year_value: Any,
) -> Sequence[Any]:
    candidate_results: list[Any] = []

    queries: list[dict[str, Any]] = []
    if title:
        if isinstance(year_value, int):
            queries.append({"title": title, "year": year_value})
        queries.append({"title": title})
    if isinstance(year_value, int):
        queries.append({"year": year_value})

    for params in queries:
        try:
            candidate_results.extend(movies_section.search(**params) or [])
        except Exception:
            continue

    deduped_results = _dedupe_media_items(candidate_results)
    acceptable = [
        item for item in deduped_results if _is_acceptable_media_match(item, title, year_value)
    ]
    acceptable.sort(
        key=lambda item: _score_media_title_match(item, title, year_value),
        reverse=True,
    )
    return acceptable
