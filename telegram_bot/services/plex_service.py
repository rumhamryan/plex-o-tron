# telegram_bot/services/plex_service.py

import asyncio
import os
import subprocess
from typing import Any, Sequence, Set

from plexapi.exceptions import Unauthorized
from plexapi.server import PlexServer
from requests import exceptions as requests_exceptions
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from ..config import logger
import re


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


async def get_plex_server_status(context: ContextTypes.DEFAULT_TYPE) -> str:
    """
    Checks the connection to the Plex Media Server and returns a formatted status message.
    """
    plex_config = context.bot_data.get("PLEX_CONFIG", {})

    if not plex_config or not _has_valid_plex_token(plex_config):
        return "Plex Status: ⚪️ Not configured. Please add your Plex details to `config.ini`."

    try:
        logger.info(
            f"Attempting to connect to Plex server at {plex_config.get('url')}..."
        )

        # Run the blocking plexapi call in a separate thread
        plex = await asyncio.to_thread(
            PlexServer, plex_config["url"], plex_config["token"]
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
    Attempts to restart the Plex server by executing the 'restart_plex.sh' script.

    Returns:
        A tuple containing (success_boolean, message_string).
    """
    script_path = os.path.abspath("restart_plex.sh")

    if not os.path.exists(script_path):
        error_msg = f"Wrapper script not found at {script_path}"
        logger.error(f"[PLEX RESTART] {error_msg}")
        return (
            False,
            "The `restart_plex.sh` script was not found in the bot's directory.",
        )

    command = ["/usr/bin/sudo", script_path]

    try:
        logger.info(f"[PLEX RESTART] Executing wrapper script: {' '.join(command)}")

        # Run the blocking subprocess call in a separate thread
        await asyncio.to_thread(
            subprocess.run,
            command,
            check=True,
            capture_output=True,
            text=True,
        )

        logger.info("[PLEX RESTART] Script executed successfully.")
        return True, ""

    except subprocess.CalledProcessError as e:
        error_output = e.stderr or e.stdout
        logger.error(f"[PLEX RESTART] Script failed to execute: {error_output}")
        return False, (
            "This almost always means the `sudoers` rule for `restart_plex.sh` is "
            "incorrect or missing.\n\n*Details:*\n`{}`"
        ).format(escape_markdown(error_output, version=2))

    except Exception as e:
        logger.error(f"[PLEX RESTART] An unexpected error occurred: {e}")
        return (
            False,
            f"An unexpected error occurred:\n`{escape_markdown(str(e), version=2)}`",
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
        season_dir = os.path.join(tv_root, safe_show, f"Season {int(season):02d}")
        if not os.path.isdir(season_dir):
            return existing

        pat = re.compile(r"(?i)\bS(\d{1,2})E(\d{1,2})\b")
        for fname in os.listdir(season_dir):
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
        plex = await asyncio.to_thread(
            PlexServer, plex_config["url"], plex_config["token"]
        )
        movies_section = await asyncio.to_thread(plex.library.section, "Movies")
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[PLEX] Could not prepare collection '{collection_name}': {exc}")
        return []

    matched_labels: list[str] = []

    for movie in movies:
        title = str(movie.get("title") or "").strip()
        if not title:
            continue
        year_value = movie.get("year")

        def _search():
            params: dict[str, Any] = {"title": title}
            if isinstance(year_value, int):
                params["year"] = year_value
            try:
                results = movies_section.search(**params)
            except Exception:
                results = []
            if not results and "year" in params:
                params.pop("year", None)
                try:
                    results = movies_section.search(**params)
                except Exception:
                    results = []
            return results

        matches = await asyncio.to_thread(_search)
        if not matches:
            logger.warning(
                "[PLEX] Could not locate '%s' (%s) when updating collection '%s'.",
                title,
                year_value or "unknown year",
                collection_name,
            )
            continue

        target = matches[0]
        try:
            await asyncio.to_thread(target.addCollection, collection_name)
            label = target.title
            target_year = getattr(target, "year", None)
            if target_year:
                label = f"{label} ({target_year})"
            matched_labels.append(label)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[PLEX] Failed to tag '%s' for collection '%s': %s",
                title,
                collection_name,
                exc,
            )

    return matched_labels
