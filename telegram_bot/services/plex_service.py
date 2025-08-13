# telegram_bot/services/plex_service.py

import asyncio
import os
import subprocess

from plexapi.exceptions import Unauthorized
from plexapi.server import PlexServer
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from ..config import logger


async def get_plex_server_status(context: ContextTypes.DEFAULT_TYPE) -> str:
    """
    Checks the connection to the Plex Media Server and returns a formatted status message.
    """
    plex_config = context.bot_data.get("PLEX_CONFIG", {})

    if not plex_config:
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
