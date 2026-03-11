# telegram_bot/services/media_manager/plex_scan.py

import asyncio

from plexapi.exceptions import NotFound, Unauthorized
from telegram.helpers import escape_markdown

from telegram_bot.config import logger
from telegram_bot.services.interfaces import PlexClient, PlexClientFactory
from telegram_bot.services.plex_adapters import create_plex_client


async def _trigger_plex_scan(
    media_type: str | None,
    plex_config: dict[str, str] | None,
    *,
    plex_client_factory: PlexClientFactory | None = None,
) -> str:
    """Triggers a Plex library scan for the relevant library."""
    if not plex_config:
        return ""

    token_value = str(plex_config.get("token") or "").strip()
    if not token_value or token_value.upper() == "PLEX_TOKEN":
        logger.debug("Skipping Plex scan because Plex token is not configured.")
        return ""

    # Ensure media_type is a valid string before use.
    if not media_type:
        return ""

    library_map = {"movie": "Movies", "tv": "TV Shows"}
    library_name = library_map.get(media_type)

    if not library_name:
        # Handles cases where media_type is 'unknown' or unexpected.
        return ""

    logger.info(f"Attempting to scan '{library_name}' library in Plex...")
    try:
        # Run blocking PlexAPI calls in a separate thread
        plex: PlexClient = await asyncio.to_thread(
            create_plex_client,
            plex_config["url"],
            plex_config["token"],
            plex_client_factory,
        )
        target_library = await asyncio.to_thread(plex.library.section, library_name)
        await asyncio.to_thread(target_library.update)

        logger.info(f"Successfully triggered Plex scan for '{library_name}'.")
        return (
            f"\n\nPlex scan for the `{escape_markdown(library_name)}` library has been initiated\\."
        )

    except (Unauthorized, NotFound, Exception) as e:
        error_map = {
            Unauthorized: "Plex token is invalid.",
            NotFound: f"Plex library '{library_name}' not found.",
        }
        reason = error_map.get(type(e), f"An unexpected error occurred: {e}")
        logger.error(f"Plex scan failed: {reason}")
        return "\n\n*Plex Error:* Could not trigger scan\\."
