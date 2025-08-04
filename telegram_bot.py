# file: telegram_bot.py

import datetime
import wikipedia
from bs4 import BeautifulSoup, Tag
from bs4.element import Tag
import asyncio
import httpx
import json
import os
import tempfile
import time
import re
import configparser
import sys
import urllib.parse
import math
from typing import Optional, Dict, Tuple, List, Set, Any, Union
import shutil
import subprocess
import platform
from pathlib import Path
from thefuzz import process, fuzz

from plexapi.server import PlexServer
from plexapi.exceptions import NotFound, Unauthorized

from telegram import Update, Message, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ApplicationBuilder, CallbackContext, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut
import libtorrent as lt

from download_torrent import download_with_progress

# --- CONFIGURATION & NEW CONSTANTS ---
MAX_TORRENT_SIZE_GB = 10
MAX_TORRENT_SIZE_BYTES = MAX_TORRENT_SIZE_GB * (1024**3)
ALLOWED_EXTENSIONS = ['.mkv', '.mp4']
DELETION_ENABLED = False

def escape_markdown(text: str) -> str:
    """Helper function to escape telegram's special characters."""
    escape_chars = r'_*[]()~`>#+-=|{}.!\\'
    return re.sub(rf'([{re.escape(escape_chars)}])', r'\\\1', text)

def get_help_message_text() -> str:
    """(NEW) Returns the formatted help message string to be reused."""
    return (
        "Here are the available commands:\n\n"
        "`delete`   \- Delete Movies or TV Shows\.\n"
        "`help`       \- Displays this message\. \n"
        "`links`     \- Lists popular torrent sites\.\n"
        "`restart` \- Restarts the Plex Server\.\n"
        "`status`   \- Checks Plex server status\."
    )

def get_configuration() -> tuple[str, dict, list[int], dict, dict]:
    """
    Reads bot token, paths, allowed IDs, Plex and Search config from the config.ini file.
    (Refactored to manually parse the [search] section and feed a cleaned config
    to the standard parser to avoid parsing errors.)
    """
    config_path = 'config.ini'
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file '{config_path}' not found. Please create it.")

    with open(config_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # --- STEP 1: Manually find and parse the [search] section ---
    search_config = {}
    search_section_content = {}
    in_search_section = False
    current_key = None
    
    for line in lines:
        stripped_line = line.strip()
        if stripped_line == '[search]':
            in_search_section = True
            continue

        if in_search_section:
            if stripped_line.startswith('[') and stripped_line.endswith(']'):
                # We've reached the next section, so stop processing for search.
                in_search_section = False
                continue
            
            # Identify new key=value pairs within the search section
            if '=' in line and line.strip().startswith(('websites', 'preferences')):
                key, value = line.split('=', 1)
                current_key = key.strip()
                search_section_content[current_key] = value.strip()
            # Append subsequent lines of a multiline value
            elif current_key and not stripped_line.startswith('['):
                 search_section_content[current_key] += '\n' + line
    
    try:
        if 'websites' in search_section_content:
            search_config['websites'] = json.loads(search_section_content['websites'])
        if 'preferences' in search_section_content:
            search_config['preferences'] = json.loads(search_section_content['preferences'])
        
        if search_config:
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [CONFIG] Search configuration loaded successfully.")
    except json.JSONDecodeError as e:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [CRITICAL] Failed to parse JSON from [search] section: {e}")
        raise ValueError(f"Invalid JSON in [search] section: {e}")

    # --- STEP 2: Create a clean config for the standard parser ---
    # This version of the config has the [search] section removed.
    config_for_parser = configparser.ConfigParser()
    clean_lines = [line for line in lines if not line.strip().startswith(('websites =', 'preferences =')) and '[search]' not in line]
    
    # Heuristically remove the JSON content lines
    final_clean_lines = []
    in_multiline = False
    for line in lines:
        stripped = line.strip()
        if stripped == '[search]':
            in_multiline = True # Start skipping lines from here
        elif stripped.startswith('[') and stripped.endswith(']'):
            in_multiline = False # Stop skipping when a new section is found
        
        if not in_multiline:
            final_clean_lines.append(line)

    config_for_parser.read_string("".join(final_clean_lines))

    # --- STEP 3: Read all other values using the clean parser object ---
    token = config_for_parser.get('telegram', 'bot_token', fallback=None)
    if not token or token == "PLACE_TOKEN_HERE":
        raise ValueError(f"Bot token not found or not set in '{config_path}'.")
        
    paths = {
        'default': config_for_parser.get('host', 'default_save_path', fallback=None),
        'movies': config_for_parser.get('host', 'movies_save_path', fallback=None),
        'tv_shows': config_for_parser.get('host', 'tv_shows_save_path', fallback=None)
    }

    for key, value in paths.items():
        if value:
            paths[key] = os.path.expanduser(value.strip())
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [CONFIG] Resolved path for '{key}': {paths[key]}")

    if not paths['default']:
        raise ValueError("'default_save_path' is mandatory and was not found in the config file.")

    for path_type, path_value in paths.items():
        if path_value and not os.path.exists(path_value):
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] INFO: {path_type.capitalize()} path '{path_value}' not found. Creating it.")
            os.makedirs(path_value)

    allowed_ids_str = config_for_parser.get('telegram', 'allowed_user_ids', fallback='')
    allowed_ids = [int(id.strip()) for id in allowed_ids_str.split(',') if id.strip()] if allowed_ids_str else []

    plex_config = {}
    if config_for_parser.has_section('plex'):
        plex_url = config_for_parser.get('plex', 'plex_url', fallback=None)
        plex_token = config_for_parser.get('plex', 'plex_token', fallback=None)
        if plex_url and plex_token and plex_token != "YOUR_PEX_TOKEN_HERE":
            plex_config = {'url': plex_url, 'token': plex_token}
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Plex configuration loaded successfully.")

    if not search_config:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] No [search] section found or it was empty. Search command will be disabled.")

    return token, paths, allowed_ids, plex_config, search_config

async def handle_link_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (CORRECTED) A dedicated handler for messages that are clearly links (magnet or http).
    """
    if not await is_user_authorized(update, context):
        return
        
    if not update.message or not update.message.text: return
    if context.user_data is None: context.user_data = {}

    text = update.message.text.strip()
    
    progress_message = await update.message.reply_text("✅ Link received. Analyzing...")
    try:
        await update.message.delete()
    except BadRequest:
        pass

    ti = await process_user_input(text, context, progress_message)
    if not ti: return

    error_message, parsed_info = await validate_and_enrich_torrent(ti, progress_message)
    if error_message or not parsed_info:
        if 'torrent_file_path' in context.user_data and os.path.exists(context.user_data['torrent_file_path']):
            os.remove(context.user_data['torrent_file_path'])
        return

    await send_confirmation_prompt(progress_message, context, ti, parsed_info)

async def _perform_chat_clear(chat_id: int, up_to_message_id: int, application: Application):
    """
    (REVISED) Core logic to delete messages, now with summarized logging
    for non-deletable messages.
    """
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] [CORE CLEAR] Performing clear for chat {chat_id} up to message {up_to_message_id}.")
    
    # --- REVISED LOGIC: Collect failed IDs instead of logging them one by one ---
    failed_to_delete_ids = []

    for message_id in range(up_to_message_id, 0, -1):
        try:
            await application.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except BadRequest as e:
            # Check for expected errors (old message, not found)
            if "message can't be deleted" in str(e) or "message to delete not found" in str(e):
                failed_to_delete_ids.append(message_id)
            else:
                # Log only truly unexpected errors
                # print(f"[{ts}] [CORE CLEAR] Unexpected error deleting message {message_id}: {e}")
                pass
        except Exception:
            # Catch any other network-related errors silently
            failed_to_delete_ids.append(message_id)

    # After the loop, log the summary of non-deletable messages
    if failed_to_delete_ids:
        compressed_output = _compress_message_ranges(failed_to_delete_ids)
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [CORE CLEAR] Could not delete messages (too old or not found): {compressed_output}")

async def schedule_delayed_clear(chat_id: int, last_message_id: int, application: Application):
    """
    (REVISED) Schedules a chat clear operation and sends a help message upon completion.
    """
    user_data = application.user_data.get(chat_id, {})

    if user_data.get('pending_clear_task'):
        return

    async def clear_task():
        """
        The actual task that waits, sends the new help message first, and then
        clears the old messages in the background.
        """
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts}] [AUTO-CLEAR] Starting 10-second countdown for chat {chat_id}.")
        await asyncio.sleep(10)
        
        current_user_data = application.user_data.get(chat_id, {})
        
        queues = application.bot_data.get('download_queues', {})
        active_downloads = application.bot_data.get('active_downloads', {})
        if queues.get(str(chat_id)) or active_downloads.get(str(chat_id)):
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [AUTO-CLEAR] Aborting clear for chat {chat_id}; an item was queued or is active.")
            current_user_data.pop('pending_clear_task', None)
            return

        # --- THE FIX: Send the new message BEFORE starting the slow deletion ---
        new_message = None
        try:
            ts_hello = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[{ts_hello}] [AUTO-CLEAR] Sending pre-clear help message to chat {chat_id} first.")
            help_text = get_help_message_text()
            new_message = await application.bot.send_message(
                chat_id=chat_id,
                text=help_text,
                parse_mode=ParseMode.MARKDOWN_V2
            )
        except Exception as e:
            ts_err = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[{ts_err}] [AUTO-CLEAR] Failed to send pre-clear help message: {e}")
        # --- End of fix ---

        # The message ID to clear up to is the one right before the new message we just sent.
        # If sending failed, we fall back to the original last_message_id.
        clear_up_to_id = new_message.message_id -1 if new_message else last_message_id

        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [AUTO-CLEAR] Countdown finished. Executing background clear for chat {chat_id} up to message {clear_up_to_id}.")
        await _perform_chat_clear(chat_id, clear_up_to_id, application)
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [AUTO-CLEAR] Background clear completed for chat {chat_id}.")

        current_user_data.pop('pending_clear_task', None)

    task = asyncio.create_task(clear_task())
    user_data['pending_clear_task'] = task

async def clear_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (REVISED) Manually triggers the chat clearing process.
    """
    if not await is_user_authorized(update, context):
        return
    if not update.message: return

    # Pass the application object from the context
    await _perform_chat_clear(update.message.chat_id, update.message.message_id, context.application)

def _compress_message_ranges(message_ids: List[int]) -> str:
    """
    (NEW HELPER) Compresses a list of integers into a string of ranges.
    Example: [56, 55, 54, 50, 49, 40] -> "56-54, 50-49, 40"
    """
    if not message_ids:
        return ""

    # Sort the unique IDs in descending order to create ranges like "56-1"
    sorted_ids = sorted(list(set(message_ids)), reverse=True)
    
    ranges = []
    range_start = sorted_ids[0]

    for i in range(1, len(sorted_ids)):
        # If the current ID is not consecutive, the previous range has ended
        if sorted_ids[i] != sorted_ids[i-1] - 1:
            range_end = sorted_ids[i-1]
            if range_start == range_end:
                ranges.append(str(range_start))
            else:
                ranges.append(f"{range_start}-{range_end}")
            range_start = sorted_ids[i]
    
    # Add the final range after the loop is done
    if range_start == sorted_ids[-1]:
        ranges.append(str(range_start))
    else:
        ranges.append(f"{range_start}-{sorted_ids[-1]}")
        
    return ", ".join(ranges)

def parse_torrent_name(name: str) -> dict:
    """
    Parses a torrent name to identify if it's a movie or a TV show
    and extracts relevant metadata.
    """
    # Normalize by replacing dots and underscores with spaces
    cleaned_name = re.sub(r'[\._]', ' ', name)
    
    # --- TV Show Detection (unchanged) ---
    tv_match = re.search(r'(?i)\b(S(\d{1,2})E(\d{1,2})|(\d{1,2})x(\d{1,2}))\b', cleaned_name)
    if tv_match:
        title = cleaned_name[:tv_match.start()].strip()
        if tv_match.group(2) is not None:
            season = int(tv_match.group(2))
            episode = int(tv_match.group(3))
        else:
            season = int(tv_match.group(4))
            episode = int(tv_match.group(5))
        title = re.sub(r'[\s-]+$', '', title).strip()
        return {'type': 'tv', 'title': title, 'season': season, 'episode': episode}

    # --- Movie Detection ---
    year_match = re.search(r'\b(19\d{2}|20\d{2})\b', cleaned_name)
    if year_match:
        year = year_match.group(1)
        title = cleaned_name[:year_match.start()].strip()
        
        # --- FIX STARTS HERE ---
        # Remove any trailing spaces, parentheses, or hyphens from the title
        title = re.sub(r'[\s(\)-]+$', '', title).strip()
        # --- FIX ENDS HERE ---

        return {'type': 'movie', 'title': title, 'year': year}

    # --- Fallback for names that don't match movie/TV patterns (unchanged) ---
    tags_to_remove = [
        r'\[.*?\]', r'\(.*?\)',
        r'\b(1080p|720p|480p|x264|x265|hevc|BluRay|WEB-DL|AAC|DTS|HDTV|RM4k|CC|10bit|commentary|HeVK)\b'
    ]
    regex_pattern = '|'.join(tags_to_remove)
    no_ext = os.path.splitext(cleaned_name)[0]
    title = re.sub(regex_pattern, '', no_ext, flags=re.I)
    title = re.sub(r'\s+', ' ', title).strip()
    return {'type': 'unknown', 'title': title}

def generate_plex_filename(parsed_info: dict, original_extension: str) -> str:
    """Generates a clean, Plex-friendly filename from the parsed info."""
    title = parsed_info.get('title', 'Unknown Title')
    
    # Sanitize title to remove characters invalid for filenames
    invalid_chars = r'<>:"/\|?*'
    safe_title = "".join(c for c in title if c not in invalid_chars)

    if parsed_info.get('type') == 'movie':
        year = parsed_info.get('year', 'Unknown Year')
        return f"{safe_title} ({year}){original_extension}"
    
    elif parsed_info.get('type') == 'tv':
        season = parsed_info.get('season', 0)
        episode = parsed_info.get('episode', 0)
        episode_title = parsed_info.get('episode_title')
        
        safe_episode_title = ""
        if episode_title:
            safe_episode_title = " - " + "".join(c for c in episode_title if c not in invalid_chars)
            
        # MODIFIED: Return format is now "sXXeXX - Episode Title.ext"
        return f"s{season:02d}e{episode:02d}{safe_episode_title}{original_extension}"
        
    else: # Fallback for 'unknown' type
        return f"{safe_title}{original_extension}"
    
def _normalize_movie_name_for_search(text: str) -> str:
    """
    (CORRECTED) Normalizes a movie title for searching by:
    1. Removing the file extension.
    2. Removing the year in parentheses (e.g., "(1986)").
    3. Stripping leading "## - " or "##. " type prefixes.
    4. Converting to lowercase.
    5. Removing all non-alphanumeric characters.
    """
    # 1. Remove extension (This was the missing, critical step)
    name_without_ext, _ = os.path.splitext(text)
    
    # 2. Remove year in parentheses
    name_without_year = re.sub(r'\s*\(\d{4}\)', '', name_without_ext)
    
    # 3. Strip leading prefixes
    name_without_prefix = re.sub(r'^\s*\d+\s*[\-.]\s*', '', name_without_year)
    
    # 4 & 5. Lowercase and remove non-alphanumeric
    return re.sub(r'[^a-z0-9]', '', name_without_prefix.lower())
    
async def find_media_by_name(
    media_type: str,
    search_query: str,
    save_paths: Dict[str, str],
    search_target: str = 'any'
) -> Union[str, List[str], None]:
    """
    (CORRECTED) Finds media by name, returning a single path for a unique best
    match, or a list of paths if multiple matches share the top score.
    """
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] [DELETE SEARCH] Initiated for type='{media_type}', query='{search_query}'")

    search_path_key = 'movies' if media_type == 'movie' else 'tv_shows'
    search_path_str = save_paths.get(search_path_key)

    if not search_path_str or not Path(search_path_str).is_dir():
        print(f"[{ts}] [DELETE SEARCH] ERROR: Invalid or missing path for '{search_path_key}'.")
        return None

    search_dir = Path(search_path_str)
    normalized_query = _normalize_movie_name_for_search(search_query)

    def perform_search() -> Union[str, List[str], None]:
        all_candidates = []
        perfect_matches = []

        # --- STAGE 1: Scan and find Perfect Matches ---
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DELETE SEARCH] Stage 1: Scanning for perfect matches.")
        for p in search_dir.rglob('*'):
            is_correct_type = (search_target == 'any') or \
                              (search_target == 'directory' and p.is_dir()) or \
                              (search_target == 'file' and p.is_file())

            if is_correct_type:
                all_candidates.append(p)
                if _normalize_movie_name_for_search(p.name) == normalized_query:
                    perfect_matches.append(str(p))

        if len(perfect_matches) == 1:
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DELETE SEARCH] Found single perfect match: {perfect_matches[0]}")
            return perfect_matches[0]
        if len(perfect_matches) > 1:
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DELETE SEARCH] Found {len(perfect_matches)} perfect matches. Returning list for user selection.")
            return perfect_matches

        # --- STAGE 2: Fallback to Fuzzy Matching ---
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DELETE SEARCH] No perfect match found. Proceeding to Stage 2: Fuzzy matching.")
        if not all_candidates:
            return None

        choices = {str(p): p.name for p in all_candidates}
        
        # --- THE FIX: Call extract first, then filter the results ---
        results = process.extract(
            search_query,
            choices,
            scorer=fuzz.token_set_ratio,
            limit=5 
        )
        
        # Manually filter results by score
        filtered_results = [res for res in results if res[1] >= 80]
        # --- End of fix ---

        if not filtered_results:
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DELETE SEARCH] Fuzzy search found no results above score threshold.")
            return None

        highest_score = filtered_results[0][1]
        best_matches = [res[0] for res in filtered_results if res[1] == highest_score]

        if len(best_matches) == 1:
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DELETE SEARCH] Found single best fuzzy match: '{best_matches[0]}' with score {highest_score}")
            return best_matches[0]
        else:
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DELETE SEARCH] Found {len(best_matches)} fuzzy matches sharing the top score of {highest_score}. Returning list.")
            return best_matches

    # --- THE FIX: Assign to a typed variable before returning to help the linter ---
    final_result: Union[str, List[str], None] = await asyncio.to_thread(perform_search)
    return final_result
    # --- End of fix ---

async def find_season_directory(show_path: str, season_number: int) -> Optional[str]:
    """
    (REBUILT) Finds a season directory using a flexible regex search.
    Looks for the season number as a whole word (e.g., '1' or '01').
    """
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] [DELETE SEARCH] Searching for season {season_number} in path: {show_path}")

    def perform_search():
        # --- THE FIX: Use a robust regular expression ---
        # This pattern looks for the season number as a whole word.
        # \b ensures that searching for '1' doesn't match '10'.
        # It checks for both the plain number (1) and the zero-padded version (01).
        pattern = re.compile(rf'\b({season_number}|{str(season_number).zfill(2)})\b')
        
        for item in os.listdir(show_path):
            item_path = os.path.join(show_path, item)
            if os.path.isdir(item_path):
                # Check if the directory name contains the season number as a whole word.
                if pattern.search(item):
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DELETE SEARCH] Flexible match found for season directory: {item_path}")
                    return item_path
        # --- End of fix ---
        
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DELETE SEARCH] No season directory found for season {season_number}.")
        return None

    return await asyncio.to_thread(perform_search)

async def find_episode_file(season_path: str, season_number: int, episode_number: int) -> Optional[str]:
    """
    (REVISED) Finds an episode file within a season's directory using more flexible patterns.
    Looks for formats like 's01e01', 's01.e01', '1x01', '1.01' etc. and also fixes logging.
    """
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # --- FIX: Use zfill in the log message to match the search logic and user's request. ---
    s_num_padded = str(season_number).zfill(2)
    e_num_padded = str(episode_number).zfill(2)
    print(f"[{ts}] [DELETE SEARCH] Searching for s{s_num_padded}e{e_num_padded} in path: {season_path}")

    def perform_search():
        # This handles cases with and without common separators like '.', '_', or ' '.
        patterns = [
            # Formats like: s01e01
            f"s{s_num_padded}e{e_num_padded}", 
            # Formats like: 1x01
            f"{season_number}x{e_num_padded}",
            # Formats like: s01.e01 or s01-e01
            f"s{s_num_padded}.e{e_num_padded}",
            f"s{s_num_padded}-e{e_num_padded}",
            # Formats like: 1.01 or 1-01
            f"{season_number}.{e_num_padded}",
            f"{season_number}-{e_num_padded}",
        ]
        
        for item in os.listdir(season_path):
            item_path = os.path.join(season_path, item)
            if os.path.isfile(item_path):
                # Normalize the filename by replacing spaces and underscores with dots for better matching
                normalized_item_name = item.lower().replace(' ', '.').replace('_', '.')
                for pattern in patterns:
                    if pattern in normalized_item_name:
                        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DELETE SEARCH] Found episode file: {item_path} (using pattern: '{pattern}')")
                        return item_path
                        
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DELETE SEARCH] No episode file found.")
        return None
        
    return await asyncio.to_thread(perform_search)

def _parse_codec(title: str) -> str:
    """
    (REVISED) Extracts a video codec from a torrent title string using more
    precise, order-dependent checking.
    """
    # Normalize the title for consistent checking
    title_lower = title.lower().replace('.', ' ')
    
    # Use word boundaries to avoid partial matches (e.g., in group names)
    if re.search(r'\b(x265|hevc)\b', title_lower):
        return "x265"
    if re.search(r'\b(x264|h264)\b', title_lower):
        return "x264"
    
    return "N/A"

def _parse_size_to_gb(size_str: str) -> float:
    """
    (REVISED) Parses a size string (e.g., '4.3 GiB', '800 MB') and returns size in GB,
    correctly handling GiB/MiB conversions.
    """
    if not isinstance(size_str, str):
        return 0.0
    
    # Normalize the string for easier parsing
    size_str_upper = size_str.strip().upper()
    
    try:
        # Extract the numeric value
        match = re.search(r'([\d\.]+)', size_str_upper)
        if not match:
            return 0.0
        
        value = float(match.group(1))
        
        # Apply the correct conversion factor based on the unit
        # 1 Gibibyte (GiB) is ~7.37% larger than 1 Gigabyte (GB)
        if 'GIB' in size_str_upper:
            return value * 1.07374
        # 1 Mebibyte (MiB) is ~4.85% larger than 1 Megabyte (MB)
        elif 'MIB' in size_str_upper:
            return (value * 1.048576) / 1024
        elif 'MB' in size_str_upper:
            return value / 1024
        # Default to GB if no specific binary prefix is found
        elif 'GB' in size_str_upper:
            return value
        else:
            return 0.0
            
    except (ValueError, TypeError):
        return 0.0

def _score_1337x_result(name: str, uploader: str, preferences: dict) -> int:
    """
    Calculates a score for a torrent result based on configured preferences.
    Now with title normalization.
    """
    score = 0
    # --- THE FIX: Normalize the name by replacing dots with spaces. ---
    name_lower = name.lower().replace('.', ' ')
    
    # Score based on resolution
    res_prefs = preferences.get('resolutions', {})
    for res, points in res_prefs.items():
        if res.lower() in name_lower:
            score += points
            break # Assume only one resolution match is needed

    # Score based on codec
    codec_prefs = preferences.get('codecs', {})
    for codec, points in codec_prefs.items():
        if codec.lower() in name_lower:
            score += points
            break # Assume only one codec match is needed

    # Score based on uploader
    uploader_prefs = preferences.get('uploaders', {})
    for up, points in uploader_prefs.items():
        if up.lower() == uploader.lower():
            score += points
            break

    return score

async def _scrape_1337x(
    query: str,
    media_type: str,
    search_url_template: str,
    context: ContextTypes.DEFAULT_TYPE
) -> List[Dict[str, Any]]:
    """
    (DEBUG & TYPE-SAFE) Scrapes 1337x.to with heavy debugging and robust type
    checks to prevent runtime errors and satisfy IDE linters.
    """
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    results = []
    
    search_config = context.bot_data.get("SEARCH_CONFIG", {})
    prefs_key = 'movies' if 'movie' in media_type else 'tv'
    preferences = search_config.get("preferences", {}).get(prefs_key, {})
    
    if not preferences:
        print(f"[{ts}] [SCRAPER] No preferences found for media type '{prefs_key}'. Cannot score 1337x results.")
        return []

    formatted_query = urllib.parse.quote_plus(query)
    search_url = search_url_template.replace("{query}", formatted_query)
    print(f"[{ts}] [SCRAPER] Scraping 1337x for torrents: {search_url}")

    try:
        headers = {
            'authority': '1337x.to', 'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        }

        # --- THE FIX: Removed http2=True to prevent dependency errors ---
        async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
        # --- End of fix ---
            response = await client.get(search_url)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'lxml')

        table_body = soup.find('tbody')
        if not isinstance(table_body, Tag):
            print(f"[{ts}] [SCRAPER] Could not find table body on 1337x page.")
            return []

        base_url = "https://1337x.to"
        for i, row in enumerate(table_body.find_all('tr')):
            if not isinstance(row, Tag): continue

            cells = row.find_all('td')
            if len(cells) < 6: continue
            
            name_cell, seeds_cell, size_cell, uploader_cell = cells[0], cells[1], cells[4], cells[5]

            if not isinstance(name_cell, Tag): continue
            links = name_cell.find_all('a')
            if len(links) < 2: continue
            link_tag = links[1]
            if not isinstance(link_tag, Tag): continue
                
            title = link_tag.get_text(strip=True)
            page_url_relative = link_tag.get('href')

            if not isinstance(size_cell, Tag): continue
            size_str = size_cell.get_text(strip=True)

            if not isinstance(seeds_cell, Tag): continue
            seeds_str = seeds_cell.get_text(strip=True)
            
            parsed_size_gb = _parse_size_to_gb(size_str)
            if parsed_size_gb > 7.0: continue

            if not isinstance(uploader_cell, Tag): continue
            uploader_tag = uploader_cell.find('a')
            uploader = uploader_tag.get_text(strip=True) if isinstance(uploader_tag, Tag) else "Anonymous"

            if not title or not page_url_relative or not isinstance(page_url_relative, str): continue
                
            page_url = f"{base_url}{page_url_relative}"
            score = _score_torrent_result(title, uploader, preferences)

            if score > 0:
                results.append({
                    'title': title, 'page_url': page_url, 'score': score,
                    'source': '1337x', 'uploader': uploader,
                    'size_gb': parsed_size_gb, 'codec': _parse_codec(title),
                    'seeders': int(seeds_str) if seeds_str.isdigit() else 0
                })

    except Exception as e:
        print(f"[{ts}] [SCRAPER ERROR] An unexpected error occurred during 1337x scrape: {e}")

    print(f"[{ts}] [SCRAPER] 1337x scrape finished. Found {len(results)} scored results.")
    return results

async def _scrape_yts(
    query: str,
    media_type: str,
    search_url_template: str,
    context: ContextTypes.DEFAULT_TYPE,
    resolution: Optional[str] = None,
    year: Optional[str] = None, # Added year parameter
) -> List[Dict[str, Any]]:
    """
    (API-REWRITE-FIXED) Uses the YTS.mx API and now filters by year in Stage 1.
    """
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] [SCRAPER] YTS: Initiating API-based scrape for '{query}' ({year or 'any year'}).")

    preferences = context.bot_data.get("SEARCH_CONFIG", {}).get("preferences", {}).get(media_type, {})
    if not preferences: return []
    
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
            
            # --- STAGE 1: Find Movie ID, now with year filtering ---
            movie_id = None
            formatted_query = urllib.parse.quote_plus(query)
            search_url = search_url_template.replace("{query}", formatted_query)
            
            response = await client.get(search_url, headers=headers)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'lxml')

            # --- THE FIX: Filter choices by year before fuzzy matching ---
            choices = {}
            for movie_wrapper in soup.find_all('div', class_='browse-movie-wrap'):
                if not isinstance(movie_wrapper, Tag): continue

                year_tag = movie_wrapper.find('div', class_='browse-movie-year')
                scraped_year = year_tag.get_text(strip=True) if isinstance(year_tag, Tag) else None

                # If a year is specified by the user, only consider movies from that year
                if year and scraped_year and year != scraped_year:
                    continue # Skip this movie, it's the wrong year

                title_tag = movie_wrapper.find('a', class_='browse-movie-title')
                if isinstance(title_tag, Tag):
                    href = title_tag.get('href')
                    title_text = title_tag.get_text(strip=True)
                    if href and title_text:
                        choices[href] = title_text
            
            if not choices:
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [SCRAPER] YTS Stage 1: No movies found matching year '{year}'.")
                return []

            best_match = process.extractOne(query, choices, scorer=fuzz.ratio)
            
            if not (best_match and len(best_match) > 2 and best_match[1] > 70 and isinstance(url_candidate := best_match[2], str)):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [SCRAPER] YTS Stage 1: No confident match found for '{query}'.")
                return []
            
            best_page_url = url_candidate
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [SCRAPER] YTS Stage 1: Best match is '{best_match[0]}'. URL: {best_page_url}")

            response = await client.get(best_page_url, headers=headers)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'lxml')
            
            movie_info_div = soup.select_one('#movie-info')
            if isinstance(movie_info_div, Tag): movie_id = movie_info_div.get('data-movie-id')
            if not movie_id:
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [SCRAPER ERROR] YTS Stage 1: Could not find data-movie-id on page {best_page_url}")
                return []

            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [SCRAPER] YTS Stage 2: Calling API with movie_id '{movie_id}' for resolution '{resolution}'")
            results = []
            api_url = f"https://yts.mx/api/v2/movie_details.json?movie_id={movie_id}"
            
            response = await client.get(api_url)
            response.raise_for_status()
            api_data = response.json()

            if api_data.get('status') != 'ok' or 'movie' not in api_data.get('data', {}):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [SCRAPER ERROR] YTS API returned an error: {api_data.get('status_message')}")
                return []

            movie_data = api_data['data']['movie']
            movie_title = movie_data.get('title_long', query)
            api_torrents = movie_data.get('torrents', [])
            
            for torrent in api_torrents:
                quality = torrent.get('quality', '').lower()
                if resolution and resolution.lower() in quality:
                    
                    size_gb = torrent.get('size_bytes', 0) / (1024**3)
                    if size_gb > 7.0: continue

                    full_title = f"{movie_title} [{torrent.get('quality')}.{torrent.get('type')}] [YTS.MX]"
                    info_hash = torrent.get('hash')
                    
                    if info_hash:
                        trackers = "&tr=" + "&tr=".join(["udp://open.demonii.com:1337/announce", "udp://tracker.openbittorrent.com:80", "udp://tracker.coppersurfer.tk:6969", "udp://glotorrents.pw:6969/announce", "udp://tracker.opentrackr.org:1337/announce", "udp://torrent.gresille.org:80/announce", "udp://p4p.arenabg.com:1337", "udp://tracker.leechers-paradise.org:6969"])
                        magnet_link = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote_plus(movie_title)}{trackers}"
                        score = _score_torrent_result(full_title, "YTS", preferences)
                        
                        # --- THE FIX: Default YTS codec to x264 if not found ---
                        parsed_codec = _parse_codec(full_title)
                        if parsed_codec == 'N/A':
                            parsed_codec = 'x264'
                        # --- End of fix ---

                        results.append({
                            'title': full_title, 'page_url': magnet_link,
                            'score': score, 'source': 'YTS.mx', 'uploader': 'YTS',
                            'size_gb': size_gb, 'codec': parsed_codec,
                            'seeders': torrent.get('seeds', 0)
                        })
            
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [SCRAPER] YTS API scrape finished. Found {len(results)} matching torrents.")
            return results

    except Exception as e:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [SCRAPER ERROR] YTS scrape failed entirely: {e}")
        return []

async def _search_for_media(
    query: str, 
    media_type: str, 
    site_name: str,
    context: ContextTypes.DEFAULT_TYPE,
    year: Optional[str] = None,
    resolution: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], str]:
    """
    (CORRECTED) Orchestrates scraping. Now includes the resolution in the 1337x
    search query to ensure a more relevant set of results is returned from the site.
    """
    search_config = context.bot_data.get("SEARCH_CONFIG", {})
    if not search_config: return [], "Search Not Configured"

    websites_for_type = search_config.get("websites", {}).get(media_type, [])
    site_to_search = next((site for site in websites_for_type if site.get("name") == site_name), None)

    if not site_to_search:
        return [], f"Site '{site_name}' not configured for media type '{media_type}'"

    search_url = site_to_search.get("search_url")
    if not search_url: return [], "Invalid site config"
    
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    results = []
    if site_name == "YTS.mx":
        print(f"[{ts}] [SEARCH] Routing search for '{query}' ({year or 'any year'}) to site: {site_name}")
        results = await _scrape_yts(query, media_type, search_url, context, resolution=resolution, year=year)
    elif site_name == "1337x":
        # --- THE FIX: Re-add resolution to the 1337x search query ---
        query_parts = [query]
        if media_type == 'movies' and year:
            query_parts.append(year)
        if resolution:
            query_parts.append(resolution)
        search_query_for_site = " ".join(query_parts)
        # --- End of fix ---
        
        print(f"[{ts}] [SEARCH] Routing search for '{search_query_for_site}' to site: {site_name}")
        results = await _scrape_1337x(search_query_for_site, media_type, search_url, context)
    else:
        print(f"[{ts}] [SEARCH] No scraper implemented for '{site_name}' yet.")

    if results:
        results.sort(key=lambda x: x.get('score', 0), reverse=True)
        print(f"[{ts}] [SEARCH] Filtered and sorted {len(results)} candidates for site {site_name}.")
        return results, site_name

    return [], site_name
    
def _extract_first_int(text: str) -> Optional[int]:
    """Safely extracts the first integer from a string, ignoring trailing characters."""
    if not text:
        return None
    match = re.search(r'\d+', text.strip()) # Changed from re.match to re.search
    if match:
        return int(match.group(0))
    return None

async def _parse_dedicated_episode_page(soup: BeautifulSoup, season: int, episode: int) -> Optional[str]:
    """
    (Primary Strategy - DEFINITIVE)
    Parses a dedicated 'List of...' page by using the 'Series overview'
    table to calculate the exact index of the target season's table.
    """
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] [WIKI] Trying Primary Strategy: Index Calculation via Overview Table")

    all_tables = soup.find_all('table', class_='wikitable')
    if not all_tables:
        return None

    # --- Step 1: Find the "Series overview" table to use as an index ---
    index_table = None
    first_table = all_tables[0]
    if isinstance(first_table, Tag):
        first_row = first_table.find('tr')
        if isinstance(first_row, Tag):
            headers = [th.get_text(strip=True) for th in first_row.find_all('th')]
            if headers and headers[0] == 'Season':
                index_table = first_table

    if not index_table or not isinstance(index_table, Tag):
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI] Could not find 'Series overview' table. Aborting Primary Strategy.")
        return None

    # --- Step 2: Calculate the target table's actual index ---
    target_table_index = -1
    # The counter starts at 1, representing the index of the first table *after* the overview table.
    current_table_index_counter = 0
    
    rows = index_table.find_all('tr')[1:] # Skip header
    for row in rows:
        if not isinstance(row, Tag): continue
        cells = row.find_all(['th', 'td'])
        if not cells: continue
        
        season_num_from_cell = _extract_first_int(cells[0].get_text(strip=True))
        
        if season_num_from_cell == season:
            target_table_index = current_table_index_counter
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI] Match for Season {season} found. Calculated target table index: {target_table_index}")
            break
        
        # IMPORTANT: Increment the counter *after* the check.
        current_table_index_counter += 1

    if target_table_index == -1:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI] Could not find Season {season} in the index table.")
        return None

    if target_table_index >= len(all_tables):
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI ERROR] Calculated index {target_table_index} is out of bounds (Total tables: {len(all_tables)}).")
        return None

    # --- Step 3: Parse the correct table using the calculated index ---
    target_table = all_tables[target_table_index]
    if not isinstance(target_table, Tag): return None

    for row in target_table.find_all('tr')[1:]:
        if not isinstance(row, Tag): continue
        cells = row.find_all(['td', 'th'])
        # A valid row must have at least 3 columns for this page type
        if len(cells) < 3: continue

        try:
            # The episode number is in the second column (index 1) of season tables
            episode_num_from_cell = _extract_first_int(cells[1].get_text(strip=True))

            if episode_num_from_cell == episode:
                # The title is in the third column (index 2)
                title_cell = cells[2]
                if not isinstance(title_cell, Tag): continue
                
                found_text = title_cell.find(string=re.compile(r'"([^"]+)"'))
                if found_text:
                    cleaned_title = str(found_text).strip().strip('"')
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [SUCCESS] Found title via Primary Strategy: '{cleaned_title}'")
                    return cleaned_title
                else:
                    cleaned_title = title_cell.get_text(strip=True)
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not find title in quotes, using full cell text: '{cleaned_title}'")
                    return cleaned_title
        except (ValueError, IndexError):
            continue
            
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI] Primary Strategy failed to find the episode in the correct table.")
    return None

async def _parse_embedded_episode_page(soup: BeautifulSoup, season: int, episode: int) -> Optional[str]:
    """
    (Fallback Strategy - HEAVY DEBUGGING & TYPE SAFE)
    Parses a page using proven logic for embedded episode lists.
    """
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI DEBUG] === Trying Fallback Strategy: Flexible Row Search ===")
    
    tables = soup.find_all('table', class_='wikitable')
    for table_idx, table in enumerate(tables):
        if not isinstance(table, Tag): continue
        
        # --- FIX: Safely find headers to prevent IDE errors ---
        headers = []
        first_row = table.find('tr')
        if isinstance(first_row, Tag):
            headers = [th.get_text(strip=True) for th in first_row.find_all('th')]
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI INSPECT] Table {table_idx+1}/{len(tables)} Headers: {headers}")

        rows = table.find_all('tr')
        for row in rows[1:]:
            if not isinstance(row, Tag): continue
            cells = row.find_all(['td', 'th'])
            if len(cells) < 2: continue

            try:
                cell_texts = [c.get_text(strip=True) for c in cells]
                match_found = False
                row_text_for_match = ' '.join(cell_texts[:2])
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI DEBUG]   Searching row text: '{row_text_for_match}'")

                if re.search(fr'\b{season}\b.*\b{episode}\b', row_text_for_match):
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI DEBUG]     Heuristic 1 MATCH on row.")
                    match_found = True
                
                elif season == 1 and re.fullmatch(str(episode), cell_texts[0]):
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI DEBUG]     Heuristic 2 MATCH on row.")
                    match_found = True

                if match_found:
                    title_cell = cells[1] 
                    if not isinstance(title_cell, Tag): continue
                    
                    found_text_element = title_cell.find(string=re.compile(r'"([^"]+)"'))
                    if found_text_element:
                        cleaned_title = str(found_text_element).strip().strip('"')
                        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [SUCCESS] Found title via Fallback Strategy: '{cleaned_title}'")
                        return cleaned_title
            except (ValueError, IndexError):
                continue
                
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WIKI] Fallback Strategy failed.")
    return None

async def fetch_episode_title_from_wikipedia(show_title: str, season: int, episode: int) -> Tuple[Optional[str], Optional[str]]:
    """
    (Coordinator - MODIFIED)
    Fetches an episode title from Wikipedia.
    Returns a tuple: (episode_title, corrected_show_title).
    'corrected_show_title' will be the new name if a redirect occurred on fallback,
    otherwise it will be None.
    """
    html_to_scrape = None
    corrected_show_title: Optional[str] = None
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # --- Step 1: Find the correct Wikipedia page ---
    try:
        direct_search_query = f"List of {show_title} episodes"
        print(f"[{ts}] [INFO] Attempting to find dedicated episode page: '{direct_search_query}'")
        page = await asyncio.to_thread(
            wikipedia.page, direct_search_query, auto_suggest=False, redirect=True
        )
        html_to_scrape = await asyncio.to_thread(page.html)
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Successfully found dedicated episode page with original title.")
    
    except wikipedia.exceptions.PageError:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] No dedicated page found. Falling back to main show page search for '{show_title}'.")
        try:
            main_page = await asyncio.to_thread(
                wikipedia.page, show_title, auto_suggest=True, redirect=True
            )
            html_to_scrape = await asyncio.to_thread(main_page.html)
            
            # --- KEY CHANGE: Check for and store a corrected title ---
            if main_page.title != show_title:
                corrected_show_title = main_page.title
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Fallback successful. Show title was corrected: '{show_title}' -> '{corrected_show_title}'")
                try:
                    direct_search_query = f"List of {corrected_show_title} episodes"
                    print(f"[{ts}] [INFO] Attempting to find dedicated episode page: '{direct_search_query}'")
                    page = await asyncio.to_thread(
                        wikipedia.page, direct_search_query, auto_suggest=False, redirect=True
                    )
                    html_to_scrape = await asyncio.to_thread(page.html)
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Successfully found dedicated episode page with original title.")
                    
                except wikipedia.exceptions.PageError:
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] No dedicated page found. Falling back to main show page search for '{show_title}'.")
            else:
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Successfully found main show page with original title.")
            # --- END OF KEY CHANGE ---

        except Exception as e:
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] An unexpected error occurred during fallback page search: {e}")
            return None, None
            
    except Exception as e:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] An unexpected error occurred during direct Wikipedia search: {e}")
        return None, None

    if not html_to_scrape:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] All page search attempts failed.")
        return None, None

    # --- Step 2: Orchestrate the parsing strategies ---
    soup = BeautifulSoup(html_to_scrape, 'lxml')
    
    episode_title = await _parse_dedicated_episode_page(soup, season, episode)
    
    if not episode_title:
        episode_title = await _parse_embedded_episode_page(soup, season, episode)

    if not episode_title:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Both parsing strategies failed to find S{season:02d}E{episode:02d}.")

    return episode_title, corrected_show_title

def get_dominant_file_type(files: lt.file_storage) -> str: # type: ignore
    if files.num_files() == 0: return "N/A"
    largest_file_index = max(range(files.num_files()), key=files.file_size)
    largest_filename = files.file_path(largest_file_index)
    _, extension = os.path.splitext(largest_filename)
    return extension[1:].upper() if extension else "N/A"

def format_bytes(size_bytes: int) -> str:
    if size_bytes <= 0: return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024))) if size_bytes > 0 else 0
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

def _blocking_fetch_metadata(ses: lt.session, magnet_link: str) -> Optional[bytes]: #type: ignore
    """
    (PRODUCTION VERSION)
    Uses a long-lived session provided by the main application. It only
    creates and destroys a temporary handle. This function is synchronous and
    is intended to be run in a separate thread.
    """
    try:
        params = lt.parse_magnet_uri(magnet_link) #type: ignore
        params.save_path = tempfile.gettempdir()
        params.upload_mode = True
        handle = ses.add_torrent(params)

        start_time = time.monotonic()
        timeout_seconds = 30

        while time.monotonic() - start_time < timeout_seconds:
            if handle.status().has_metadata:
                ti = handle.torrent_file()
                creator = lt.create_torrent(ti) #type: ignore
                torrent_dict = creator.generate()
                bencoded_metadata = lt.bencode(torrent_dict) #type: ignore
                
                ses.remove_torrent(handle) # Clean up the handle
                return bencoded_metadata
            
            time.sleep(0.5)
    
    except Exception as e:
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts}] [ERROR] An exceptio`n occurred in the metadata worker thread: {e}")

    # This part is reached on timeout or error
    if 'handle' in locals() and handle.is_valid(): #type: ignore
        ses.remove_torrent(handle) #type: ignore
        
    return None

async def _update_fetch_timer(progress_message: Message, timeout: int, cancel_event: asyncio.Event):
    """(Helper) Updates a message with a simple elapsed time counter."""
    start_time = time.monotonic()
    while not cancel_event.is_set():
        elapsed = int(time.monotonic() - start_time)
        if elapsed > timeout:
            break
            
        # --- THE FIX: Changed from rf"..." to f"..." to correctly process \n ---
        message_text = (
            f"⬇️ *Fetching Metadata\\.\\.\\.*\n"
            f"`Magnet Link`\n\n"
            f"*Please wait, this can be slow\\.*\n"
            f"*The bot is NOT frozen\\.*\n\n"
            f"Elapsed Time: `{elapsed}s`"
        )
        # --- End of fix ---
        try:
            await progress_message.edit_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e}")
            
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass # This is expected.

async def fetch_metadata_from_magnet(magnet_link: str, progress_message: Message, context: ContextTypes.DEFAULT_TYPE) -> Optional[lt.torrent_info]: #type: ignore
    """
    (Coordinator) Fetches metadata by running the blocking libtorrent code in a
    separate thread, while running a responsive UI timer in the main thread.
    """
    cancel_timer = asyncio.Event()
    timer_task = asyncio.create_task(
        _update_fetch_timer(progress_message, 120, cancel_timer)
    )

    ses = context.bot_data["TORRENT_SESSION"]
    bencoded_metadata = await asyncio.to_thread(_blocking_fetch_metadata, ses, magnet_link)
    
    cancel_timer.set()
    await timer_task

    if bencoded_metadata:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Reconstructing torrent_info object from bencoded data.")
        ti = lt.torrent_info(bencoded_metadata) #type: ignore
        return ti
    else:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Metadata fetch failed or timed out.")
        error_message_text = "Timed out fetching metadata from the magnet link. It might be inactive or poorly seeded."
        message_text = f"❌ *Error:* {escape_markdown(error_message_text)}"
        try:
            await progress_message.edit_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e}")
        return None
    
def parse_resolution_from_name(name: str) -> str:
    """Parses a torrent name to find video resolution."""
    name_lower = name.lower()
    # Check for 4K variations
    if any(res in name_lower for res in ['2160p', '4k', 'uhd']):
        return "4K"
    # Check for 1080p
    if '1080p' in name_lower:
        return "1080p"
    # Check for 720p
    if '720p' in name_lower:
        return "720p"
    # Check for standard definition
    if any(res in name_lower for res in ['480p', 'sd', 'dvdrip']):
        return "SD"
    return "N/A"

async def fetch_and_parse_magnet_details(
    magnet_links: List[str],
    context: ContextTypes.DEFAULT_TYPE,
    progress_message: Message
) -> List[Dict[str, Any]]:
    """
    Fetches metadata for a list of magnet links in parallel, parses their
    details, and returns a list of dictionaries, one for each valid link.
    """
    ses = context.bot_data["TORRENT_SESSION"]
    
    async def fetch_one(magnet_link: str, index: int):
        """Worker to fetch metadata for a single magnet link."""
        bencoded_metadata = await asyncio.to_thread(_blocking_fetch_metadata, ses, magnet_link)
        if bencoded_metadata:
            ti = lt.torrent_info(bencoded_metadata) #type: ignore
            return {
                "index": index,
                "ti": ti,
                "magnet_link": magnet_link,
                "bencoded_metadata": bencoded_metadata
            }
        return None

    message_text = f"Found {len(magnet_links)} links. Fetching details... this may take a moment."
    try:
        await progress_message.edit_text(message_text)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e}")

    tasks = [fetch_one(link, i) for i, link in enumerate(magnet_links)]
    results = await asyncio.gather(*tasks)

    parsed_choices = []
    for result in filter(None, results):
        ti = result['ti']
        parsed_choices.append({
            "index": result['index'],
            "resolution": parse_resolution_from_name(ti.name()),
            "size": format_bytes(ti.total_size()),
            "file_type": get_dominant_file_type(ti.files()),
            "name": ti.name(),
            "magnet_link": result['magnet_link'],
            "bencoded_metadata": result['bencoded_metadata']
        })
            
    parsed_choices.sort(key=lambda x: x['index'])
    return parsed_choices

def validate_torrent_files(ti: lt.torrent_info) -> Optional[str]: # type: ignore
    """Checks if the torrent's files are of an allowed type."""
    files = ti.files()
    if files.num_files() == 0:
        return "the torrent contains no files."
        
    large_files_exist = False
    for i in range(files.num_files()):
        file_path = files.file_path(i)
        file_size = files.file_size(i)
        
        if file_size > 10 * 1024 * 1024:
            large_files_exist = True
            _, ext = os.path.splitext(file_path)
            if ext.lower() not in ALLOWED_EXTENSIONS:
                return f"contains an unsupported file type ('{ext}'). I can only download .mkv and .mp4 files."
    
    if not large_files_exist:
        largest_file_idx = max(range(files.num_files()), key=files.file_size)
        file_path = files.file_path(largest_file_idx)
        _, ext = os.path.splitext(file_path)
        if ext.lower() not in ALLOWED_EXTENSIONS:
             return f"contains an unsupported file type ('{ext}'). I can only download .mkv and .mp4 files."

    return None

def _score_torrent_result(name: str, uploader: Optional[str], preferences: dict) -> int:
    """
    (REVISED) Calculates a score for any torrent result based on configured preferences.
    Now includes title normalization.
    """
    score = 0
    # --- THE FIX: Normalize the name by replacing dots with spaces. ---
    name_lower = name.lower().replace('.', ' ')
    
    # Score based on resolution
    res_prefs = preferences.get('resolutions', {})
    for res, points in res_prefs.items():
        if res.lower() in name_lower:
            score += points
            break # Assume only one resolution match is needed

    # Score based on codec
    codec_prefs = preferences.get('codecs', {})
    for codec, points in codec_prefs.items():
        if codec.lower() in name_lower:
            score += points
            break # Assume only one codec match is needed

    # Score based on uploader (if provided)
    if uploader:
        uploader_prefs = preferences.get('uploaders', {})
        for up, points in uploader_prefs.items():
            if up.lower() == uploader.lower():
                score += points
                break

    return score

async def is_user_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Checks if the user sending the update is in the allowed list.
    Returns True if authorized, False otherwise.
    """
    allowed_user_ids = context.bot_data.get('ALLOWED_USER_IDS', [])
    
    if not allowed_user_ids:
        return True

    user = update.effective_user
    if not user or user.id not in allowed_user_ids:
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if user:
            print(f"[{ts}] [ACCESS DENIED] User {user.id} ({user.username}) attempted to use the bot.")
        else:
            print(f"[{ts}] [ACCESS DENIED] An update with no user was received.")
        return False
    
    return True

async def validate_and_enrich_torrent(
    ti: lt.torrent_info, # type: ignore
    progress_message: Message
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Validates a torrent_info object and enriches its metadata.
    """
    if ti.total_size() > MAX_TORRENT_SIZE_BYTES:
        error_msg = f"This torrent is *{format_bytes(ti.total_size())}*, which is larger than the *{MAX_TORRENT_SIZE_GB} GB* limit."
        message_text = f"❌ *Size Limit Exceeded*\n\n{error_msg}"
        try:
            await progress_message.edit_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e}")
        return "Size limit exceeded", None

    validation_error = validate_torrent_files(ti)
    if validation_error:
        error_msg = f"This torrent {validation_error}"
        message_text = f"❌ *Unsupported File Type*\n\n{error_msg}"
        try:
            await progress_message.edit_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e}")
        return f"Unsupported file type", None

    parsed_info = parse_torrent_name(ti.name())

    if parsed_info['type'] == 'tv':
        wiki_search_msg = escape_markdown("TV show detected. Searching Wikipedia for episode title...")
        message_text = f"📺 {wiki_search_msg}"
        try:
            await progress_message.edit_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e}")

        episode_title, corrected_show_title = await fetch_episode_title_from_wikipedia(
            show_title=parsed_info['title'],
            season=parsed_info['season'],
            episode=parsed_info['episode']
        )
        parsed_info['episode_title'] = episode_title

        if corrected_show_title:
            parsed_info['title'] = corrected_show_title

    return None, parsed_info

async def process_user_input(
    text: str,
    context: ContextTypes.DEFAULT_TYPE,
    progress_message: Message
    ) -> Optional[lt.torrent_info]: # type: ignore
    """
    Analyzes user input text to acquire a torrent_info object.
    """
    if context.user_data is None:
        context.user_data = {}
        ti: Optional[lt.torrent_info] = None # type: ignore

    if text.startswith('magnet:?xt=urn:btih:'):
        context.user_data['pending_magnet_link'] = text
        return await fetch_metadata_from_magnet(text, progress_message, context)

    elif text.startswith(('http://', 'https://')) and text.endswith('.torrent'):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(text, follow_redirects=True, timeout=30)
                response.raise_for_status()
            torrent_content = response.content
            ti = lt.torrent_info(torrent_content) # type: ignore

            info_hash = str(ti.info_hashes().v1) # type: ignore
            torrents_dir = ".torrents"
            os.makedirs(torrents_dir, exist_ok=True)
            source_value = os.path.join(torrents_dir, f"{info_hash}.torrent")
            with open(source_value, "wb") as f:
                f.write(torrent_content)

            context.user_data['torrent_file_path'] = source_value
            return ti

        except httpx.RequestError as e:
            error_msg = f"Failed to download .torrent file from URL: {e}"
            message_text = f"❌ *Error:* {escape_markdown(error_msg)}"
            try:
                await progress_message.edit_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
            except BadRequest as e_inner:
                if "Message is not modified" not in str(e_inner):
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e_inner}")
            return None
        except RuntimeError:
            message_text = r"❌ *Error:* The provided file is not a valid torrent\."
            try:
                await progress_message.edit_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
            except BadRequest as e_inner:
                if "Message is not modified" not in str(e_inner):
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e_inner}")
            return None

    elif text.startswith(('http://', 'https://')):
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts}] [PROCESS] URL detected. Starting web scrape for: {text}")
        message_text = f"🌐 Found a web page\\. Scraping for magnet link\\.\\.\\."
        try:
            await progress_message.edit_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e}")

        extracted_magnet_links = await find_magnet_link_on_page(text)

        if not extracted_magnet_links:
            error_msg = "The provided URL does not contain any magnet links, or the page could not be accessed."
            message_text = f"❌ *Error:* {escape_markdown(error_msg)}"
            try:
                await progress_message.edit_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
            except BadRequest as e_inner:
                if "Message is not modified" not in str(e_inner):
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e_inner}")
            return None

        if len(extracted_magnet_links) == 1:
            magnet_link = extracted_magnet_links[0]
            context.user_data['pending_magnet_link'] = magnet_link
            return await fetch_metadata_from_magnet(magnet_link, progress_message, context)

        if len(extracted_magnet_links) > 1:
            parsed_choices = await fetch_and_parse_magnet_details(extracted_magnet_links, context, progress_message)

            if not parsed_choices:
                error_msg = "Could not fetch details for any of the found magnet links. They may be inactive."
                message_text = f"❌ *Error:* {escape_markdown(error_msg)}"
                try:
                    await progress_message.edit_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
                except BadRequest as e_inner:
                    if "Message is not modified" not in str(e_inner):
                        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e_inner}")
                return None

            context.user_data['temp_magnet_choices_details'] = parsed_choices
            
            first_choice_name = parsed_choices[0]['name']
            parsed_title_info = parse_torrent_name(first_choice_name)
            
            common_title = "Unknown Title"
            if parsed_title_info.get('type') == 'movie':
                common_title = f"{parsed_title_info.get('title', '')} ({parsed_title_info.get('year', '')})".strip()
            else:
                common_title = parsed_title_info.get('title', first_choice_name)

            header_text = f"*{escape_markdown(common_title)}*\n\n"
            subtitle_text = rf"Found {len(parsed_choices)} valid torrents\. Please select one:"
            final_text = header_text + subtitle_text

            keyboard = []
            for choice in parsed_choices:
                button_label = f"{choice['resolution']} | {choice['file_type']} | {choice['size']}"
                keyboard.append([InlineKeyboardButton(button_label, callback_data=f"select_magnet_{choice['index']}")])
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                await progress_message.edit_text(final_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e}")
            return None
    else:
        error_message_text = "This does not look like a valid .torrent URL, magnet link, or a web page containing a magnet link."
        message_text = f"❌ *Error:* {escape_markdown(error_message_text)}"
        try:
            await progress_message.edit_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e}")
        return None

async def send_confirmation_prompt(
    progress_message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    ti: lt.torrent_info, # type: ignore
    parsed_info: Dict[str, Any]
) -> None:
    """
    Formats and sends the final confirmation message to the user with buttons.
    """
    if context.user_data is None:
        context.user_data = {}

    display_name = ""
    if parsed_info['type'] == 'movie':
        display_name = f"{parsed_info['title']} ({parsed_info['year']})"
    elif parsed_info['type'] == 'tv':
        base_name = f"{parsed_info['title']} - S{parsed_info['season']:02d}E{parsed_info['episode']:02d}"
        display_name = f"{base_name} - {parsed_info.get('episode_title')}" if parsed_info.get('episode_title') else base_name
    else:
        display_name = parsed_info['title']

    resolution = parse_resolution_from_name(ti.name())
    file_type_str = get_dominant_file_type(ti.files())
    total_size_str = format_bytes(ti.total_size())
    details_line = f"{resolution} | {file_type_str} | {total_size_str}"

    # --- THE FIX: Using a standard multi-line f-string for proper newlines ---
    message_text = (
        f"✅ *Validation Passed*\n\n"
        f"*Name:* {escape_markdown(display_name)}\n"
        f"*Details:* `{escape_markdown(details_line)}`\n\n"
        f"Do you want to start this download?"
    )
    # --- End of fix ---

    keyboard = [[
        InlineKeyboardButton("✅ Confirm Download", callback_data="confirm_download"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation"),
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    source_type: str
    source_value: str
    if 'pending_magnet_link' in context.user_data:
        source_type = 'magnet'
        source_value = str(context.user_data.pop('pending_magnet_link'))
    elif 'torrent_file_path' in context.user_data:
        source_type = 'file'
        source_value = str(context.user_data['torrent_file_path'])
    else:
        source_type = 'magnet'
        source_value = f"magnet:?xt=urn:btih:{ti.info_hashes().v1}"

    context.user_data['pending_torrent'] = {
        'type': source_type,
        'value': source_value,
        'clean_name': display_name,
        'parsed_info': parsed_info,
        'original_message_id': progress_message.message_id
    }

    try:
        await progress_message.edit_text(
            text=message_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e}")

# --- PERSISTENCE FUNCTIONS ---

def save_state(file_path: str, active_downloads: Dict, download_queues: Dict):
    """Saves the state of active and queued downloads to a JSON file."""
    serializable_active = {}
    for chat_id, download_data in active_downloads.items():
        data_copy = download_data.copy()
        data_copy.pop('task', None)
        data_copy.pop('lock', None)
        serializable_active[chat_id] = data_copy

    data_to_save = {
        'active_downloads': serializable_active,
        'download_queues': download_queues
    }

    try:
        with open(file_path, 'w') as f:
            json.dump(data_to_save, f, indent=4)
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        queued_count = sum(len(q) for q in download_queues.values())
        print(f"[{ts}] [INFO] Saved state: {len(serializable_active)} active, {queued_count} queued.")
    except Exception as e:
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts}] [ERROR] Could not save persistence file: {e}")

def load_state(file_path: str) -> Tuple[Dict, Dict]:
    """Loads the state of active and queued downloads from a JSON file."""
    if not os.path.exists(file_path):
        return {}, {}
    try:
        with open(file_path, 'r') as f:
            data = json.load(f)
            active = data.get('active_downloads', {})
            queued = data.get('download_queues', {})
            ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            queued_count = sum(len(q) for q in queued.values())
            print(f"[{ts}] [INFO] Loaded state: {len(active)} active, {queued_count} queued.")
            return active, queued
    except (json.JSONDecodeError, IOError) as e:
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts}] [ERROR] Could not read or parse persistence file '{file_path}': {e}. Starting fresh.")
        return {}, {}
    
async def post_init(application: Application):
    """Resumes any active downloads after the bot has been initialized."""
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] --- Loading persisted state and resuming downloads ---")
    persistence_file = application.bot_data['persistence_file']
    
    active_downloads, download_queues = load_state(persistence_file)
    
    application.bot_data['active_downloads'] = active_downloads
    application.bot_data['download_queues'] = download_queues
    
    if active_downloads:
        for chat_id_str, download_data in active_downloads.items():
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Resuming download for chat_id {chat_id_str}...")
            task = asyncio.create_task(download_task_wrapper(download_data, application))
            download_data['task'] = task
    
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] --- Resume process finished ---")

async def post_shutdown(application: Application):
    """Gracefully signals tasks to stop and preserves the persistence file."""
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] --- Shutting down: Signalling active tasks to stop ---")
    
    application.bot_data['is_shutting_down'] = True
    
    active_downloads = application.bot_data.get('active_downloads', {})
    
    tasks_to_cancel = [
        download_data['task'] 
        for download_data in active_downloads.values() 
        if 'task' in download_data and not download_data['task'].done()
    ]
    
    if not tasks_to_cancel:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] No active tasks to stop.")
        return

    for task in tasks_to_cancel:
        task.cancel()
    
    await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] --- All active tasks stopped. Shutdown complete. ---")

# --- BOT HANDLER FUNCTIONS ---

from telegram import Update
from telegram.ext import CallbackContext

async def links_command(update: Update, context: CallbackContext) -> None:
    """Sends a message with instructions and torrent site links when the /links command is issued."""
    if not await is_user_authorized(update, context):
        return
    if update.message is None:
        return

    try:
        await update.message.delete()
    except BadRequest:
        # This can happen if the bot doesn't have delete permissions
        # or if the message is too old. It's safe to ignore.
        pass

    chat = update.effective_chat
    if not chat:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] links_command was triggered but could not find an effective_chat.")
        return

    message_text = """
I can scrape webpages for magnet and torrent links, send me a URL!

For Movies:
https://yts.mx/
https://1337x.to/
https://thepiratebay.org/

For TV Shows:
https://eztvx.to/
https://1337x.to/
"""
    try:
        # Now we use the guarded 'chat' variable
        await context.bot.send_message(chat_id=chat.id, text=message_text)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not send links message: {e}")

async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(NEW) Starts the conversation to delete media from the library."""
    if not await is_user_authorized(update, context):
        return
    if not update.message: return
    # This guard prevents user_data from ever being None.
    if context.user_data is None: context.user_data = {}

    try:
        await update.message.delete()
    except BadRequest:
        pass

    # --- THE FIX: Set the workflow state immediately ---
    context.user_data['active_workflow'] = 'delete'
    # --- End of fix ---

    keyboard = [
        [
            InlineKeyboardButton("🎬 Movie", callback_data="delete_start_movie"),
            InlineKeyboardButton("📺 TV Show", callback_data="delete_start_tv"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    message_text = "What type of media do you want to delete?"
    
    try:
        await update.message.reply_text(text=message_text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not send delete prompt: {e}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(REVISED) Provides a formatted list of available commands by calling the helper."""
    if not await is_user_authorized(update, context):
        return
    if not update.message: return

    try:
        await update.message.delete()
    except BadRequest:
        pass

    chat = update.effective_chat
    if not chat:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] help_command was triggered but could not find an effective_chat.")
        return
    
    # Use the new helper function to get the message text
    message_text = get_help_message_text()
    
    try:
        await context.bot.send_message(
            chat_id=chat.id,
            text=message_text,
            parse_mode=ParseMode.MARKDOWN_V2
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not send help message: {e}")

async def plex_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Checks the connection to the Plex Media Server."""
    if not await is_user_authorized(update, context):
        return
    if not update.message: return

    try:
        await update.message.delete()
    except BadRequest:
        pass

    chat = update.effective_chat
    if not chat:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] plex_status_command was triggered but could not find an effective_chat.")
        return

    status_message = None
    try:
        # Use the guarded 'chat' variable
        status_message = await context.bot.send_message(chat_id=chat.id, text="Plex Status: 🟡 Checking connection...")
    except BadRequest as e:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not send initial plex status message: {e}")
        return

    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    plex_config = context.bot_data.get("PLEX_CONFIG", {})

    if not plex_config:
        message_text = "Plex Status: ⚪️ Not configured. Please add your Plex details to the `config.ini` file."
        try:
            await status_message.edit_text(message_text)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e}")
        return

    try:
        print(f"[{ts}] [PLEX STATUS] Attempting to connect to Plex server...")
        plex = await asyncio.to_thread(PlexServer, plex_config['url'], plex_config['token'])
        # These values are retrieved but not used in the message, which is fine.
        server_version = plex.version
        server_platform = plex.platform
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PLEX STATUS] Success! Connected.")
        
        message_text = (
            f"Plex Status: ✅ *Connected*"
        )
        try:
            await status_message.edit_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e}")

    except Unauthorized:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PLEX STATUS] ERROR: Unauthorized.")
        message_text = (
            f"Plex Status: ❌ *Authentication Failed*\n\n"
            f"The Plex API token is incorrect\\. Please check your `config\\.ini` file\\."
        )
        try:
            await status_message.edit_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e}")

    except Exception as e:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PLEX STATUS] ERROR: {e}")
        message_text = (
            f"Plex Status: ❌ *Connection Failed*\n"
            f"Could not connect to the Plex server at `{escape_markdown(plex_config['url'])}`\\. "
            f"Please ensure the server is running and accessible\\."
        )
        try:
            await status_message.edit_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e_inner:
            if "Message is not modified" not in str(e_inner):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e_inner}")

async def plex_restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(NEW - Linux Simplified) Restarts the Plex server via a direct subprocess call."""
    if not await is_user_authorized(update, context):
        return
    if not update.message: return

    try:
        await update.message.delete()
    except BadRequest:
        pass

    chat = update.effective_chat
    if not chat:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] plex_restart_command was triggered but could not find an effective_chat.")
        return

    if platform.system() != "Linux":
        try:
            await context.bot.send_message(chat_id=chat.id, text="This command is configured to run on Linux only.")
        except BadRequest as e:
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not send plex restart message: {e}")
        return

    status_message = None
    try:
        status_message = await context.bot.send_message(chat_id=chat.id, text="Plex Restart: 🟡 Sending restart command to the server...")
    except BadRequest as e:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not send initial plex restart message: {e}")
        return
        
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    script_path = os.path.abspath("restart_plex.sh")

    if not os.path.exists(script_path):
        print(f"[{ts}] [PLEX RESTART] ERROR: Wrapper script not found at {script_path}")
        # --- THE FIX ---
        message_text = "❌ *Error:* The `restart_plex\\.sh` script was not found in the bot's directory\\."
        # --- End of fix ---
        try:
            await status_message.edit_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e}")
        return
    
    command = ["/usr/bin/sudo", script_path]

    try:
        print(f"[{ts}] [PLEX RESTART] Executing wrapper script: {' '.join(command)}")
        await asyncio.to_thread(subprocess.run, command, check=True, capture_output=True, text=True)
        message_text = "✅ *Plex Restart Successful*"
        try:
            await status_message.edit_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e}")
        print(f"[{ts}] [PLEX RESTART] Success!")

    except subprocess.CalledProcessError as e:
        error_output = e.stderr or e.stdout
        message_text = f"❌ *Script Failed*\n\nThis almost always means the `sudoers` rule for `restart_plex\\.sh` is incorrect or missing\\.\n\n*Details:*\n`{escape_markdown(error_output)}`"
        print(f"[{ts}] [PLEX RESTART] ERROR executing script: {error_output}")
        try:
            await status_message.edit_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e_inner:
            if "Message is not modified" not in str(e_inner):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e_inner}")

    except Exception as e:
        message_text = f"❌ *An Unexpected Error Occurred*\n\n`{escape_markdown(str(e))}`"
        print(f"[{ts}] [PLEX RESTART] ERROR: {str(e)}")
        try:
            await status_message.edit_text(message_text, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest as e_inner:
            if "Message is not modified" not in str(e_inner):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e_inner}")

# async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     """(NEW) Starts the conversation to search for media."""
#     if not await is_user_authorized(update, context):
#         return
#     if not update.message: return
#     # This guard prevents user_data from ever being None.
#     if context.user_data is None: context.user_data = {}

#     ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
#     user = update.effective_user
#     if user:
#         print(f"[{ts}] [SEARCH] User {user.id} ({user.username}) initiated /search command.")
#     else:
#         print(f"[{ts}] [SEARCH] An anonymous user initiated /search command.")
        
#     # --- THE FIX: Set the workflow state immediately ---
#     context.user_data['active_workflow'] = 'search'
#     # --- End of fix ---

#     try:
#         await update.message.delete()
#     except BadRequest:
#         pass

#     keyboard = [
#         [
#             InlineKeyboardButton("🎬 Movie", callback_data="search_start_movie"),
#             InlineKeyboardButton("📺 TV Show", callback_data="search_start_tv"),
#         ],
#         [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
#     ]
#     reply_markup = InlineKeyboardMarkup(keyboard)
#     message_text = "What type of media do you want to search for?"
    
#     await update.message.reply_text(text=message_text, reply_markup=reply_markup)

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(CORRECTED) Starts the consolidated search workflow."""
    if not await is_user_authorized(update, context):
        return
    if not update.message: return
    if context.user_data is None: context.user_data = {}

    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    user = update.effective_user
    
    if user:
        log_msg = f"[{ts}] [SEARCH] User {user.id} ({user.username}) initiated /search command."
    else:
        log_msg = f"[{ts}] [SEARCH] An anonymous user initiated /search command."
    print(log_msg)
    
    context.user_data['active_workflow'] = 'search'

    try:
        await update.message.delete()
    except BadRequest:
        pass

    keyboard = [
        [
            InlineKeyboardButton("🎬 Movie", callback_data="search_start_movie"),
            InlineKeyboardButton("📺 TV Show", callback_data="search_start_tv"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    # --- THE FIX: Escaped the '?' at the end of the string ---
    message_text = "What type of media do you want to search for\?"
    
    await update.message.reply_text(text=message_text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN_V2)

async def find_magnet_link_on_page(url: str) -> List[str]:
    """
    Fetches a web page and attempts to find all unique magnet links (href starting with 'magnet:').
    Returns a list of unique found magnet links, or an empty list if none are found.
    """
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    # --- MODIFIED: Use a set to store unique magnet links ---
    unique_magnet_links: Set[str] = set() 

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            print(f"[{ts}] [WEBSCRAPE] Fetching URL: {url}")
            response = await client.get(url, follow_redirects=True)
            response.raise_for_status() # Raise an exception for HTTP errors (4xx or 5xx)

        soup = BeautifulSoup(response.text, 'lxml')

        # Look for all <a> tags with an href starting with 'magnet:'
        magnet_link_tags = soup.find_all('a', href=re.compile(r'^magnet:'))

        if magnet_link_tags:
            for tag in magnet_link_tags:
                if isinstance(tag, Tag):
                    magnet_link = tag.get('href')
                    if isinstance(magnet_link, str):
                        # --- MODIFIED: Add to set instead of list ---
                        unique_magnet_links.add(magnet_link) 
            
            if unique_magnet_links:
                print(f"[{ts}] [WEBSCRAPE] Found {len(unique_magnet_links)} unique magnet link(s) on page: {url}")
                # Log the first one found (arbitrary order from set) for brevity
                first_link = next(iter(unique_magnet_links)) # Get first element from set
                print(f"[{ts}] [WEBSCRAPE] First unique magnet link: {first_link[:100]}...")
            else:
                print(f"[{ts}] [WEBSCRAPE] No valid magnet links found after parsing tags on page: {url}")
        else:
            print(f"[{ts}] [WEBSCRITICAL] No <a> tags with magnet links found on page: {url}")

    except httpx.RequestError as e:
        print(f"[{ts}] [WEBSCRAPE ERROR] HTTP Request failed for {url}: {e}")
    except Exception as e:
        print(f"[{ts}] [WEBSCRAPE ERROR] An unexpected error occurred during scraping {url}: {e}")
    
    # --- Convert set back to a list before returning ---
    return list(unique_magnet_links)

async def process_queue_for_user(chat_id: int, application: Application):
    """
    Checks and processes the download queue for a specific user.

    This function is the single authority for starting a download from the queue.
    It is safe to call multiple times, as it will exit immediately if a download
    is already active for the user.
    
    Args:
        chat_id: The integer chat ID of the user.
        application: The main Application object.
    """
    chat_id_str = str(chat_id)
    active_downloads = application.bot_data.get('active_downloads', {})
    download_queues = application.bot_data.get('download_queues', {})
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # --- DEBUG: Guard clause to prevent duplicate downloads ---
    if chat_id_str in active_downloads:
        print(f"[{ts}] [QUEUE_PROCESSOR] Invoked for {chat_id_str}, but a download is already active. No action taken.")
        return

    # --- DEBUG: Check if there's anything to process ---
    if chat_id_str in download_queues and download_queues[chat_id_str]:
        print(f"[{ts}] [QUEUE_PROCESSOR] No active download for {chat_id_str}. Starting next item from queue.")
        
        # Pop the next item from the front of the queue
        next_download_data = download_queues[chat_id_str].pop(0)

        # If the queue is now empty, remove the user's entry completely
        if not download_queues[chat_id_str]:
            del download_queues[chat_id_str]
        
        # This helper function now correctly starts the task
        await start_download_task(next_download_data, application)
    else:
        print(f"[{ts}] [QUEUE_PROCESSOR] Invoked for {chat_id_str}, but their queue is empty. No action taken.")

# file: telegram_bot.py

async def handle_delete_workflow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    (CORRECTED) Manages the multi-step conversation for deleting media, now
    handling multiple search results by prompting the user for selection.
    """
    if not update.message or not update.message.text: return
    # This guard prevents user_data from ever being None.
    if context.user_data is None: context.user_data = {}

    chat_id = update.message.chat_id
    text = update.message.text.strip()
    next_action = context.user_data.get('next_action', '')
    prompt_message_id = context.user_data.pop('prompt_message_id', None)

    try:
        await update.message.delete()
        if prompt_message_id:
            await context.bot.delete_message(chat_id=chat_id, message_id=prompt_message_id)
    except (BadRequest, TimedOut) as e:
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts}] [WARN] Non-critical error while deleting message: {e}")
        pass

    status_message: Message

    # --- THE FIX: Pass `context` as an explicit argument ---
    async def present_results(
        results: Union[str, List[str], None], 
        s_message: Message, 
        media_name: str, 
        ctx: ContextTypes.DEFAULT_TYPE
    ):
        if ctx.user_data is None: ctx.user_data = {} # Extra safety guard

        if isinstance(results, str): # Single result
            ctx.user_data['path_to_delete'] = results
            base_name = os.path.basename(results)
            keyboard = [[InlineKeyboardButton("✅ Yes, Delete It", callback_data="confirm_delete"), InlineKeyboardButton("❌ No, Cancel", callback_data="cancel_operation")]]
            message_text = (
                f"Found:\n`{escape_markdown(base_name)}`\n\n"
                f"*Path:*\n`{escape_markdown(results)}`\n\n"
                f"Are you sure you want to permanently delete this item?"
            )
            await s_message.edit_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
        elif isinstance(results, list): # Multiple results
            ctx.user_data['selection_choices'] = results
            keyboard = []
            for i, path in enumerate(results):
                button_label = os.path.basename(path)
                keyboard.append([InlineKeyboardButton(button_label, callback_data=f"delete_select_{i}")])
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")])
            message_text = "Multiple matches found, which one?"
            await s_message.edit_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard))
        else: # No results
            await s_message.edit_text(f"❌ No {media_name} found matching: `{escape_markdown(text)}`", parse_mode=ParseMode.MARKDOWN_V2)
    # --- End of fix ---

    if next_action == 'delete_movie_collection_search':
        status_message = await context.bot.send_message(chat_id=chat_id, text=rf"🔎 Searching for movie collection: `{escape_markdown(text)}`\.\.\.", parse_mode=ParseMode.MARKDOWN_V2)
        save_paths = context.bot_data.get("SAVE_PATHS", {})
        found_results = await find_media_by_name('movie', text, save_paths, search_target='directory')
        context.user_data.pop('next_action', None)
        # Pass context to the helper function
        await present_results(found_results, status_message, "movie collection", context)

    elif next_action == 'delete_movie_single_search':
        status_message = await context.bot.send_message(chat_id=chat_id, text=rf"🔎 Searching for single movie: `{escape_markdown(text)}`\.\.\.", parse_mode=ParseMode.MARKDOWN_V2)
        save_paths = context.bot_data.get("SAVE_PATHS", {})
        found_results = await find_media_by_name('movie', text, save_paths, search_target='file')
        context.user_data.pop('next_action', None)
        # Pass context to the helper function
        await present_results(found_results, status_message, "single movie", context)

    # (The rest of the function remains the same as before)
    elif next_action == 'delete_tv_show_search':
        status_message = await context.bot.send_message(chat_id=chat_id, text=rf"🔎 Searching for TV show: `{escape_markdown(text)}`\.\.\.", parse_mode=ParseMode.MARKDOWN_V2)
        save_paths = context.bot_data.get("SAVE_PATHS", {})
        found_path = await find_media_by_name('tv_show', text, save_paths) # TV show logic remains singular
        context.user_data.pop('next_action', None)

        if isinstance(found_path, str):
            context.user_data['show_path_to_delete'] = found_path
            base_name = os.path.basename(found_path)
            keyboard = [
                [InlineKeyboardButton("🗑️ All", callback_data="delete_tv_all")],
                [InlineKeyboardButton("💿 Season", callback_data="delete_tv_season")],
                [InlineKeyboardButton("▶️ Episode", callback_data="delete_tv_episode")],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
            ]
            await status_message.edit_text(f"Found show: `{escape_markdown(base_name)}`\.\n\nWhat would you like to delete\?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
        else:
            # This part handles None or a list (which isn't expected for TV shows yet)
            await status_message.edit_text(f"❌ No single TV show directory found matching: `{escape_markdown(text)}`", parse_mode=ParseMode.MARKDOWN_V2)

    elif next_action == 'delete_tv_season_search':
        status_message = await context.bot.send_message(chat_id=chat_id, text=rf"🔎 Searching for season `{escape_markdown(text)}`\.\.\.", parse_mode=ParseMode.MARKDOWN_V2)
        show_path = context.user_data.get('show_path_to_delete')
        context.user_data.pop('next_action', None)
        
        if show_path and text.isdigit():
            found_path = await find_season_directory(show_path, int(text))
            if found_path:
                context.user_data['path_to_delete'] = found_path
                base_name = os.path.basename(found_path)
                keyboard = [[InlineKeyboardButton("✅ Yes, Delete Season", callback_data="confirm_delete"), InlineKeyboardButton("❌ No, Cancel", callback_data="cancel_operation")]]
                message_text = (
                    f"Found Season:\n`{escape_markdown(base_name)}`\n\n"
                    f"*Path:*\n`{escape_markdown(found_path)}`\n\n"
                    f"Are you sure you want to delete this entire season\\?"
                )
                await status_message.edit_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await status_message.edit_text(f"❌ Could not find Season {escape_markdown(text)} in that show\.", parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await status_message.edit_text(r"❌ Invalid input or show context lost\. Please start over\.", parse_mode=ParseMode.MARKDOWN_V2)

    elif next_action == 'delete_tv_episode_season_prompt':
        show_path = context.user_data.get('show_path_to_delete')
        if show_path and text.isdigit():
            context.user_data['season_to_delete_num'] = int(text)
            context.user_data['next_action'] = 'delete_tv_episode_episode_prompt'
            new_prompt = await context.bot.send_message(chat_id=chat_id, text=rf"📺 Season {escape_markdown(text)} selected\. Now, please send the episode number to delete\.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]), parse_mode=ParseMode.MARKDOWN_V2)
            context.user_data['prompt_message_id'] = new_prompt.message_id
        else:
            await context.bot.send_message(chat_id=chat_id, text=r"❌ Invalid season number\. Please start over\.", parse_mode=ParseMode.MARKDOWN_V2)
            context.user_data.pop('next_action', None)

    elif next_action == 'delete_tv_episode_episode_prompt':
        status_message = await context.bot.send_message(chat_id=chat_id, text=rf"🔎 Searching for episode `{escape_markdown(text)}`\.\.\.", parse_mode=ParseMode.MARKDOWN_V2)
        show_path = context.user_data.get('show_path_to_delete')
        season_num = context.user_data.get('season_to_delete_num')
        context.user_data.pop('next_action', None)

        if show_path and season_num and text.isdigit():
            season_path = await find_season_directory(show_path, season_num)
            if season_path:
                found_path = await find_episode_file(season_path, season_num, int(text))
                if found_path:
                    context.user_data['path_to_delete'] = found_path
                    base_name = os.path.basename(found_path)
                    directory_path = os.path.dirname(found_path)
                    keyboard = [[InlineKeyboardButton("✅ Yes, Delete Episode", callback_data="confirm_delete"), InlineKeyboardButton("❌ No, Cancel", callback_data="cancel_operation")]]
                    message_text = (
                        f"Found Episode:\n`{escape_markdown(base_name)}`\n\n"
                        f"*Path:*\n`{escape_markdown(directory_path)}`\n\n"
                        f"Are you sure you want to delete this file\\?"
                    )
                    await status_message.edit_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
                else:
                    await status_message.edit_text(rf"❌ Could not find Episode {escape_markdown(text)} in that season\.", parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await status_message.edit_text(rf"❌ Could not find Season {season_num} to look for the episode in\.", parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await status_message.edit_text(r"❌ Invalid input or context lost\. Please start over\.", parse_mode=ParseMode.MARKDOWN_V2)

async def handle_delete_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    (FINAL CORRECTION) Handles all button presses for the delete workflow, with
    the proper MarkdownV2 escapes for all special characters.
    """
    query = update.callback_query
    if not query or not query.data: return
    
    message = query.message
    if not isinstance(message, Message): return

    if context.user_data is None: context.user_data = {}

    await query.answer()

    message_text: str = ""
    reply_markup: Optional[InlineKeyboardMarkup] = None
    parse_mode: Optional[str] = None

    if query.data.startswith("delete_select_"):
        choices = context.user_data.pop('selection_choices', [])
        try:
            index = int(query.data.split('_')[2])
            if 0 <= index < len(choices):
                path_to_delete = choices[index]
                context.user_data['path_to_delete'] = path_to_delete
                base_name = os.path.basename(path_to_delete)
                message_text = (
                    f"You selected:\n`{escape_markdown(base_name)}`\n\n"
                    f"Are you sure you want to permanently delete this item?"
                )
                reply_markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Yes, Delete It", callback_data="confirm_delete"),
                    InlineKeyboardButton("❌ No, Cancel", callback_data="cancel_operation")
                ]])
                parse_mode = ParseMode.MARKDOWN_V2
            else:
                message_text = "❌ Error: Invalid selection index. Please start over."
        except (ValueError, IndexError):
            message_text = "❌ Error: Could not process selection. Please start over."

    elif query.data == "delete_start_movie":
        message_text = "Which would you like to delete?"
        keyboard = [
            [
                InlineKeyboardButton("🗂️ Collection", callback_data="delete_movie_collection"),
                InlineKeyboardButton("📄 Single", callback_data="delete_movie_single"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

    elif query.data == "delete_movie_collection":
        context.user_data['next_action'] = 'delete_movie_collection_search'
        context.user_data['prompt_message_id'] = message.message_id
        message_text = "🎬 Please send me the title of the movie collection (folder) to delete."
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]])

    elif query.data == "delete_movie_single":
        context.user_data['next_action'] = 'delete_movie_single_search'
        context.user_data['prompt_message_id'] = message.message_id
        message_text = "🎬 Please send me the title of the single movie (file) to delete."
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]])

    elif query.data == "delete_start_tv":
        context.user_data['next_action'] = 'delete_tv_show_search'
        context.user_data['prompt_message_id'] = message.message_id
        message_text = "📺 Please send me the title of the TV show to delete."
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]])

    elif query.data == "delete_tv_all":
        show_path = context.user_data.get('show_path_to_delete')
        if show_path:
            context.user_data['path_to_delete'] = show_path
            base_name = os.path.basename(show_path)
            message_text = (
                f"Are you sure you want to delete the ENTIRE show `{escape_markdown(base_name)}` and all its contents\\?\n\n"
                f"*Path:*\n`{escape_markdown(show_path)}`"
            )
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Yes, Delete All", callback_data="confirm_delete"), InlineKeyboardButton("❌ No, Cancel", callback_data="cancel_operation")]])
            parse_mode = ParseMode.MARKDOWN_V2
        else:
            message_text = f"❌ Error: Show context lost. Please start over."

    elif query.data == "delete_tv_season":
        context.user_data['next_action'] = 'delete_tv_season_search'
        context.user_data['prompt_message_id'] = message.message_id
        message_text = "💿 Please send me the season number to delete."
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]])

    elif query.data == "delete_tv_episode":
        context.user_data['next_action'] = 'delete_tv_episode_season_prompt'
        context.user_data['prompt_message_id'] = message.message_id
        message_text = "📺 First, please send the season number."
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]])

    elif query.data == "confirm_delete":
        path_to_delete = context.user_data.pop('path_to_delete', None)
        
        if not path_to_delete:
            message_text = f"❌ Error: Path to delete not found. The action may have expired."
        elif not DELETION_ENABLED:
            message_text = f"ℹ️ *Deletion Confirmed*\n\nActual file deletion is disabled by the administrator"
        else:
            try:
                if os.path.exists(path_to_delete):
                    show_path_context = context.user_data.get('show_path_to_delete')
                    base_name = os.path.basename(path_to_delete)
                    display_name = base_name

                    if show_path_context:
                        show_name = os.path.basename(show_path_context)
                        if base_name != show_name:
                            display_name = f"{show_name} - {base_name}"
                    
                    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    if os.path.isfile(path_to_delete):
                        print(f"[{ts}] [DELETE] Deleting file: {path_to_delete}")
                        await asyncio.to_thread(os.remove, path_to_delete)
                        message_text = f"🗑️ *Successfully Deleted File*\n`{escape_markdown(display_name)}`"
                    elif os.path.isdir(path_to_delete):
                        print(f"[{ts}] [DELETE] Deleting directory and all contents: {path_to_delete}")
                        await asyncio.to_thread(shutil.rmtree, path_to_delete)
                        message_text = f"🗑️ *Successfully Deleted Directory*\n`{escape_markdown(display_name)}`"
                    else:
                        message_text = f"❌ *Deletion Failed*\nCould not delete `{escape_markdown(base_name)}` because it is neither a file nor a directory."
                else:
                    message_text = f"❌ *Deletion Failed*\nThe path no longer exists on the server."
            
            except (OSError, PermissionError) as e:
                ts_err = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                print(f"[{ts_err}] [DELETE ERROR] An OS error occurred: {e}")
                message_text = f"❌ *Deletion Failed*\nAn error occurred on the server:\n`{escape_markdown(str(e))}`"
        
        parse_mode = ParseMode.MARKDOWN_V2
        keys_to_clear = ['show_path_to_delete', 'next_action', 'prompt_message_id', 'season_to_delete_num', 'selection_choices']
        for key in keys_to_clear:
            context.user_data.pop(key, None)
    
    elif query.data == "cancel_operation":
        keys_to_clear = ['path_to_delete', 'show_path_to_delete', 'next_action', 'prompt_message_id', 'season_to_delete_num', 'selection_choices']
        for key in keys_to_clear:
            context.user_data.pop(key, None)
        message_text = "❌ Operation cancelled."

    try:
        await query.edit_message_text(text=message_text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit message in delete_button_handler: {e}")

# async def handle_search_workflow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
#     """
#     (REBUILT) Manages the search workflow. After finding the best match,
#     it now passes the movie's dedicated page URL to the main
#     process_user_input function, leveraging the existing download flow.
#     """
#     if not update.message or not update.message.text: return
#     if context.user_data is None: context.user_data = {}

#     chat_id = update.message.chat_id
#     text = update.message.text.strip()
#     next_action = context.user_data.get('next_action', '')
#     prompt_message_id = context.user_data.pop('prompt_message_id', None)

#     try:
#         if prompt_message_id:
#             await context.bot.delete_message(chat_id=chat_id, message_id=prompt_message_id)
#         await update.message.delete()
#     except (BadRequest, TimedOut):
#         pass

#     if next_action in ['search_movie_title', 'search_tv_title']:
#         config_media_type = "movies"
#         context.user_data.pop('next_action', None)
        
#         status_message = await context.bot.send_message(chat_id=chat_id, text=f"🔎 Searching for *{escape_markdown(text)}*\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
        
#         results, site_name = await _search_for_media(query=text, media_type=config_media_type, context=context)

#         if not results:
#             await status_message.edit_text(f"❌ No results found for '`{escape_markdown(text)}`' on *{escape_markdown(site_name)}*\\.", parse_mode=ParseMode.MARKDOWN_V2)
#             return

#         highest_score = results[0]['score']
#         top_results = [res for res in results if res['score'] == highest_score]
#         unique_top_movies = list({f"{res['title']} ({res['year']})": res for res in top_results}.values())

#         if len(unique_top_movies) > 1:
#             context.user_data['search_ambiguous_choices'] = unique_top_movies
#             keyboard = []
#             for i, movie in enumerate(unique_top_movies):
#                 keyboard.append([InlineKeyboardButton(f"{movie['title']} ({movie['year']})", callback_data=f"search_clarify_{i}")])
#             keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")])
#             await status_message.edit_text("Multiple possible matches found\\. Please select the correct one:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)
#         else:
#             # Single best match found, proceed directly
#             best_match = unique_top_movies[0]
#             page_url_to_process = best_match['page_url']
            
#             ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
#             print(f"[{ts}] [SEARCH] Single best match found: '{best_match['title']}'. Passing URL to handler: {page_url_to_process}")
            
#             # --- HANDOFF TO EXISTING WORKFLOW ---
#             await process_user_input(page_url_to_process, context, status_message)

async def _orchestrate_searches(
    query: str,
    media_type: str,
    context: ContextTypes.DEFAULT_TYPE,
    year: Optional[str] = None,
    resolution: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    (REVISED) Runs searches across all sites, applies source-specific limits
    (3 for 1337x, 1 for YTS), and returns a final, sorted list.
    """
    search_config = context.bot_data.get("SEARCH_CONFIG", {})
    if not search_config: return []
    
    config_lookup_key = 'movies' if media_type == 'movie' else 'tv'
    sites_to_search = search_config.get("websites", {}).get(config_lookup_key, [])
    
    if not sites_to_search:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [SEARCH] No sites configured for key: '{config_lookup_key}'")
        return []

    tasks = []
    for site in sites_to_search:
        site_name = site.get("name", "")
        task = _search_for_media(query=query, media_type=config_lookup_key, site_name=site_name, context=context, year=year, resolution=resolution)
        tasks.append(task)
        
    all_results_with_site = await asyncio.gather(*tasks)

    # --- THE FIX: Apply limits per source and then combine ---
    final_results = []
    site_limits = {
        "1337x": 3,
        "YTS.mx": 1
    }

    for result_list, site_name in all_results_with_site:
        limit = site_limits.get(site_name)
        if limit:
            # The result_list is already sorted by score from _search_for_media
            final_results.extend(result_list[:limit])
    
    # Sort the final combined list by score
    final_results.sort(key=lambda x: x.get('score', 0), reverse=True)
    
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] [SEARCH] Aggregated and sorted {len(final_results)} results from {len(sites_to_search)} sites after applying source limits.")
    
    return final_results


async def _prompt_for_resolution(chat_id: int, context: ContextTypes.DEFAULT_TYPE, full_title: str):
    """Asks the user to select a resolution."""
    # --- THE FIX: Ensure user_data is a dictionary before use. ---
    if context.user_data is None:
        context.user_data = {}
        
    # Store the final title for later retrieval
    context.user_data['search_final_title'] = full_title
    # The next action is for the button handler to catch the resolution choice
    context.user_data['next_action'] = 'handle_resolution_choice' 

    keyboard = [[
        InlineKeyboardButton("1080p", callback_data="search_resolution_1080p"),
        InlineKeyboardButton("2160p", callback_data="search_resolution_2160p"),
    ], [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    prompt_message = await context.bot.send_message(
        chat_id=chat_id,
        text=f"Got it: `{escape_markdown(full_title)}`\\. Now, please select your desired resolution:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN_V2
    )
    context.user_data['prompt_message_id'] = prompt_message.message_id


async def _handle_search_text_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (FIXED) Manages the conversational workflow for the unified search command.
    Correctly escapes the hyphen in "4-digit" for MarkdownV2 compatibility.
    """
    if not update.message or not update.message.text: return
    if context.user_data is None: context.user_data = {}

    chat = update.effective_chat
    if not chat: return

    text = update.message.text.strip()
    next_action = context.user_data.get('next_action')

    # Clean up previous messages
    prompt_message_id = context.user_data.pop('prompt_message_id', None)
    try:
        if prompt_message_id:
            await context.bot.delete_message(chat_id=chat.id, message_id=prompt_message_id)
        await update.message.delete()
    except BadRequest:
        pass

    if next_action == 'search_movie_title':
        # Check if the title includes a year
        year_match = re.search(r'\b(19\d{2}|20\d{2})\b', text)
        if year_match:
            # Year found, store title and proceed to resolution prompt
            context.user_data['search_movie_title'] = text
            context.user_data['next_action'] = 'search_movie_resolution'
            
            keyboard = [[
                InlineKeyboardButton("1080p", callback_data="search_resolution_1080p"),
                InlineKeyboardButton("4K", callback_data="search_resolution_4k"),
            ], [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            prompt_message = await context.bot.send_message(
                chat_id=chat.id,
                text=f"Got it: `{escape_markdown(text)}`\\. Now, please select your desired resolution:",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN_V2
            )
            context.user_data['prompt_message_id'] = prompt_message.message_id
        else:
            # No year found, store title and ask for the year
            context.user_data['search_movie_title_no_year'] = text
            context.user_data['next_action'] = 'search_movie_year'
            
            # --- THE FIX: Escape the hyphen in "4-digit" ---
            prompt_text = f"I need a year for `{escape_markdown(text)}`\\. Please send the 4\\-digit year\\."
            # --- End of fix ---

            prompt_message = await context.bot.send_message(
                chat_id=chat.id,
                text=prompt_text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            context.user_data['prompt_message_id'] = prompt_message.message_id

    elif next_action == 'search_movie_year':
        year_text = text
        original_title = context.user_data.pop('search_movie_title_no_year', None)

        if not original_title:
            await context.bot.send_message(chat_id=chat.id, text="❌ Search context was lost. Please start over.")
            context.user_data.pop('next_action', None)
            return

        # --- THE FIX: Escape the hyphen in "4-digit" in the error message ---
        if not (year_text.isdigit() and len(year_text) == 4):
            # Invalid year, ask again
            context.user_data['search_movie_title_no_year'] = original_title
            context.user_data['next_action'] = 'search_movie_year'
            error_text = f"That doesn't look like a valid 4\\-digit year\\. Please try again for `{escape_markdown(original_title)}` or cancel\\."
            # --- End of fix ---
            error_prompt = await context.bot.send_message(
                chat_id=chat.id, text=error_text, parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]])
            )
            context.user_data['prompt_message_id'] = error_prompt.message_id
            return
            
        # Valid year received, combine with title and proceed to resolution prompt
        full_title = f"{original_title} ({year_text})"
        context.user_data['search_movie_title'] = full_title
        context.user_data['next_action'] = 'search_movie_resolution'

        keyboard = [[
            InlineKeyboardButton("1080p", callback_data="search_resolution_1080p"),
            InlineKeyboardButton("4K", callback_data="search_resolution_4k"),
        ], [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        prompt_message = await context.bot.send_message(
            chat_id=chat.id,
            text=f"Got it: `{escape_markdown(full_title)}`\\. Now, please select your desired resolution:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2
        )
        context.user_data['prompt_message_id'] = prompt_message.message_id

async def handle_search_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(CORRECTED & REVISED) Manages the multi-step conversation for searching."""
    if not await is_user_authorized(update, context): return
    if not update.message or not update.message.text: return
    if context.user_data is None: context.user_data = {}

    next_action = context.user_data.get('next_action', '')
    
    # Only handle search and delete workflows here
    if not next_action.startswith(('search_movie_', 'handle_title_for_tv', 'delete_')):
        return

    chat = update.effective_chat
    if not chat: return
    
    query = update.message.text.strip()
    prompt_message_id = context.user_data.pop('prompt_message_id', None)
    
    try:
        if prompt_message_id:
            await context.bot.delete_message(chat_id=chat.id, message_id=prompt_message_id)
        await update.message.delete()
    except BadRequest:
        pass

    # --- DELETE WORKFLOW ---
    if next_action.startswith('delete_'):
        await handle_delete_workflow(update, context)
        return

    # --- MOVIE WORKFLOW ---
    if next_action == 'search_movie_get_title':
        year_match = re.search(r'\b(19\d{2}|20\d{2})\b', query)
        
        if year_match:
            # Year is present, so we can proceed to ask for resolution.
            year = year_match.group(0)
            title = query[:year_match.start()].strip()
            title = re.sub(r'[\s(]+$', '', title).strip()
            full_title = f"{title} ({year})"
            
            context.user_data['search_media_type'] = 'movie' # Store type for the button handler
            await _prompt_for_resolution(chat.id, context, full_title)
        else:
            # No year found, ask the user for it.
            context.user_data['search_query_title'] = query
            context.user_data['next_action'] = 'search_movie_get_year'
            prompt_text = f"Got it\. Now, please send the 4\-digit year for *{escape_markdown(query)}*\."
            new_prompt = await context.bot.send_message(
                chat_id=chat.id,
                text=prompt_text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            context.user_data['prompt_message_id'] = new_prompt.message_id
        return

    elif next_action == 'search_movie_get_year':
        year_text = query
        title = context.user_data.get('search_query_title')
        
        if not title:
            await context.bot.send_message(chat_id=chat.id, text="❌ Search context was lost. Please start over.")
            return

        if not (year_text.isdigit() and len(year_text) == 4):
            context.user_data['next_action'] = 'search_movie_get_year' # Reset to retry
            error_text = f"That doesn't look like a valid 4\-digit year\. Please try again for *{escape_markdown(title)}* or cancel\."
            error_prompt = await context.bot.send_message(
                chat_id=chat.id, text=error_text, parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]])
            )
            context.user_data['prompt_message_id'] = error_prompt.message_id
            return

        full_title = f"{title} ({year_text})"
        context.user_data['search_media_type'] = 'movie' # Store type for the button handler
        await _prompt_for_resolution(chat.id, context, full_title)
        return

    # --- TV SHOW WORKFLOW ---
    elif next_action == 'handle_title_for_tv':
        full_title = query
        context.user_data['search_media_type'] = 'tv' # Store type for the button handler
        await _prompt_for_resolution(chat.id, context, full_title)
        return

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    (REVISED) Handles all button presses, including the new consolidated search flow.
    """
    if not await is_user_authorized(update, context):
        return

    query = update.callback_query
    if not query or not query.data: return
    message = query.message
    if not isinstance(message, Message): return
    if context.user_data is None: context.user_data = {}
    
    if query.data.startswith("search_start_"):
        await query.answer()
        if query.data == 'search_start_movie':
            context.user_data['next_action'] = 'search_movie_get_title'
            prompt_text = "🎬 Please send me the title of the movie to search for \\(you can include the year\\)\\."
        else:
            context.user_data['next_action'] = 'handle_title_for_tv'
            prompt_text = "📺 Please send me the title of the TV show to search for\\."
        
        await message.edit_text(
            text=prompt_text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        context.user_data['prompt_message_id'] = message.message_id
        return

    elif query.data.startswith("search_resolution_"):
        await query.answer()
        
        resolution = "2160p" if "2160p" in query.data else "1080p"
        final_title = context.user_data.get('search_final_title')
        media_type = context.user_data.get('search_media_type')

        if not final_title or not media_type:
            await message.edit_text("❌ Search context has expired. Please start over.")
            return

        await message.edit_text(
            text=f"🔎 Searching all sources for *{escape_markdown(final_title)}* in *{resolution}*\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        
        search_title = final_title
        year = None
        if media_type == 'movie':
            year_match = re.search(r'\((\d{4})\)', final_title)
            if year_match:
                year = year_match.group(1)
                search_title = final_title[:year_match.start()].strip()
        
        results = await _orchestrate_searches(search_title, media_type, context, year=year, resolution=resolution)

        keys_to_clear = ['active_workflow', 'next_action', 'search_query_title', 'search_final_title', 'search_media_type']
        for key in keys_to_clear:
            context.user_data.pop(key, None)

        if not results:
            await message.edit_text(f"❌ No results found for '`{escape_markdown(final_title)}`' in `{resolution}` across all configured sites\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return
        
        context.user_data['search_results'] = results
        keyboard = []
        
        results_text = f"Found {len(results)} results for *{escape_markdown(final_title)}* in `{resolution}`\\. Please select one to download:"
        
        for i, result in enumerate(results[:10]):
            codec = result.get('codec', 'N/A')
            size_gb = result.get('size_gb', 0.0)
            seeders = result.get('seeders', 0)
            
            button_label = f"{codec} | {size_gb:.2f} GB | S: {seeders}"
            
            keyboard.append([InlineKeyboardButton(button_label, callback_data=f"search_select_{i}")])

        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")])
        await message.edit_text(
            text=results_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    elif query.data.startswith("search_select_"):
        await query.answer()

        search_results = context.user_data.pop('search_results', [])
        if not search_results:
            await query.edit_message_text(text="❌ This selection has expired\\. Please start the search again\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return
        
        try:
            choice_index = int(query.data.split('_')[2])
            selected_result = search_results[choice_index]
            
            page_url_to_process = selected_result['page_url']
            ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[{ts}] [SEARCH] User selected '{selected_result['title']}'. Passing URL/Magnet to handler: {page_url_to_process}")

            # --- THE FIX: Capture the returned torrent_info and proceed to the next steps ---
            ti = await process_user_input(page_url_to_process, context, message)
            if not ti:
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [BUTTON_HANDLER] process_user_input failed to return a torrent_info object. Aborting.")
                return

            error_message, parsed_info = await validate_and_enrich_torrent(ti, message)
            if error_message or not parsed_info:
                if 'torrent_file_path' in context.user_data and os.path.exists(context.user_data['torrent_file_path']):
                    os.remove(context.user_data['torrent_file_path'])
                return

            await send_confirmation_prompt(message, context, ti, parsed_info)
            # --- End of fix ---

        except (ValueError, IndexError):
            await query.edit_message_text(text="❌ An error occurred with your selection\\. Please try again\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    is_delete_action = query.data.startswith("delete_") or query.data == "confirm_delete"
    is_cancel_for_delete = (
        query.data == "cancel_operation" and 
        context.user_data.get('active_workflow') == 'delete'
    )

    if is_delete_action or is_cancel_for_delete:
        await handle_delete_buttons(update, context)
        return

    await query.answer()
    chat_id = message.chat_id
    chat_id_str = str(chat_id)
    active_downloads = context.bot_data.get('active_downloads', {})
    download_queues = context.bot_data.get('download_queues', {})
    
    message_text: str = ""
    reply_markup: Optional[InlineKeyboardMarkup] = None
    parse_mode: Optional[str] = None
    
    if query.data == "pause_download":
        if chat_id_str in active_downloads:
            download_data = active_downloads[chat_id_str]
            lock = download_data.get('lock')
            if lock:
                async with lock:
                    queue_for_user = download_queues.get(chat_id_str, [])
                    if queue_for_user:
                        message_text = "⏸️ Paused and moved to the back of the queue."
                        download_data['requeued'] = True
                        if 'task' in download_data and not download_data['task'].done():
                            download_data['task'].cancel()
                        await query.edit_message_text(text=message_text)
                    else:
                        download_data['is_paused'] = True
                        reply_markup = InlineKeyboardMarkup([[
                            InlineKeyboardButton("▶️ Resume", callback_data="resume_download"),
                            InlineKeyboardButton("⏹️ Cancel", callback_data="cancel_download"),
                        ]])
                        await query.edit_message_reply_markup(reply_markup=reply_markup)
                return
        else:
            message_text = "ℹ️ Could not find an active download to pause."
    
    elif query.data == "resume_download":
        if chat_id_str in active_downloads:
            download_data = active_downloads[chat_id_str]
            lock = download_data.get('lock')
            if lock:
                async with lock:
                    download_data['is_paused'] = False
                    reply_markup = InlineKeyboardMarkup([[
                        InlineKeyboardButton("⏸️ Pause", callback_data="pause_download"),
                        InlineKeyboardButton("⏹️ Cancel", callback_data="cancel_download"),
                    ]])
                    await query.edit_message_reply_markup(reply_markup=reply_markup)
            return
        else:
            message_text = "ℹ️ This download is in the queue and will resume automatically in its turn."
            
    elif query.data in ["cancel_download", "confirm_cancel"]:
        if chat_id_str in active_downloads:
            download_data = active_downloads[chat_id_str]
            lock = download_data.get('lock')
            if lock:
                async with lock:
                    if query.data == "cancel_download":
                        continue_callback = "resume_download" if download_data.get('is_paused') else "pause_download_noop"
                        continue_text = "❌ No, Continue"
                        
                        message_text = "Are you sure you want to cancel this download?"
                        reply_markup = InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ Yes, Cancel", callback_data="confirm_cancel"),
                            InlineKeyboardButton(continue_text, callback_data=continue_callback),
                        ]])
                    elif query.data == "confirm_cancel":
                        download_data.pop('is_paused', None)
                        download_data.pop('requeued', None)
                        if 'task' in download_data and not download_data['task'].done():
                            download_data['task'].cancel()
                        message_text = "✅ Download has been cancelled."
        else:
            message_text = "ℹ️ Could not find an active download to cancel."

    elif query.data == "cancel_operation":
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        user = query.from_user
        
        workflow = context.user_data.get('active_workflow')
        if workflow == 'search':
            log_msg = f"[{ts}] [SEARCH] User {user.id} ({user.username}) cancelled the search workflow." if user else f"[{ts}] [SEARCH] An anonymous user cancelled the search workflow."
            print(log_msg)
        elif 'pending_torrent' in context.user_data:
            log_msg = f"[{ts}] [DOWNLOAD] User {user.id} ({user.username}) cancelled a download confirmation." if user else f"[{ts}] [DOWNLOAD] An anonymous user cancelled a download confirmation."
            print(log_msg)
        
        keys_to_clear = [
            'temp_magnet_choices_details', 'pending_torrent', 'next_action', 
            'prompt_message_id', 'active_workflow', 'search_media_type',
            'search_results', 'search_query_title', 'search_final_title'
        ]
        for key in keys_to_clear:
            context.user_data.pop(key, None)
        message_text = "❌ Operation cancelled\\."
        parse_mode = ParseMode.MARKDOWN_V2

    elif query.data.startswith("select_magnet_"):
        if 'temp_magnet_choices_details' not in context.user_data:
            message_text = "This selection has expired. Please send the link again."
        else:
            selected_index = int(query.data.split('_')[2])
            choices = context.user_data.pop('temp_magnet_choices_details')
            selected_choice = next((c for c in choices if c['index'] == selected_index), None)
            if not selected_choice:
                message_text = "An internal error occurred. Please try again."
            else:
                bencoded_metadata = selected_choice['bencoded_metadata']
                ti = lt.torrent_info(bencoded_metadata) #type: ignore
                context.user_data['pending_magnet_link'] = selected_choice['magnet_link']
                error_message, parsed_info = await validate_and_enrich_torrent(ti, message)
                if error_message or not parsed_info:
                    return
                await send_confirmation_prompt(message, context, ti, parsed_info)
                return

    elif query.data == "confirm_download":
        pending_torrent = context.user_data.pop('pending_torrent', None)
        if not pending_torrent:
            message_text = "This action has expired. Please send the link again."
        else:
            ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            if chat_id_str in active_downloads:
                active_data = active_downloads[chat_id_str]
                if active_data.get('is_paused', False):
                    print(f"[{ts}] [BUTTON_HANDLER] New download added while one is paused. Requeueing the paused one.")
                    active_data['requeued'] = True
                    if 'task' in active_data and not active_data['task'].done():
                        active_data['task'].cancel()

            save_paths = context.bot_data["SAVE_PATHS"]
            initial_save_path = save_paths['default']
            download_data = { 'source_dict': pending_torrent, 'chat_id': chat_id, 'message_id': pending_torrent['original_message_id'], 'save_path': initial_save_path }
            
            if chat_id_str not in download_queues: download_queues[chat_id_str] = []
            download_queues[chat_id_str].append(download_data)
            position = len(download_queues[chat_id_str])
            print(f"[{ts}] [BUTTON_HANDLER] User {chat_id_str} confirmed download. Queued at position {position}.")
            
            is_truly_active = chat_id_str in active_downloads and not active_downloads[chat_id_str].get('requeued')
            if is_truly_active:
                message_text = f"✅ Download queued. You are position #{position} in line."
            else:
                message_text = f"✅ Your download is next in line and will begin shortly."
                
            save_state(context.bot_data['persistence_file'], active_downloads, download_queues)
            await process_queue_for_user(chat_id, context.application)
    
    elif query.data == "pause_download_noop":
         if chat_id_str in active_downloads:
            download_data = active_downloads[chat_id_str]
            download_data['cancellation_pending'] = False
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("⏸️ Pause", callback_data="pause_download"), InlineKeyboardButton("⏹️ Cancel", callback_data="cancel_download")]])
            await query.edit_message_reply_markup(reply_markup=reply_markup)
            return

    if not message_text:
        return

    try:
        await query.edit_message_text(text=message_text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit message in main button_handler: {e}")

class ProgressReporter:
    def __init__(self, application: Application, chat_id: int, message_id: int, parsed_info: Dict[str, Any], clean_name: str, download_data: Dict[str, Any]):
        self.application, self.chat_id, self.message_id, self.parsed_info, self.clean_name, self.download_data = application, chat_id, message_id, parsed_info, clean_name, download_data
        self.last_update_time: float = 0

    async def report(self, status: lt.torrent_status): # type: ignore
        async with self.download_data['lock']:
            if self.download_data.get('cancellation_pending', False):
                return

            log_name = status.name if status.name else self.clean_name
            progress_percent = status.progress * 100
            speed_mbps = status.download_rate / 1024 / 1024
            ts_progress = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # Reduce log spam by only logging active downloads
            if not self.download_data.get('is_paused', False):
                print(f"[{ts_progress}] [LOG] {log_name}: {progress_percent:.2f}% | Peers: {status.num_peers} | Speed: {speed_mbps:.2f} MB/s")
            
            current_time = time.monotonic()
            if current_time - self.last_update_time < 5:
                return
            self.last_update_time = current_time

            if self.parsed_info.get('type') == 'tv':
                seasson_str = "season"
                episode_str = "episode"
                episode_title_str = "episode_title"
                empty_string = ""
                name_str = f"`{escape_markdown(self.parsed_info.get('title', ''))}`\n`{escape_markdown(f'S{self.parsed_info.get(seasson_str, 0):02d}E{self.parsed_info.get(episode_str, 0):02d} - {self.parsed_info.get(episode_title_str, empty_string)}')}`"
            else:
                name_str = f"`{escape_markdown(self.clean_name)}`"

            # --- THE FIX: Conditionally set the header AND the state string ---
            is_paused = self.download_data.get('is_paused', False)
            
            header_str = "⏸️ *Paused:*" if is_paused else "⬇️ *Downloading:*"
            state_str = "*paused*" if is_paused else escape_markdown(status.state.name)
            
            message_text = (
                f"{header_str}\n{name_str}\n"
                f"*Progress:* {escape_markdown(f'{progress_percent:.2f}')}%\n"
                f"*State:* {state_str}\n"
                f"*Peers:* {status.num_peers}\n"
                f"*Speed:* {escape_markdown(f'{speed_mbps:.2f}')} MB/s"
            )

            if is_paused:
                reply_markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton("▶️ Resume", callback_data="resume_download"),
                    InlineKeyboardButton("⏹️ Cancel", callback_data="cancel_download")
                ]])
            else:
                reply_markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton("⏸️ Pause", callback_data="pause_download"),
                    InlineKeyboardButton("⏹️ Cancel", callback_data="cancel_download")
                ]])
            # --- End of fix ---
            
            try:
                await self.application.bot.edit_message_text(
                    text=message_text,
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=reply_markup
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit Telegram message: {e}")

def cleanup_download_resources(
    application: Application,
    chat_id: int,
    source_type: str,
    source_value: str,
    base_save_path: str
):
    """
    Handles all post-task cleanup, including application state and file system.
    """
    ts_final = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts_final}] [INFO] Cleaning up resources for task for chat_id {chat_id}.")
    
    # --- THE FIX: Call the new save_state function ---
    active_downloads = application.bot_data.get('active_downloads', {})
    download_queues = application.bot_data.get('download_queues', {})
    if str(chat_id) in active_downloads:
        del active_downloads[str(chat_id)]
        save_state(application.bot_data['persistence_file'], active_downloads, download_queues)
    # --- End of fix ---

    if source_type == 'file' and source_value and os.path.exists(source_value):
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [CLEANUP] Deleting temporary .torrent file: {source_value}")
        os.remove(source_value)

    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [CLEANUP] Scanning '{base_save_path}' for leftover .parts files...")
    try:
        for filename in os.listdir(base_save_path):
            if filename.endswith(".parts"):
                parts_file_path = os.path.join(base_save_path, filename)
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [CLEANUP] Found and deleting leftover parts file: {parts_file_path}")
                os.remove(parts_file_path)
    except Exception as e:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] Could not perform .parts file cleanup: {e}")

async def handle_successful_download(
    ti: lt.torrent_info, # type: ignore
    parsed_info: Dict[str, Any],
    initial_download_path: str, # The source directory (e.g., '~/Downloads')
    save_paths: Dict[str, str],   # The full paths config
    plex_config: Optional[Dict[str, str]]
) -> str:
    """
    Moves completed downloads from the initial path to the correct final media directory.
    (Refactored to be type-safe)
    
    Args:
        ti: The torrent_info object for the completed download.
        parsed_info: The enriched metadata for the torrent.
        initial_download_path: The directory where the download was initially saved.
        save_paths: The dictionary containing 'movies' and 'tv_shows' final paths.
        plex_config: A dictionary with 'url' and 'token' for the Plex server.
        
    Returns:
        A formatted string to be sent to the user as the final status message.
    """
    scan_status_message = ""
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    media_type = parsed_info.get('type')

    if media_type == 'tv':
        clean_name = f"{parsed_info.get('title')} - S{parsed_info.get('season', 0):02d}E{parsed_info.get('episode', 0):02d}"
    else:
        clean_name = parsed_info.get('title', 'Download')

    try:
        files = ti.files()
        target_file_path_in_torrent = None
        original_extension = ".mkv"

        for i in range(files.num_files()):
            _, ext = os.path.splitext(files.file_path(i))
            if ext.lower() in ALLOWED_EXTENSIONS:
                target_file_path_in_torrent = files.file_path(i)
                original_extension = ext
                break
        
        if not target_file_path_in_torrent:
            raise FileNotFoundError("No valid media file (.mkv, .mp4) found in the completed torrent.")

        final_filename = generate_plex_filename(parsed_info, original_extension)
        
        destination_directory_root: Optional[str] = None
        if media_type == 'movie':
            destination_directory_root = save_paths.get('movies', save_paths.get('default'))
        elif media_type == 'tv':
            destination_directory_root = save_paths.get('tv_shows', save_paths.get('default'))
        else: # Fallback for 'unknown' or other types
            destination_directory_root = save_paths.get('default')

        # --- THE FIX: Type guard to ensure the path is not None ---
        if not destination_directory_root:
            raise ValueError("Configuration error: 'default_save_path' is missing or invalid.")

        destination_directory: str = destination_directory_root
        # --- End of fix ---
        
        if media_type == 'tv':
            show_title = parsed_info.get('title', 'Unknown Show')
            season_num = parsed_info.get('season', 0)
            
            invalid_chars = r'<>:"/\|?*'
            safe_show_title = "".join(c for c in show_title if c not in invalid_chars)
            show_directory = os.path.join(destination_directory_root, safe_show_title)
            season_prefix = f"Season {season_num:02d}"
            destination_directory = os.path.join(show_directory, season_prefix)

        os.makedirs(destination_directory, exist_ok=True)
        
        current_path = os.path.join(initial_download_path, target_file_path_in_torrent)
        new_path = os.path.join(destination_directory, final_filename)
        
        print(f"[{ts}] [MOVE] Invoking move operation...\n     From: {current_path}\n     To:   {new_path}")
        await asyncio.to_thread(shutil.move, current_path, new_path)
        
        ts_after_move = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts_after_move}] [MOVE] Move operation completed successfully.")
        
        if plex_config:
            if library_name := ('Movies' if media_type == 'movie' else 'TV Shows' if media_type == 'tv' else None):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PLEX] Attempting to scan '{library_name}' library...")
                try:
                    plex = await asyncio.to_thread(PlexServer, plex_config['url'], plex_config['token'])
                    target_library = await asyncio.to_thread(plex.library.section, library_name)
                    await asyncio.to_thread(target_library.update)
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PLEX] Successfully triggered scan for '{library_name}' library.")
                    scan_status_message = f"\n\nPlex scan for the `{escape_markdown(library_name)}` library has been initiated\\."
                except (Unauthorized, NotFound, Exception) as e:
                    error_map = { Unauthorized: "Plex token is invalid.", NotFound: f"Plex library '{library_name}' not found." }
                    error_reason = error_map.get(type(e), f"An unexpected error occurred: {e}")
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PLEX ERROR] {error_reason}")
                    scan_status_message = f"\n\n*Plex Error:* Could not trigger scan\\."

        original_top_level_dir = os.path.join(initial_download_path, target_file_path_in_torrent.split(os.path.sep)[0])
        if os.path.isdir(original_top_level_dir) and not os.listdir(original_top_level_dir):
             print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [CLEANUP] Deleting empty original directory: {original_top_level_dir}")
             shutil.rmtree(original_top_level_dir)

    except Exception as e:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] Post-processing failed: {e}")
        return (
            f"❌ *Post-Processing Error*\n"
            f"Download completed but failed during file handling\\.\n\n"
            f"`{escape_markdown(str(e))}`"
        )
        
    return (
        f"✅ *Success\\!*\n"
        f"Renamed and moved to Plex Server:\n"
        f"`{escape_markdown(clean_name)}`"
        f"{scan_status_message}"
    )

async def start_download_task(download_data: Dict, application: Application):
    """
    Creates, registers, and persists a new download task.
    """
    active_downloads = application.bot_data.get('active_downloads', {})
    download_queues = application.bot_data.get('download_queues', {})
    chat_id_str = str(download_data['chat_id'])

    download_data['lock'] = asyncio.Lock()
    task = asyncio.create_task(download_task_wrapper(download_data, application))
    download_data['task'] = task
    active_downloads[chat_id_str] = download_data
    
    save_state(application.bot_data['persistence_file'], active_downloads, download_queues)

    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("⏹️ Cancel Download", callback_data="cancel_download")]])
    message_text = "▶️ Your download is now starting..."
    
    try:
        await application.bot.edit_message_text(
            text=message_text,
            chat_id=download_data['chat_id'],
            message_id=download_data['message_id'],
            reply_markup=reply_markup
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not edit message to start queued download: {e}")

async def download_task_wrapper(download_data: Dict, application: Application):
    """
    (REVISED) Wraps the entire download lifecycle, now with a trigger
    for the automated chat clearing and handling for being paused and requeued.
    """
    source_dict = download_data['source_dict']
    chat_id = download_data['chat_id']
    message_id = download_data['message_id']
    initial_save_path = download_data['save_path']
    clean_name = source_dict.get('clean_name', "Download")
    message_text = "No message"
    
    reporter = ProgressReporter(application, chat_id, message_id, source_dict.get('parsed_info', {}), clean_name, download_data)

    try:
        success, ti = await download_with_progress(
            source=source_dict['value'], 
            save_path=initial_save_path,
            status_callback=reporter.report,
            bot_data=application.bot_data,
            download_data=download_data, # Pass the dictionary through
            allowed_extensions=ALLOWED_EXTENSIONS
        )

        if success and ti:
            message_text = await handle_successful_download(
                ti=ti,
                parsed_info=source_dict.get('parsed_info', {}),
                initial_download_path=initial_save_path,
                save_paths=application.bot_data.get("SAVE_PATHS", {}),
                plex_config=application.bot_data.get("PLEX_CONFIG")
            )
        else:
            # If success is false, it could be a requeue, check the flag.
            if not download_data.get('requeued', False):
                message_text = "❌ *Download Failed*\nAn unknown error occurred in the download manager."

    except asyncio.CancelledError:
        # --- THE FIX: Check for requeue signal before setting cancel message ---
        if download_data.get('requeued', False):
             ts_requeue = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
             print(f"[{ts_requeue}] [INFO] Task for '{clean_name}' cancelled for requeue.")
             # Message is handled by the finally block logic.
        elif application.bot_data.get('is_shutting_down', False):
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Task for '{clean_name}' paused for shutdown.")
            raise # Re-raise to be handled by post_shutdown
        else: # This is a true user cancellation
            message_text = f"⏹️ *Cancelled*\nDownload has been stopped for:\n`{escape_markdown(clean_name)}`"
            
    except Exception as e:
        ts_except = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts_except}] [ERROR] Unexpected exception in download task for '{clean_name}': {e}")
        message_text = f"❌ *Error*\nAn unexpected error occurred:\n`{escape_markdown(str(e))}`"
            
    finally:
        # --- THE FIX: This block now handles requeueing as well as final states ---
        if download_data.get('requeued', False):
            ts_requeue_fin = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[{ts_requeue_fin}] [REQUEUE] Moving paused/interrupted download to back of queue.")
            
            chat_id_str = str(chat_id)
            active_downloads = application.bot_data.get('active_downloads', {})
            download_queues = application.bot_data.get('download_queues', {})

            # Clean up the download data for requeueing but keep pause state
            download_data.pop('task', None)
            download_data.pop('handle', None)
            download_data.pop('requeued', None)
            download_data['is_paused'] = True # Ensure it's marked as paused for the queue
            
            # Add to the back of the queue
            if chat_id_str not in download_queues:
                download_queues[chat_id_str] = []
            download_queues[chat_id_str].append(download_data)
            
            # Remove from active downloads
            if chat_id_str in active_downloads:
                del active_downloads[chat_id_str]
            
            save_state(application.bot_data['persistence_file'], active_downloads, download_queues)
            await process_queue_for_user(chat_id, application) # Start the next download
        
        elif not application.bot_data.get('is_shutting_down', False):
            # This is the original cleanup logic for a completed/failed/cancelled download
            final_message = None
            try:
                final_message = await application.bot.edit_message_text(
                    text=message_text, chat_id=chat_id, message_id=message_id, 
                    parse_mode=ParseMode.MARKDOWN_V2, reply_markup=None
                )
            except (BadRequest, NetworkError) as e:
                 if "Message is not modified" not in str(e):
                    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not send final status message: {e}")
            
            cleanup_download_resources(application, chat_id, source_dict['type'], source_dict['value'], initial_save_path)
            await process_queue_for_user(chat_id, application)

            # --- THE NEW TRIGGER LOGIC ---
            queues = application.bot_data.get('download_queues', {})
            if not queues.get(str(chat_id)) and final_message:
                ts_trigger = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                print(f"[{ts_trigger}] [TRIGGER] Queue for chat {chat_id} is empty. Scheduling delayed clear.")
                await schedule_delayed_clear(chat_id, final_message.message_id, application)

# --- MAIN SCRIPT EXECUTION ---
if __name__ == '__main__':
    PERSISTENCE_FILE = 'persistence.json'

    try:
        BOT_TOKEN, SAVE_PATHS, ALLOWED_USER_IDS, PLEX_CONFIG, SEARCH_CONFIG = get_configuration()
    except (FileNotFoundError, ValueError) as e:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] CRITICAL ERROR: {e}")
        sys.exit(1)

    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting bot...")
    
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    
    application.bot_data["SAVE_PATHS"] = SAVE_PATHS
    application.bot_data["PLEX_CONFIG"] = PLEX_CONFIG
    application.bot_data["SEARCH_CONFIG"] = SEARCH_CONFIG
    application.bot_data["persistence_file"] = PERSISTENCE_FILE
    application.bot_data["ALLOWED_USER_IDS"] = ALLOWED_USER_IDS
    application.bot_data.setdefault('active_downloads', {})
    application.bot_data.setdefault('download_queues', {})

    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Creating global libtorrent session for the application.")
    application.bot_data["TORRENT_SESSION"] = lt.session({ #type: ignore
        'listen_interfaces': '0.0.0.0:6881', 
        'dht_bootstrap_nodes': 'router.utorrent.com:6881,router.bittorrent.com:6881,dht.transmissionbt.com:6881'
    })
    
    # 1. Command Handlers (Most specific)
    # --- REVISED: Replaced old search handlers with the new consolidated one ---
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^/?search$', re.IGNORECASE)), search_command))
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^/?links$', re.IGNORECASE)), links_command))
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^/?help$', re.IGNORECASE)), help_command))
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^/?start$', re.IGNORECASE)), help_command))
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^/?status$', re.IGNORECASE)), plex_status_command))
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^/?restart$', re.IGNORECASE)), plex_restart_command))
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^/?delete$', re.IGNORECASE)), delete_command))
    
    # 2. Callback Query Handler for all button presses
    application.add_handler(CallbackQueryHandler(button_handler))

    # 3. Message Handler specifically for magnet/http links (uses a specific Regex filter)
    link_filter = filters.Regex(r'^(magnet:?|https?://)')
    application.add_handler(MessageHandler(link_filter & ~filters.COMMAND, handle_link_message))
    
    # 4. General Text Handler for conversational workflows (search/delete replies)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search_message))
    
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Bot startup complete. Handlers registered.")
    application.run_polling()