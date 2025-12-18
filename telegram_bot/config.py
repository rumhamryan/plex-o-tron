# telegram_bot/config.py

import configparser
import json
import logging
import os
import sys
from typing import Any

# --- Constants ---
MAX_TORRENT_SIZE_GB = 21
MAX_TORRENT_SIZE_BYTES = MAX_TORRENT_SIZE_GB * (1024**3)
ALLOWED_EXTENSIONS = [".mkv", ".mp4"]
DELETION_ENABLED = True
PERSISTENCE_FILE = "persistence.json"
LOG_SCRAPER_STATS = True

# Setup basic logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


def get_configuration() -> (
    tuple[str, dict[str, str], list[int], dict[str, str], dict[str, Any]]
):
    """
    Reads bot token, paths, allowed IDs, Plex, and Search config from the
    config.ini file. This function has been refactored to be more robust
    and provide clearer error messages.
    """
    config_path = "config.ini"
    if not os.path.exists(config_path):
        logger.critical(
            f"Configuration file '{config_path}' not found. Please create it."
        )
        sys.exit(1)

    with open(config_path, encoding="utf-8") as f:
        lines = f.readlines()

    # --- Manually parse the [search] section to handle multi-line JSON ---
    search_config = _parse_search_section(lines)

    # --- Create a clean config for the standard parser (without the search section) ---
    config_for_parser = configparser.ConfigParser()
    clean_lines = [
        line for line in lines if not _is_in_section("[search]", line, lines)
    ]
    config_for_parser.read_string("".join(clean_lines))

    # --- Read all other values using the standard parser ---
    token = config_for_parser.get("telegram", "bot_token", fallback=None)
    if not token or token == "PLACE_TOKEN_HERE":
        logger.critical(f"Bot token not found or not set in '{config_path}'.")
        sys.exit(1)

    paths = _load_and_validate_paths(config_for_parser)

    allowed_ids_str = config_for_parser.get("telegram", "allowed_user_ids", fallback="")
    allowed_ids = (
        [int(id.strip()) for id in allowed_ids_str.split(",") if id.strip()]
        if allowed_ids_str
        else []
    )

    plex_config = _load_plex_config(config_for_parser)

    if not search_config:
        logger.info(
            "No [search] section found or it was empty. Search command now disabled."
        )

    return token, paths, allowed_ids, plex_config, search_config


def _is_in_section(
    section_header: str, current_line: str, all_lines: list[str]
) -> bool:
    """Helper to check if a line belongs to a given section."""
    try:
        index = all_lines.index(current_line)
        for i in range(index, -1, -1):
            line = all_lines[i].strip()
            if line.startswith("[") and line.endswith("]"):
                return line == section_header
        return False
    except ValueError:
        return False


def _parse_search_section(lines: list[str]) -> dict[str, Any]:
    """Extracts and parses the [search] section JSON content."""
    search_config = {}
    search_section_content = {}
    in_search_section = False
    current_key = None

    for line in lines:
        stripped_line = line.strip()
        if stripped_line == "[search]":
            in_search_section = True
            continue
        if in_search_section:
            if stripped_line.startswith("[") and stripped_line.endswith("]"):
                break  # Reached the next section
            if "=" in line and line.strip().startswith(("websites", "preferences")):
                key, value = line.split("=", 1)
                current_key = key.strip()
                search_section_content[current_key] = value.strip()
            elif current_key and not stripped_line.startswith("["):
                search_section_content[current_key] += "\n" + line

    try:
        if "websites" in search_section_content:
            search_config["websites"] = json.loads(search_section_content["websites"])
        if "preferences" in search_section_content:
            search_config["preferences"] = json.loads(
                search_section_content["preferences"]
            )
        if search_config:
            logger.info("[CONFIG] Search configuration loaded successfully.")
    except json.JSONDecodeError as e:
        logger.critical(f"Failed to parse JSON from [search] section: {e}")
        raise ValueError(f"Invalid JSON in [search] section: {e}")

    return search_config


def _load_and_validate_paths(config: configparser.ConfigParser) -> dict[str, str]:
    """
    Loads, validates, and creates all necessary media paths from the configuration.

    This function ensures that a default path is always present. If movie or
    TV show paths are not specified, they fall back to the default path. All
    configured paths are created if they do not exist.
    """
    # 1. Load the default path, which is mandatory
    default_path_str = config.get("host", "default_save_path", fallback=None)
    if not default_path_str:
        raise ValueError(
            "'default_save_path' is mandatory and was not found in the config file."
        )

    default_path = os.path.expanduser(default_path_str.strip())

    # 2. Load optional paths, falling back to the default path if they aren't specified
    movies_path_str = config.get("host", "movies_save_path", fallback=None)
    movies_path = (
        os.path.expanduser(movies_path_str.strip()) if movies_path_str else default_path
    )

    tv_shows_path_str = config.get("host", "tv_shows_save_path", fallback=None)
    tv_shows_path = (
        os.path.expanduser(tv_shows_path_str.strip())
        if tv_shows_path_str
        else default_path
    )

    # 3. Assemble the final paths dictionary. All values are guaranteed to be strings.
    paths: dict[str, str] = {
        "default": default_path,
        "movies": movies_path,
        "tv_shows": tv_shows_path,
    }

    # 4. Validate existence and create directories if necessary
    for key, path in paths.items():
        logger.info(f"[CONFIG] Resolved path for '{key}': {path}")
        if not os.path.exists(path):
            logger.info(f"Path '{path}' not found. Creating it.")
            os.makedirs(path)

    return paths


def _load_plex_config(config: configparser.ConfigParser) -> dict[str, str]:
    """Loads the Plex configuration if it exists and is valid."""
    plex_config = {}
    if config.has_section("plex"):
        plex_url = config.get("plex", "plex_url", fallback=None)
        plex_token = config.get("plex", "plex_token", fallback=None)
        if plex_url and plex_token and plex_token != "YOUR_PEX_TOKEN_HERE":
            plex_config = {"url": plex_url, "token": plex_token}
            logger.info("[INFO] Plex configuration loaded successfully.")
    return plex_config
