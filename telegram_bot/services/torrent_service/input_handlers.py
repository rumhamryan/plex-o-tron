# telegram_bot/services/torrent_service/input_handlers.py

import asyncio
import os
import re
from typing import Any, Dict, Iterable, cast
from urllib.parse import unquote

import httpx
import libtorrent as lt
from telegram import Message
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from telegram_bot.config import logger
from telegram_bot.services.media_manager import (
    get_dominant_file_type,
    parse_resolution_from_name,
)
from telegram_bot.ui.keyboards import single_column_keyboard
from telegram_bot.utils import format_bytes, parse_torrent_name, safe_edit_message

from .metadata_fetch import _blocking_fetch_metadata, fetch_metadata_from_magnet

_MAGNET_LINK_PATTERN = re.compile(r"""(?i)href=["'](magnet:\?xt=urn:btih:[^"']+)["']""")
_MAGNET_TEXT_PATTERN = re.compile(r"""(?i)magnet:\?xt=urn:btih:[^\s"'<>]+""")


async def find_magnet_link_on_page(url: str) -> list[str]:
    """
    Extracts magnet links from a user-supplied page URL.

    This is kept for manual link ingestion only. Torrent search/collection no
    longer uses direct tracker HTML scraping.
    """
    try:
        from .adapters import fetch_url

        response = await fetch_url(url, timeout=20, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("[TORRENT] Failed to fetch page for magnet extraction: %s", exc)
        return []

    candidates = [
        *_MAGNET_LINK_PATTERN.findall(response.text),
        *_MAGNET_TEXT_PATTERN.findall(response.text),
    ]
    seen: set[str] = set()
    magnet_links: list[str] = []
    for candidate in candidates:
        cleaned = unquote(candidate).strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            magnet_links.append(cleaned)
    return magnet_links


async def process_user_input(
    text: str,
    context: ContextTypes.DEFAULT_TYPE,
    progress_message: Message,
    info_url: str | None = None,
) -> lt.torrent_info | None:  # type: ignore
    """
    Analyzes user input text to acquire a torrent_info object. It handles
    magnet links, direct .torrent URLs, and webpages containing magnet links.
    """
    # --- Refactored Guard Clause: Ensure user_data exists before use ---
    if context.user_data is None:
        logger.error("process_user_input: context.user_data is None. Cannot store state.")
        return None

    if info_url:
        context.user_data["pending_info_url"] = info_url
        logger.debug(f"[TORRENT] Stored pending_info_url: {info_url}")
    else:
        # Clear any previous value if not provided
        context.user_data.pop("pending_info_url", None)

    if text.startswith("magnet:?xt=urn:btih:"):
        context.user_data["pending_magnet_link"] = text
        return await fetch_metadata_from_magnet(text, progress_message, context)

    if text.startswith(("http://", "https://")) and text.endswith(".torrent"):
        return await _handle_torrent_url(text, context, progress_message)

    if text.startswith(("http://", "https://")):
        return await _handle_webpage_url(text, context, progress_message)

    error_msg = "This does not look like a valid \\.torrent URL, magnet link, or web page\\."
    await safe_edit_message(
        progress_message,
        text=f"❌ *Error:* {escape_markdown(error_msg)}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return None


async def _handle_torrent_url(
    url: str, context: ContextTypes.DEFAULT_TYPE, progress_message: Message
) -> lt.torrent_info | None:  # type: ignore
    """Downloads a .torrent file from a URL and returns its torrent_info."""
    if context.user_data is None:
        logger.error("_handle_torrent_url: context.user_data is None. Cannot store state.")
        return None

    try:
        from .adapters import ensure_dir, fetch_url, write_bytes

        response = await fetch_url(url, timeout=30, follow_redirects=True)
        response.raise_for_status()

        torrent_content = response.content
        ti = lt.torrent_info(torrent_content)  # type: ignore

        info_hash = str(ti.info_hashes().v1)
        torrents_dir = ".torrents"
        ensure_dir(torrents_dir)
        file_path = os.path.join(torrents_dir, f"{info_hash}.torrent")
        write_bytes(file_path, torrent_content)

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
) -> lt.torrent_info | None:  # type: ignore
    """Scrapes a webpage for magnet links and proceeds accordingly."""
    if context.user_data is None:
        logger.error("_handle_webpage_url: context.user_data is None. Cannot store state.")
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
        return await fetch_metadata_from_magnet(magnet_links[0], progress_message, context)

    parsed_choices = await _fetch_and_parse_magnet_details(magnet_links, context, progress_message)

    if not parsed_choices:
        error_msg = (
            "Could not fetch details for any of the found magnet links\\. They may be inactive\\."
        )
        await safe_edit_message(
            progress_message,
            text=f"❌ *Error:* {escape_markdown(error_msg)}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return None

    prompt_message_id = context.user_data.pop("link_prompt_message_id", None)
    if isinstance(prompt_message_id, int):
        try:
            await context.bot.delete_message(
                chat_id=progress_message.chat_id,
                message_id=prompt_message_id,
            )
        except Exception:
            pass

    context.user_data["temp_magnet_choices_details"] = parsed_choices

    first_choice_name = parsed_choices[0]["name"]
    parsed_title_info = parse_torrent_name(first_choice_name)
    common_title = parsed_title_info.get("title", first_choice_name)

    header_text = f"*{escape_markdown(common_title)}*\n\n"
    subtitle_text = f"Found {len(parsed_choices)} valid torrents\\. Please select one:"

    await safe_edit_message(
        progress_message,
        text=header_text + subtitle_text,
        reply_markup=single_column_keyboard(
            [
                (
                    f"{choice['resolution']} | {choice['file_type']} | {choice['size']}",
                    f"select_magnet_{choice['index']}",
                )
                for choice in parsed_choices
            ]
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return None


async def _fetch_and_parse_magnet_details(
    magnet_links: list[str],
    context: ContextTypes.DEFAULT_TYPE,
    progress_message: Message,
) -> list[dict[str, Any]]:
    """Fetches metadata for multiple magnet links in parallel and parses their details."""
    ses = context.bot_data["TORRENT_SESSION"]
    status_text = escape_markdown(
        f"Found {len(magnet_links)} links. Fetching details...",
        version=2,
    )
    await safe_edit_message(
        progress_message,
        text=status_text,
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    async def fetch_one(magnet_link: str, index: int):
        """Worker to fetch metadata for a single magnet link."""
        bencoded_metadata = await asyncio.to_thread(_blocking_fetch_metadata, ses, magnet_link)
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
    for result in cast(Iterable[Dict[str, Any]], filter(None, results)):
        ti = result["ti"]

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
