# telegram_bot/services/torrent_service.py

import asyncio
import os
import tempfile
import time
from typing import Any, Dict, List, Optional

import httpx
import libtorrent as lt

# --- Refactored Import: Import UI components directly from the library ---
from telegram import Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown

# --- Refactored Imports: Changed to absolute for consistency and IDE compatibility ---
from telegram_bot.config import logger, MAX_TORRENT_SIZE_BYTES
from telegram_bot.utils import format_bytes, safe_edit_message
from telegram_bot.services.media_manager import (
    parse_resolution_from_name,
    get_dominant_file_type,
    parse_torrent_name,
)
from telegram_bot.services.scraping_service import find_magnet_link_on_page


async def process_user_input(
    text: str, context: ContextTypes.DEFAULT_TYPE, progress_message: Message
) -> Optional[lt.torrent_info]:  # type: ignore
    """
    Analyzes user input text to acquire a torrent_info object. It handles
    magnet links, direct .torrent URLs, and webpages containing magnet links.
    """
    # --- Refactored Guard Clause: Ensure user_data exists before use ---
    if context.user_data is None:
        logger.error(
            "process_user_input: context.user_data is None. Cannot store state."
        )
        return None

    if text.startswith("magnet:?xt=urn:btih:"):
        context.user_data["pending_magnet_link"] = text
        return await fetch_metadata_from_magnet(text, progress_message, context)

    elif text.startswith(("http://", "https://")) and text.endswith(".torrent"):
        return await _handle_torrent_url(text, context, progress_message)

    elif text.startswith(("http://", "https://")):
        return await _handle_webpage_url(text, context, progress_message)

    else:
        error_msg = "This does not look like a valid \\.torrent URL, magnet link, or web page\\."
        await safe_edit_message(
            progress_message,
            text=f"❌ *Error:* {escape_markdown(error_msg)}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return None


async def _handle_torrent_url(
    url: str, context: ContextTypes.DEFAULT_TYPE, progress_message: Message
) -> Optional[lt.torrent_info]:  # type: ignore
    """Downloads a .torrent file from a URL and returns its torrent_info."""
    if context.user_data is None:
        logger.error(
            "_handle_torrent_url: context.user_data is None. Cannot store state."
        )
        return None

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, follow_redirects=True, timeout=30)
            response.raise_for_status()

        torrent_content = response.content
        ti = lt.torrent_info(torrent_content)  # type: ignore

        info_hash = str(ti.info_hashes().v1)
        torrents_dir = ".torrents"
        os.makedirs(torrents_dir, exist_ok=True)
        file_path = os.path.join(torrents_dir, f"{info_hash}.torrent")
        with open(file_path, "wb") as f:
            f.write(torrent_content)

        context.user_data["torrent_file_path"] = file_path
        return ti

    except httpx.RequestError as e:
        error_msg = f"Failed to download \\.torrent file from URL: {e}"
        await safe_edit_message(
            progress_message,
            text=f"❌ *Error:* {escape_markdown(error_msg)}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except RuntimeError:
        await safe_edit_message(
            progress_message,
            text="❌ *Error:* The provided file is not a valid torrent\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    return None


async def _handle_webpage_url(
    url: str, context: ContextTypes.DEFAULT_TYPE, progress_message: Message
) -> Optional[lt.torrent_info]:  # type: ignore
    """Scrapes a webpage for magnet links and proceeds accordingly."""
    if context.user_data is None:
        logger.error(
            "_handle_webpage_url: context.user_data is None. Cannot store state."
        )
        return None

    logger.info(f"URL detected. Starting web scrape for: {url}")
    await safe_edit_message(
        progress_message,
        text="🌐 Found a web page\\. Scraping for magnet links\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    magnet_links = await find_magnet_link_on_page(url)

    if not magnet_links:
        error_msg = "The provided URL does not contain any magnet links, or the page could not be accessed\\."
        await safe_edit_message(
            progress_message,
            text=f"❌ *Error:* {escape_markdown(error_msg)}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return None

    if len(magnet_links) == 1:
        context.user_data["pending_magnet_link"] = magnet_links[0]
        return await fetch_metadata_from_magnet(
            magnet_links[0], progress_message, context
        )

    parsed_choices = await _fetch_and_parse_magnet_details(
        magnet_links, context, progress_message
    )

    if not parsed_choices:
        error_msg = "Could not fetch details for any of the found magnet links\\. They may be inactive\\."
        await safe_edit_message(
            progress_message,
            text=f"❌ *Error:* {escape_markdown(error_msg)}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return None

    context.user_data["temp_magnet_choices_details"] = parsed_choices

    first_choice_name = parsed_choices[0]["name"]
    parsed_title_info = parse_torrent_name(first_choice_name)
    common_title = parsed_title_info.get("title", first_choice_name)

    header_text = f"*{escape_markdown(common_title)}*\n\n"
    subtitle_text = f"Found {len(parsed_choices)} valid torrents\\. Please select one:"

    keyboard = []
    for choice in parsed_choices:
        button_label = (
            f"{choice['resolution']} | {choice['file_type']} | {choice['size']}"
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    button_label, callback_data=f"select_magnet_{choice['index']}"
                )
            ]
        )
    keyboard.append(
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]
    )

    await safe_edit_message(
        progress_message,
        text=header_text + subtitle_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return None


async def _fetch_and_parse_magnet_details(
    magnet_links: List[str],
    context: ContextTypes.DEFAULT_TYPE,
    progress_message: Message,
) -> List[Dict[str, Any]]:
    """Fetches metadata for multiple magnet links in parallel and parses their details."""
    ses = context.bot_data["TORRENT_SESSION"]
    await safe_edit_message(
        progress_message,
        text=f"Found {len(magnet_links)} links. Fetching details\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    async def fetch_one(magnet_link: str, index: int):
        """Worker to fetch metadata for a single magnet link."""
        bencoded_metadata = await asyncio.to_thread(
            _blocking_fetch_metadata, ses, magnet_link
        )
        if bencoded_metadata:
            ti = lt.torrent_info(bencoded_metadata)  # type: ignore
            return {
                "index": index,
                "ti": ti,
                "magnet_link": magnet_link,
                "bencoded_metadata": bencoded_metadata,
            }
        return None

    tasks = [fetch_one(link, i) for i, link in enumerate(magnet_links)]
    results = await asyncio.gather(*tasks)

    parsed_choices = []
    for result in filter(None, results):
        ti = result["ti"]

        # Filter out the torrent if it's too large before adding it to the choices
        if ti.total_size() > MAX_TORRENT_SIZE_BYTES:
            logger.info(
                f"Filtering out torrent '{ti.name()}' from webpage scrape due to size: {format_bytes(ti.total_size())}"
            )
            continue

        parsed_choices.append(
            {
                "index": result["index"],
                "resolution": parse_resolution_from_name(ti.name()),
                "size": format_bytes(ti.total_size()),
                "file_type": get_dominant_file_type(ti.files()),
                "name": ti.name(),
                "magnet_link": result["magnet_link"],
                "bencoded_metadata": result["bencoded_metadata"],
            }
        )

    parsed_choices.sort(key=lambda x: x["index"])
    return parsed_choices


async def fetch_metadata_from_magnet(
    magnet_link: str, progress_message: Message, context: ContextTypes.DEFAULT_TYPE
) -> Optional[lt.torrent_info]:  # type: ignore
    """
    Fetches torrent metadata from a magnet link with a UI timer.
    """
    if context.bot_data is None:
        logger.error(
            "fetch_metadata_from_magnet: context.bot_data is None. Cannot access torrent session."
        )
        return None

    cancel_timer = asyncio.Event()
    timer_task = asyncio.create_task(
        _update_fetch_timer(progress_message, 120, cancel_timer)
    )

    ses = context.bot_data["TORRENT_SESSION"]
    bencoded_metadata = await asyncio.to_thread(
        _blocking_fetch_metadata, ses, magnet_link
    )

    cancel_timer.set()
    await timer_task

    if bencoded_metadata:
        logger.info(
            "Successfully fetched metadata and reconstructed torrent_info object."
        )
        return lt.torrent_info(bencoded_metadata)  # type: ignore
    else:
        logger.warning("Metadata fetch failed or timed out.")
        error_msg = "Timed out fetching metadata from the magnet link\\. It might be inactive or poorly seeded\\."
        await safe_edit_message(
            progress_message,
            text=f"❌ *Error:* {escape_markdown(error_msg)}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return None


def _blocking_fetch_metadata(ses: lt.session, magnet_link: str) -> Optional[bytes]:  # type: ignore
    """
    Synchronous worker function to fetch torrent metadata. Intended to be run in a thread.
    """
    handle = None
    try:
        params = lt.parse_magnet_uri(magnet_link)  # type: ignore
        params.save_path = tempfile.gettempdir()
        params.upload_mode = True  # Fetch metadata without downloading data
        handle = ses.add_torrent(params)

        timeout_seconds = 30
        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout_seconds:
            if handle.status().has_metadata:
                ti = handle.torrent_file()
                return lt.bencode(lt.create_torrent(ti).generate())  # type: ignore
            time.sleep(0.5)

    except Exception as e:
        logger.error(f"An exception occurred in the metadata worker thread: {e}")
    finally:
        # Ensure the handle is removed from the session on exit
        if handle and handle.is_valid():
            ses.remove_torrent(handle)

    return None


async def _update_fetch_timer(
    progress_message: Message, timeout: int, cancel_event: asyncio.Event
):
    """Helper coroutine to update a message with an elapsed time counter."""
    start_time = time.monotonic()
    while not cancel_event.is_set():
        elapsed = int(time.monotonic() - start_time)
        if elapsed > timeout:
            break

        message_text = (
            f"⬇️ *Fetching Metadata\\.\\.\\.*\n"
            f"`Magnet Link`\n\n"
            f"*Please wait, this can be slow\\.*\n"
            f"Elapsed Time: `{elapsed}s`"
        )
        await safe_edit_message(
            progress_message, text=message_text, parse_mode=ParseMode.MARKDOWN_V2
        )

        try:
            # Wait for 1 second or until the cancel event is set
            await asyncio.wait_for(cancel_event.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass  # This is the expected behavior for the loop
