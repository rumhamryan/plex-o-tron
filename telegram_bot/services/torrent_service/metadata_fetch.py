# telegram_bot/services/torrent_service/metadata_fetch.py

import asyncio
import tempfile
import time

import libtorrent as lt
from telegram import Message
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from telegram_bot.config import logger
from telegram_bot.services.interfaces import TorrentSession
from telegram_bot.utils import safe_edit_message


async def fetch_metadata_from_magnet(
    magnet_link: str, progress_message: Message, context: ContextTypes.DEFAULT_TYPE
) -> lt.torrent_info | None:  # type: ignore
    """
    Fetches torrent metadata from a magnet link with a UI timer.
    """
    if context.bot_data is None:
        logger.error(
            "fetch_metadata_from_magnet: context.bot_data is None. Cannot access torrent session."
        )
        return None

    cancel_timer = asyncio.Event()
    timer_task = asyncio.create_task(_update_fetch_timer(progress_message, 120, cancel_timer))

    ses = context.bot_data["TORRENT_SESSION"]
    bencoded_metadata = await asyncio.to_thread(_blocking_fetch_metadata, ses, magnet_link)

    cancel_timer.set()
    await timer_task

    if bencoded_metadata:
        logger.info("Successfully fetched metadata and reconstructed torrent_info object.")
        return lt.torrent_info(bencoded_metadata)  # type: ignore

    logger.warning("Metadata fetch failed or timed out.")
    error_msg = "Timed out fetching metadata from the magnet link\\. It might be inactive or poorly seeded\\."
    await safe_edit_message(
        progress_message,
        text=f"❌ *Error:* {escape_markdown(error_msg)}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return None


def _blocking_fetch_metadata(ses: TorrentSession, magnet_link: str) -> bytes | None:  # type: ignore
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
) -> None:
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
        except TimeoutError:
            pass  # This is the expected behavior for the loop
