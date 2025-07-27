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
from typing import Optional, Dict, Tuple, List, Set, Any
import shutil
import subprocess
import platform

from plexapi.server import PlexServer
from plexapi.exceptions import NotFound, Unauthorized

from telegram import Update, Message, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ApplicationBuilder, CallbackContext, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError
import libtorrent as lt

from download_torrent import download_with_progress

# --- CONFIGURATION & NEW CONSTANTS ---
MAX_TORRENT_SIZE_GB = 10
MAX_TORRENT_SIZE_BYTES = MAX_TORRENT_SIZE_GB * (1024**3)
ALLOWED_EXTENSIONS = ['.mkv', '.mp4']

def escape_markdown(text: str) -> str:
    """Helper function to escape telegram's special characters."""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(rf'([{re.escape(escape_chars)}])', r'\\\1', text)

def get_configuration() -> tuple[str, dict, list[int], dict]:
    """
    Reads bot token, paths, allowed IDs, and Plex config from the config.ini file.
    (Refactored to correctly expand user home directory paths)
    """
    config = configparser.ConfigParser()
    config_path = 'config.ini'
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file '{config_path}' not found. Please create it.")
    
    config.read(config_path)
    
    token = config.get('telegram', 'bot_token', fallback=None)
    if not token or token == "PLACE_TOKEN_HERE":
        raise ValueError(f"Bot token not found or not set in '{config_path}'.")
        
    paths = {
        'default': config.get('host', 'default_save_path', fallback=None),
        'movies': config.get('host', 'movies_save_path', fallback=None),
        'tv_shows': config.get('host', 'tv_shows_save_path', fallback=None)
    }

    # --- THE FIX: Expand the user tilde (~) and resolve to absolute paths ---
    # This ensures that paths like '~/Downloads' are correctly interpreted.
    for key, value in paths.items():
        if value:
            paths[key] = os.path.expanduser(value)
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [CONFIG] Resolved path for '{key}': {paths[key]}")
    # --- End of fix ---

    if not paths['default']:
        raise ValueError("'default_save_path' is mandatory and was not found in the config file.")

    if not paths['movies']:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] 'movies_save_path' not set. Falling back to default path for movies.")
        paths['movies'] = paths['default']
    if not paths['tv_shows']:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] 'tv_shows_save_path' not set. Falling back to default path for TV shows.")
        paths['tv_shows'] = paths['default']
    
    for path_type, path_value in paths.items():
        if path_value is not None:
            # The directory existence check will now work on the correct, expanded path
            if not os.path.exists(path_value):
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] INFO: {path_type.capitalize()} path '{path_value}' not found. Creating it.")
                os.makedirs(path_value)

    allowed_ids_str = config.get('telegram', 'allowed_user_ids', fallback='')
    allowed_ids = []
    if not allowed_ids_str:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] 'allowed_user_ids' is empty. The bot will be accessible to everyone.")
    else:
        try:
            allowed_ids = [int(id.strip()) for id in allowed_ids_str.split(',') if id.strip()]
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Bot access is restricted to the following User IDs: {allowed_ids}")
        except ValueError:
            raise ValueError("Invalid entry in 'allowed_user_ids'.")

    plex_config = {}
    if config.has_section('plex'):
        plex_url = config.get('plex', 'plex_url', fallback=None)
        plex_token = config.get('plex', 'plex_token', fallback=None)
        if plex_url and plex_token and plex_token != "YOUR_PEX_TOKEN_HERE":
            plex_config = {'url': plex_url, 'token': plex_token}
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Plex configuration loaded successfully.")
        else:
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Plex section found, but URL or token is missing or default. Plex scanning will be disabled.")
    else:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] No [plex] section in config file. Plex scanning will be disabled.")

    return token, paths, allowed_ids, plex_config

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
            
        text = (
            f"⬇️ *Fetching Metadata...*\n"
            f"`Magnet Link`\n\n"
            f"*Please wait, this can be slow.*\n"
            f"*The bot is NOT frozen.*\n\n"
            f"Elapsed Time: `{elapsed}s`"
        )
        try:
            await progress_message.edit_text(text, parse_mode=ParseMode.MARKDOWN_V2)
        except BadRequest:
            pass # Ignore "message not modified"
            
        try:
            # Wait for 1 second, but break immediately if cancelled.
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

    # Get the global session from the application context
    ses = context.bot_data["TORRENT_SESSION"]
    
    # Run the blocking code in a worker thread, passing the global session
    bencoded_metadata = await asyncio.to_thread(_blocking_fetch_metadata, ses, magnet_link)
    
    # Signal the timer to stop and wait for it to finish.
    cancel_timer.set()
    await timer_task

    if bencoded_metadata:
        # Reconstruct the torrent_info object from the bytes in the main thread
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Reconstructing torrent_info object from bencoded data.")
        ti = lt.torrent_info(bencoded_metadata) #type: ignore
        return ti
    else:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Metadata fetch failed or timed out.")
        error_message_text = "Timed out fetching metadata from the magnet link. It might be inactive or poorly seeded."
        await progress_message.edit_text(f"❌ *Error:* {escape_markdown(error_message_text)}", parse_mode=ParseMode.MARKDOWN_V2)
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

    # The fix is to remove the parse_mode argument, as this is a simple text message.
    await progress_message.edit_text(
        f"Found {len(magnet_links)} links. Fetching details... this may take a moment."
    )

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
            
    # Sort by the original index to maintain the order from the webpage
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

    This function performs the following checks and actions:
    1. Validates torrent size against MAX_TORRENT_SIZE_BYTES.
    2. Validates file types using validate_torrent_files.
    3. Parses the torrent name to identify movie/TV show.
    4. For TV shows, fetches the episode title from Wikipedia.
    5. If a corrected show title is found, it updates the parsed info.

    Args:
        ti: The torrent_info object to process.
        progress_message: The message to edit with progress updates.

    Returns:
        A tuple containing:
        - An error message string if validation fails, otherwise None.
        - A dictionary with the fully parsed and enriched information if
          validation succeeds, otherwise None.
    """
    # 1. Validate Torrent Size
    if ti.total_size() > MAX_TORRENT_SIZE_BYTES:
        error_msg = f"This torrent is *{format_bytes(ti.total_size())}*, which is larger than the *{MAX_TORRENT_SIZE_GB} GB* limit."
        await progress_message.edit_text(f"❌ *Size Limit Exceeded*\n\n{error_msg}", parse_mode=ParseMode.MARKDOWN_V2)
        return "Size limit exceeded", None

    # 2. Validate Torrent File Types
    validation_error = validate_torrent_files(ti)
    if validation_error:
        error_msg = f"This torrent {validation_error}"
        await progress_message.edit_text(f"❌ *Unsupported File Type*\n\n{error_msg}", parse_mode=ParseMode.MARKDOWN_V2)
        return f"Unsupported file type", None

    # 3. Parse Torrent Name and Enrich Metadata
    parsed_info = parse_torrent_name(ti.name())

    if parsed_info['type'] == 'tv':
        wiki_search_msg = escape_markdown("TV show detected. Searching Wikipedia for episode title...")
        await progress_message.edit_text(f"📺 {wiki_search_msg}", parse_mode=ParseMode.MARKDOWN_V2)

        episode_title, corrected_show_title = await fetch_episode_title_from_wikipedia(
            show_title=parsed_info['title'],
            season=parsed_info['season'],
            episode=parsed_info['episode']
        )
        parsed_info['episode_title'] = episode_title

        # If Wikipedia provided a better title (e.g., from a redirect), use it.
        if corrected_show_title:
            parsed_info['title'] = corrected_show_title

    # 4. Return success (no error message) and the enriched info
    return None, parsed_info

async def process_user_input(
    text: str,
    context: ContextTypes.DEFAULT_TYPE,
    progress_message: Message
) -> Optional[lt.torrent_info]: # type: ignore
    """
    Analyzes user input text to acquire a torrent_info object.
    It handles direct magnet links, .torrent file URLs, and webpages
    containing magnet links. If multiple magnets are found, it presents
    a detailed selection UI and returns None, handing off to the button_handler.
    """
    if context.user_data is None:
        context.user_data = {}

    source_type: Optional[str] = None
    source_value: Optional[str] = None
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
            await progress_message.edit_text(f"❌ *Error:* {escape_markdown(error_msg)}", parse_mode=ParseMode.MARKDOWN_V2)
            return None
        except RuntimeError:
            await progress_message.edit_text(r"❌ *Error:* The provided file is not a valid torrent\.", parse_mode=ParseMode.MARKDOWN_V2)
            return None

    elif text.startswith(('http://', 'https://')):
        safe_message_part = escape_markdown("Attempting to find magnet link(s) on:")
        await progress_message.edit_text(
            f"🌐 *Web Page Detected:*\n{safe_message_part}\n`{escape_markdown(text)}`",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        extracted_magnet_links = await find_magnet_link_on_page(text)

        if not extracted_magnet_links:
            error_msg = "The provided URL does not contain any magnet links, or the page could not be accessed."
            await progress_message.edit_text(f"❌ *Error:* {escape_markdown(error_msg)}", parse_mode=ParseMode.MARKDOWN_V2)
            return None

        if len(extracted_magnet_links) == 1:
            magnet_link = extracted_magnet_links[0]
            context.user_data['pending_magnet_link'] = magnet_link
            return await fetch_metadata_from_magnet(magnet_link, progress_message, context)

        if len(extracted_magnet_links) > 1:
            parsed_choices = await fetch_and_parse_magnet_details(extracted_magnet_links, context, progress_message)

            if not parsed_choices:
                error_msg = "Could not fetch details for any of the found magnet links. They may be inactive."
                await progress_message.edit_text(f"❌ *Error:* {escape_markdown(error_msg)}", parse_mode=ParseMode.MARKDOWN_V2)
                return None

            context.user_data['temp_magnet_choices_details'] = parsed_choices

            # Determine the common title from the first valid torrent.
            first_choice_name = parsed_choices[0]['name']
            parsed_title_info = parse_torrent_name(first_choice_name)
            
            common_title = "Unknown Title"
            if parsed_title_info.get('type') == 'movie':
                common_title = f"{parsed_title_info.get('title', '')} ({parsed_title_info.get('year', '')})".strip()
            else: # For TV shows or unknown types, use the parsed title.
                common_title = parsed_title_info.get('title', first_choice_name)

            # Construct the message text with a title and subtitle.
            header_text = f"*{escape_markdown(common_title)}*\n\n"
            subtitle_text = f"Found {len(parsed_choices)} valid torrents\\. Please select one:"
            final_text = header_text + subtitle_text

            # Construct the keyboard with details on the buttons.
            keyboard = []
            for choice in parsed_choices:
                button_label = f"{choice['resolution']} | {choice['file_type']} | {choice['size']}"
                keyboard.append([
                    InlineKeyboardButton(
                        button_label,
                        callback_data=f"select_magnet_{choice['index']}"
                    )
                ])

            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            await progress_message.edit_text(
                final_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return None
    else:
        error_message_text = "This does not look like a valid .torrent URL, magnet link, or a web page containing a magnet link."
        await progress_message.edit_text(f"❌ *Error:* {escape_markdown(error_message_text)}", parse_mode=ParseMode.MARKDOWN_V2)
        return None

async def send_confirmation_prompt(
    progress_message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    ti: lt.torrent_info, # type: ignore
    parsed_info: Dict[str, Any]
) -> None:
    """
    Formats and sends the final confirmation message to the user with buttons,
    including resolution, file type, and size.
    """
    if context.user_data is None:
        context.user_data = {}

    display_name = ""
    if parsed_info['type'] == 'movie':
        display_name = f"{parsed_info['title']} ({parsed_info['year']})"
    elif parsed_info['type'] == 'tv':
        base_name = f"{parsed_info['title']} - S{parsed_info['season']:02d}E{parsed_info['episode']:02d}"
        display_name = f"{base_name} - {parsed_info['episode_title']}" if parsed_info.get('episode_title') else base_name
    else:
        display_name = parsed_info['title']

    resolution = parse_resolution_from_name(ti.name())
    file_type_str = get_dominant_file_type(ti.files())
    total_size_str = format_bytes(ti.total_size())

    details_line = f"{resolution} | {file_type_str} | {total_size_str}"

    confirmation_text = (
        f"✅ *Validation Passed*\n\n"
        f"*Name:* {escape_markdown(display_name)}\n"
        f"*Details:* `{escape_markdown(details_line)}`\n\n"
        f"Do you want to start this download?"
    )

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

    await progress_message.edit_text(
        confirmation_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN_V2
    )

# --- PERSISTENCE FUNCTIONS ---

def save_state(file_path: str, active_downloads: Dict, download_queues: Dict):
    """Saves the state of active and queued downloads to a JSON file."""
    serializable_active = {}
    for chat_id, download_data in active_downloads.items():
        data_copy = download_data.copy()
        data_copy.pop('task', None)
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

async def start_command(update: Update, context: CallbackContext) -> None:
    """Sends a message with instructions and torrent site links when the /start command is issued."""
    # Ensure update.message is not None before trying to use it.
    # This check satisfies the type checker and adds robustness.
    if update.message is None:
        # In this specific context (CommandHandler for /start), this case is highly unlikely,
        # but adding it makes the type checker happy and provides a fallback.
        if update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Hello! Please send /start again." # A simpler fallback
            )
        return

    welcome_message = """
Send me a .torrent or .magnet link!

For Movies:
https://yts.mx/
https://1337x.to/
https://thepiratebay.org/

For TV Shows:
https://eztvx.to/
https://1337x.to/
"""
    await update.message.reply_text(welcome_message)

# You would then register this handler in your main bot setup, for example:
# application.add_handler(CommandHandler("start", start))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Provides a formatted list of available commands."""
    if not await is_user_authorized(update, context):
        return
    if not update.message: return
    
    # Using MarkdownV2 for nice formatting.
    # Note that special characters like '.', '-', and '!' must be escaped with a '\'.
    help_text = (
        r"Here are the available commands:\n\n"
        r"`start` \- Show welcome message\.\n"
        r"`plexstatus` \- Check Plex\.\n"
        r"`plexrestart` \- Restart Plex\.\n\n"
    )
    
    await update.message.reply_text(
        text=help_text,
        parse_mode=ParseMode.MARKDOWN_V2
    )

async def plex_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Checks the connection to the Plex Media Server."""
    if not await is_user_authorized(update, context):
        return
    if not update.message: return

    status_message = await update.message.reply_text("Plex Status: 🟡 Checking connection...")
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    plex_config = context.bot_data.get("PLEX_CONFIG", {})
    if not plex_config:
        await status_message.edit_text("Plex Status: ⚪️ Not configured. Please add your Plex details to the `config.ini` file.")
        return

    try:
        print(f"[{ts}] [PLEX STATUS] Attempting to connect to Plex server...")
        
        # Run blocking Plex calls in a separate thread
        plex = await asyncio.to_thread(PlexServer, plex_config['url'], plex_config['token'])
        server_version = plex.version
        server_platform = plex.platform
        
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PLEX STATUS] Success! Connected to Plex Media Server v{server_version} on {server_platform}.")
        
        success_text = (
            f"Plex Status: ✅ *Connected*\n\n"
            f"*Server Version:* `{escape_markdown(server_version)}`\n"
            f"*Platform:* `{escape_markdown(server_platform)}`"
        )
        await status_message.edit_text(success_text, parse_mode=ParseMode.MARKDOWN_V2)

    except Unauthorized:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PLEX STATUS] ERROR: Connection failed. The Plex token is invalid.")
        error_text = (
            "Plex Status: ❌ *Authentication Failed*\n\n"
            "The Plex API token is incorrect\\. Please check your `config\\.ini` file\\."
        )
        await status_message.edit_text(error_text, parse_mode=ParseMode.MARKDOWN_V2)

    except Exception as e:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [PLEX STATUS] ERROR: An unexpected error occurred: {e}")
        error_text = (
            f"Plex Status: ❌ *Connection Failed*\n\n"
            f"Could not connect to the Plex server at `{escape_markdown(plex_config['url'])}`\\. "
            f"Please ensure the server is running and accessible\\."
        )
        await status_message.edit_text(error_text, parse_mode=ParseMode.MARKDOWN_V2)

async def plex_restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(NEW - Linux Simplified) Restarts the Plex server via a direct subprocess call."""
    if not await is_user_authorized(update, context):
        return
    if not update.message: return

    # Check if the command is being run on Linux
    if platform.system() != "Linux":
        await update.message.reply_text("This command is configured to run on Linux only.")
        return

    status_message = await update.message.reply_text("Plex Restart: 🟡 Sending restart command to the server...")
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    script_path = os.path.abspath("restart_plex.sh")

    if not os.path.exists(script_path):
        print(f"[{ts}] [PLEX RESTART] ERROR: Wrapper script not found at {script_path}")
        await status_message.edit_text("❌ *Error:* The `restart_plex.sh` script was not found in the bot's directory.")
        return
    
    # The command to run, assuming sudoers is pre-configured
    command = ["/usr/bin/sudo", script_path]

    try:
        # Get the absolute path to the script, assuming it's in the same directory as the bot.
        script_path = os.path.abspath("restart_plex.sh")
        
        if not os.path.exists(script_path):
            await status_message.edit_text("❌ *Error:* `restart_plex.sh` not found in the bot's directory.")
            return

        command = ["/usr/bin/sudo", script_path]

        print(f"[{ts}] [PLEX RESTART] Executing wrapper script: {' '.join(command)}")
        
        result = await asyncio.to_thread(
            subprocess.run, command, check=True, capture_output=True, text=True
        )

        success_message = "✅ *Plex Restart Successful*"
        await status_message.edit_text(success_message, parse_mode=ParseMode.MARKDOWN_V2)
        print(f"[{ts}] [PLEX RESTART] Success!")

    except subprocess.CalledProcessError as e:
        error_output = e.stderr or e.stdout
        error_text = f"❌ *Script Failed*\n\nThis almost always means the `sudoers` rule for `restart_plex.sh` is incorrect or missing\\.\n\n*Details:*\n`{escape_markdown(error_output)}`"
        print(f"[{ts}] [PLEX RESTART] ERROR executing script: {error_output}")
        await status_message.edit_text(error_text, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        error_text = f"❌ *An Unexpected Error Occurred*\n\n`{escape_markdown(str(e))}`"
        print(f"[{ts}] [PLEX RESTART] ERROR: {str(e)}")
        await status_message.edit_text(error_text, parse_mode=ParseMode.MARKDOWN_V2)

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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles incoming messages by coordinating the torrent processing pipeline.
    (Refactored to delete user messages immediately)
    """
    if not await is_user_authorized(update, context):
        return
        
    if not update.message or not update.message.text: return
    chat_id = update.message.chat_id
    text = update.message.text.strip()
    user_message_to_delete = update.message

    if context.user_data is None:
        context.user_data = {}
    
    progress_message = await update.message.reply_text("✅ Input received. Analyzing...")

    try:
        await user_message_to_delete.delete()
    except BadRequest as e:
        if "Message to delete not found" not in str(e):
             print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not delete user's message: {e}")

    # --- 1. PROCESS ---
    ti = await process_user_input(text, context, progress_message)
    
    if not ti:
        return

    # --- 2. VALIDATE & ENRICH ---
    error_message, parsed_info = await validate_and_enrich_torrent(ti, progress_message)
    
    if error_message or not parsed_info:
        if 'torrent_file_path' in context.user_data and os.path.exists(context.user_data['torrent_file_path']):
            os.remove(context.user_data['torrent_file_path'])
        return

    # --- 3. CONFIRM ---
    await send_confirmation_prompt(progress_message, context, ti, parsed_info)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles all button presses from inline keyboards.
    (Refactored to add a confirmation step for download cancellation)
    """
    if not await is_user_authorized(update, context):
        return

    query = update.callback_query
    if not query: return
    await query.answer()

    message = query.message
    if not isinstance(message, Message): return
    
    if context.user_data is None:
        context.user_data = {}

    chat_id = message.chat_id
    chat_id_str = str(chat_id)
    active_downloads = context.bot_data.get('active_downloads', {})

    # --- Cancellation Flow for Active Downloads ---

    if query.data == "cancel_download":
        if chat_id_str in active_downloads:
            confirm_text = "Are you sure you want to cancel this download?"
            keyboard = [[
                InlineKeyboardButton("✅ Yes, Cancel", callback_data="confirm_cancel"),
                InlineKeyboardButton("❌ No, Continue", callback_data="resume_download"),
            ]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            try:
                await query.edit_message_text(text=confirm_text, reply_markup=reply_markup)
            except BadRequest: # Ignore if message is already gone/changed
                pass
        else:
            await query.edit_message_text("ℹ️ This download has already completed or been cancelled.", reply_markup=None)
        return

    if query.data == "confirm_cancel":
        if chat_id_str in active_downloads:
            download_data = active_downloads[chat_id_str]
            clean_name = download_data.get('source_dict', {}).get('clean_name', 'your download')
            ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[{ts}] [CANCEL] User {chat_id} confirmed cancellation for '{clean_name}'.")

            if 'task' in download_data and not download_data['task'].done():
                task: asyncio.Task = download_data['task']
                task.cancel() # The task's exception handler will edit the final message.
            else:
                await query.edit_message_text("ℹ️ This download has already completed or been cancelled.", reply_markup=None)
        else:
            await query.edit_message_text("ℹ️ Could not find an active download to cancel.", reply_markup=None)
        return

    if query.data == "resume_download":
        keyboard = [[InlineKeyboardButton("⏹️ Cancel Download", callback_data="cancel_download")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            # Edit the message back to an intermediate state. The ProgressReporter
            # will overwrite this on its next cycle with the full live status.
            await query.edit_message_text(
                text="▶️ Download resuming...",
                reply_markup=reply_markup
            )
        except BadRequest:
            pass # Ignore if message is already gone
        return

    # --- Cancellation Flow for Pending Operations ---

    if query.data == "cancel_operation":
        # For magnet selection
        if 'temp_magnet_choices_details' in context.user_data:
            context.user_data.pop('temp_magnet_choices_details', None)
            await query.edit_message_text("❌ Selection cancelled.")
            return

        # For final download confirmation
        if 'pending_torrent' in context.user_data:
            pending_torrent = context.user_data.pop('pending_torrent')
            await query.edit_message_text("❌ Operation cancelled by user.")
            if pending_torrent.get('type') == 'file' and pending_torrent.get('value') and os.path.exists(pending_torrent.get('value')):
                os.remove(pending_torrent.get('value'))
            return

    # --- Magnet Selection & Download Confirmation Flow ---

    if query.data and query.data.startswith("select_magnet_"):
        if 'temp_magnet_choices_details' not in context.user_data:
            await query.edit_message_text("This selection has expired. Please send the link again.")
            return

        selected_index = int(query.data.split('_')[2])
        choices = context.user_data.pop('temp_magnet_choices_details')
        selected_choice = next((c for c in choices if c['index'] == selected_index), None)

        if not selected_choice:
            await query.edit_message_text("An internal error occurred. Please try again.")
            return

        bencoded_metadata = selected_choice['bencoded_metadata']
        ti = lt.torrent_info(bencoded_metadata) #type: ignore

        context.user_data['pending_magnet_link'] = selected_choice['magnet_link']
        
        error_message, parsed_info = await validate_and_enrich_torrent(ti, message)
        if error_message or not parsed_info:
            return

        await send_confirmation_prompt(message, context, ti, parsed_info)
        return

    if 'pending_torrent' not in context.user_data:
        await query.edit_message_text("This action has expired. Please send the link again.")
        return

    pending_torrent = context.user_data.pop('pending_torrent')
    
    if query.data == "confirm_download":
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        save_paths = context.bot_data["SAVE_PATHS"]
        initial_save_path = save_paths['default']
        
        print(f"[{ts}] [BUTTON_HANDLER] Staging download to initial path: {initial_save_path}")

        download_data = {
            'source_dict': pending_torrent,
            'chat_id': chat_id,
            'message_id': pending_torrent['original_message_id'],
            'save_path': initial_save_path
        }
        
        download_queues = context.bot_data.get('download_queues', {})
        if chat_id_str not in download_queues:
            download_queues[chat_id_str] = []
        
        download_queues[chat_id_str].append(download_data)
        position = len(download_queues[chat_id_str])
        
        print(f"[{ts}] [BUTTON_HANDLER] User {chat_id_str} confirmed download. Added to queue at position {position}.")
        
        if chat_id_str in active_downloads:
             await query.edit_message_text(f"✅ Download queued. You are position #{position} in line.", reply_markup=None)
        else:
             await query.edit_message_text(f"✅ Your download is next in line and will begin shortly.", reply_markup=None)

        save_state(context.bot_data['persistence_file'], active_downloads, download_queues)
        await process_queue_for_user(chat_id, context.application)

class ProgressReporter:
    """
    A class to encapsulate the state and logic for reporting download progress.
    It handles rate-limiting updates to avoid hitting Telegram API limits.
    """
    def __init__(
        self,
        application: Application,
        chat_id: int,
        message_id: int,
        parsed_info: Dict[str, Any],
        clean_name: str
    ):
        self.application = application
        self.chat_id = chat_id
        self.message_id = message_id
        self.parsed_info = parsed_info
        self.clean_name = clean_name
        self.last_update_time: float = 0

    async def report(self, status: lt.torrent_status): # type: ignore
        """
        Formats and sends a progress update to Telegram, respecting a 5-second interval.
        """
        # --- Logging to console on every check ---
        log_name = status.name if status.name else self.clean_name
        progress_percent = status.progress * 100
        speed_mbps = status.download_rate / 1024 / 1024
        ts_progress = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts_progress}] [LOG] {log_name}: {progress_percent:.2f}% | Peers: {status.num_peers} | Speed: {speed_mbps:.2f} MB/s")
        
        # --- Rate-limit updates to Telegram ---
        current_time = time.monotonic()
        if current_time - self.last_update_time < 5:
            return # Don't update if it has been less than 5 seconds
        self.last_update_time = current_time

        # --- Formatting the message for Telegram ---
        name_str = ""
        if self.parsed_info.get('type') == 'tv':
            show_title = self.parsed_info.get('title', 'Unknown Show')
            season_num = self.parsed_info.get('season', 0)
            episode_num = self.parsed_info.get('episode', 0)
            episode_title = self.parsed_info.get('episode_title', 'Unknown Episode')
            safe_show_title = escape_markdown(show_title)
            safe_episode_details = escape_markdown(f"S{season_num:02d}E{episode_num:02d} - {episode_title}")
            name_str = f"`{safe_show_title}`\n`{safe_episode_details}`"
        else:
            safe_clean_name = escape_markdown(self.clean_name)
            name_str = f"`{safe_clean_name}`"

        progress_str = escape_markdown(f"{progress_percent:.2f}")
        speed_str = escape_markdown(f"{speed_mbps:.2f}")
        state_str = escape_markdown(status.state.name)

        telegram_message = (
            f"⬇️ *Downloading:*\n{name_str}\n"
            f"*Progress:* {progress_str}%\n"
            f"*State:* {state_str}\n"
            f"*Peers:* {status.num_peers}\n"
            f"*Speed:* {speed_str} MB/s"
        )
        
        keyboard = [[InlineKeyboardButton("⏹️ Cancel Download", callback_data="cancel_download")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await self.application.bot.edit_message_text(
                text=telegram_message,
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
    
    This function updates the user's message to show that the download is
    starting and provides a cancel button.
    
    Args:
        download_data: The dictionary containing all necessary info for the download.
        application: The main Application object to access bot_data.
    """
    active_downloads = application.bot_data.get('active_downloads', {})
    download_queues = application.bot_data.get('download_queues', {})
    chat_id_str = str(download_data['chat_id'])

    # Create and store the task
    task = asyncio.create_task(download_task_wrapper(download_data, application))
    download_data['task'] = task
    active_downloads[chat_id_str] = download_data
    
    # Persist the new state (active download started, queue might have changed)
    save_state(
        application.bot_data['persistence_file'],
        active_downloads,
        download_queues
    )

    # Let the user know the download is starting and give them a cancel button
    keyboard = [[InlineKeyboardButton("⏹️ Cancel Download", callback_data="cancel_download")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await application.bot.edit_message_text(
            text="▶️ Your download is now starting...",
            chat_id=download_data['chat_id'],
            message_id=download_data['message_id'],
            reply_markup=reply_markup
        )
    except BadRequest as e:
        # This can happen if the original confirmation message was deleted
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts}] [WARN] Could not edit message to start queued download: {e}")

async def download_task_wrapper(download_data: Dict, application: Application):
    """
    (V7 Refactor) Passes the full save paths configuration to the handler.
    """
    # --- 1. SETUP ---
    source_dict = download_data['source_dict']
    chat_id = download_data['chat_id']
    message_id = download_data['message_id']
    initial_save_path = download_data['save_path']
    
    source_value = source_dict['value']
    source_type = source_dict['type']
    clean_name = source_dict.get('clean_name', "Download")
    parsed_info = source_dict.get('parsed_info', {})

    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Starting/Resuming download task for '{clean_name}' for chat_id {chat_id}.")
    
    reporter = ProgressReporter(application, chat_id, message_id, parsed_info, clean_name)

    try:
        # --- 2. EXECUTE ---
        success, ti = await download_with_progress(
            source=source_value, 
            save_path=initial_save_path, # Download to the initial default path
            status_callback=reporter.report,
            bot_data=application.bot_data,
            allowed_extensions=ALLOWED_EXTENSIONS
        )

        # --- 3. POST-PROCESS ON SUCCESS ---
        if success and ti:
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [SUCCESS] Download for '{clean_name}' completed. Starting post-processing.")
            
            final_message = await handle_successful_download(
                ti=ti,
                parsed_info=parsed_info,
                initial_download_path=initial_save_path, # The source directory
                save_paths=application.bot_data.get("SAVE_PATHS", {}), # The full paths config
                plex_config=application.bot_data.get("PLEX_CONFIG")
            )
            
            await application.bot.edit_message_text(
                text=final_message, 
                chat_id=chat_id, 
                message_id=message_id, 
                parse_mode=ParseMode.MARKDOWN_V2
            )

    except asyncio.CancelledError:
        ts_cancel = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if application.bot_data.get('is_shutting_down', False):
            print(f"[{ts_cancel}] [INFO] Task for '{clean_name}' paused due to bot shutdown. It will be resumed on restart.")
            raise
        
        print(f"[{ts_cancel}] [CANCEL] Download task for '{clean_name}' was cancelled by user {chat_id}.")
        final_message = (
            f"⏹️ *Cancelled*\n"
            f"Download has been stopped for:\n"
            f"`{escape_markdown(clean_name)}`"
        )
        try:
            await application.bot.edit_message_text(
                text=final_message, 
                chat_id=chat_id, 
                message_id=message_id, 
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=None
            )
        except (BadRequest, NetworkError) as e:
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not send cancellation confirmation message during shutdown: {e}")
            
    except Exception as e:
        ts_except = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[{ts_except}] [ERROR] An unexpected exception occurred in download task for '{clean_name}': {e}")
        safe_error = escape_markdown(str(e))
        final_message = (
            f"❌ *Error*\n"
            f"An unexpected error occurred:\n"
            f"`{safe_error}`"
        )
        try:
            await application.bot.edit_message_text(
                text=final_message,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=None
            )
        except (BadRequest, NetworkError) as e:
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WARN] Could not send final error message during shutdown: {e}")
            
    finally:
        if not application.bot_data.get('is_shutting_down', False):
            cleanup_download_resources(application, chat_id, source_type, source_value, initial_save_path)
            
            ts_final = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[{ts_final}] [FINALLY] Task for {chat_id} finished normally. Nudging queue processor.")
            await process_queue_for_user(chat_id, application)

# --- MAIN SCRIPT EXECUTION ---
if __name__ == '__main__':
    PERSISTENCE_FILE = 'persistence.json'

    try:
        BOT_TOKEN, SAVE_PATHS, ALLOWED_USER_IDS, PLEX_CONFIG = get_configuration()
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
    application.bot_data["persistence_file"] = PERSISTENCE_FILE
    application.bot_data["ALLOWED_USER_IDS"] = ALLOWED_USER_IDS
    application.bot_data.setdefault('active_downloads', {})
    application.bot_data.setdefault('download_queues', {})

    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Creating global libtorrent session for the application.")
    application.bot_data["TORRENT_SESSION"] = lt.session({ #type: ignore
        'listen_interfaces': '0.0.0.0:6881', 
        'dht_bootstrap_nodes': 'router.utorrent.com:6881,router.bittorrent.com:6881,dht.transmissionbt.com:6881'
    })
    
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^/?hello$', re.IGNORECASE)), start_command))
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^/?start$', re.IGNORECASE)), start_command))
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^/?help$', re.IGNORECASE)), help_command))
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^/?plexstatus$', re.IGNORECASE)), plex_status_command))
    application.add_handler(MessageHandler(filters.Regex(re.compile(r'^/?plexrestart$', re.IGNORECASE)), plex_restart_command))
        
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(button_handler))
    
    application.run_polling()