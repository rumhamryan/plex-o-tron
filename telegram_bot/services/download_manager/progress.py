# telegram_bot/services/download_manager/progress.py

import time
from typing import Any

import libtorrent as lt
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import NetworkError, TimedOut
from telegram.ext import Application
from telegram.helpers import escape_markdown

from telegram_bot.config import logger
from telegram_bot.domain.types import DownloadData
from telegram_bot.ui.messages import BTN_CANCEL, BTN_PAUSE, BTN_RESUME, BTN_STOP_ALL

from .bot_data_access import get_download_queues


class ProgressReporter:
    """A class to handle reporting download progress to the user via Telegram."""

    def __init__(
        self,
        application: Application,
        chat_id: int,
        message_id: int,
        parsed_info: dict[str, Any],
        clean_name: str,
        download_data: DownloadData,
    ):
        self.application = application
        self.chat_id = chat_id
        self.message_id = message_id
        self.parsed_info = parsed_info
        self.clean_name = clean_name
        self.download_data = download_data
        self.last_update_time: float = 0

    async def report(self, status: lt.torrent_status) -> None:  # type: ignore
        """Formats and sends a progress update message."""
        from . import safe_edit_message

        async with self.download_data["lock"]:
            if self.download_data.get("cancellation_pending"):
                return

            current_time = time.monotonic()
            if current_time - self.last_update_time < 2:  # Reduced from 5 for more frequent updates
                return
            self.last_update_time = current_time

            progress_percent = status.progress * 100
            is_paused = self.download_data.get("is_paused", False)
            # When a download is paused libtorrent may still report the last
            # observed download rate. We override the speed to reflect the
            # effective transfer rate during a pause.
            speed_mbps = 0.0 if is_paused else status.download_rate / 1024 / 1024

            # --- CORRECTED STRING PREPARATION WITH version=2 ---
            progress_str = escape_markdown(f"{progress_percent:.2f}", version=2)
            speed_str = escape_markdown(f"{speed_mbps:.2f}", version=2)

            # --- Build the message content ---
            if self.parsed_info.get("type") == "tv":
                s = int(self.parsed_info.get("season", 0) or 0)
                title_str = escape_markdown(self.parsed_info.get("title", ""), version=2)

                # Season pack handling: show single-line "<Title> Season 01"
                if self.parsed_info.get("is_season_pack"):
                    name_str = f"`{title_str} Season {s:02d}`"
                else:
                    e = int(self.parsed_info.get("episode", 0) or 0)
                    ep_title = self.parsed_info.get("episode_title", "")
                    episode_details_str = escape_markdown(
                        f"S{s:02d}E{e:02d} - {ep_title}", version=2
                    )
                    name_str = f"`{title_str}`\n`{episode_details_str}`"
            else:
                name_str = f"`{escape_markdown(self.clean_name, version=2)}`"

            header_str = "⏸️ *Paused:*" if is_paused else "⬇️ *Downloading:*"
            state_str = "*paused*" if is_paused else escape_markdown(status.state.name, version=2)

            message_text = (
                f"{header_str}\n{name_str}\n"
                f"*Progress:* {progress_str}%\n"
                f"*State:* {state_str}\n"
                f"*Peers:* {status.num_peers}\n"
                f"*Speed:* {speed_str} MB/s"
            )

            # Use a single toggle button for both pause and resume actions.
            # Build control row and conditionally add "Stop" if there is a queue
            controls_row = []
            if is_paused:
                controls_row.append(InlineKeyboardButton(BTN_RESUME, callback_data="pause_resume"))
            else:
                controls_row.append(InlineKeyboardButton(BTN_PAUSE, callback_data="pause_resume"))
            controls_row.append(InlineKeyboardButton(BTN_CANCEL, callback_data="cancel_download"))

            # If user has at least 1 queued download, expose "Cancel All"
            try:
                dq = get_download_queues(self.application.bot_data)
                if dq.get(str(self.chat_id)):
                    controls_row.append(
                        InlineKeyboardButton(BTN_STOP_ALL, callback_data="cancel_all")
                    )
            except Exception:
                pass

            reply_markup = InlineKeyboardMarkup([controls_row])

            # --- FIX: Add a try/except block to prevent UI errors from crashing the download ---
            try:
                await safe_edit_message(
                    self.application.bot,
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=message_text,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=reply_markup,
                )
            except (TimedOut, NetworkError) as e:
                logger.warning(
                    f"Failed to send progress update due to a network error: {e}. "
                    "The download will continue in the background."
                )
