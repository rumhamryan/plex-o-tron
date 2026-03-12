# telegram_bot/ui/messages.py

from __future__ import annotations

from telegram.helpers import escape_markdown

BTN_PAUSE = "⏸️ Pause"
BTN_RESUME = "▶️ Resume"
BTN_CANCEL = "⏹️ Cancel"
BTN_CANCEL_DOWNLOAD = "⏹️ Cancel Download"
BTN_STOP_ALL = "🛑 Stop"
BTN_CONFIRM_CANCEL = "✅ Yes, Cancel"
BTN_DENY_CANCEL = "❌ No, Continue"
BTN_CONFIRM_CANCEL_ALL = "✅ Yes, Cancel All"
BTN_DENY_CANCEL_ALL = "❌ No, Continue"

MSG_NO_ACTIVE_DOWNLOAD_PAUSE_RESUME = "ℹ️ Could not find an active download to pause or resume\\."
MSG_NO_ACTIVE_DOWNLOAD_CANCEL = "ℹ️ Could not find an active download to cancel\\."
MSG_CONFIRM_CANCEL = "Are you sure you want to cancel this download\\?"
MSG_CONFIRM_CANCEL_ALL = (
    "Are you sure you want to cancel the current download and clear all queued downloads?"
)
MSG_CANCEL_ALL_DONE = "⏹️ Cancelled all downloads for this chat\\."

MSG_ACTION_EXPIRED_SEND_LINK = "This action has expired\\. Please send the link again\\."
MSG_ACTION_EXPIRED_START_OVER = "This action has expired\\. Please start over\\."
MSG_ACTION_EXPIRED_RESTART_COLLECTION = (
    "This action has expired\\. Please restart the collection workflow\\."
)
MSG_NO_MOVIES_SELECTED = "No movies were selected for download\\. Please try again\\."
MSG_DOWNLOAD_NEXT_IN_LINE = "✅ Your download is next in line and will begin shortly\\."
MSG_STARTING_DOWNLOAD = "▶️ Your download is now starting\\.\\.\\."


def format_download_queue_position(position: int) -> str:
    return f"✅ Download queued\\. You are position \\#{position} in line\\."


def format_season_queue_added(count: int) -> str:
    return f"✅ Success\\! Added {count} episodes to your download queue\\."


def format_collection_queue_added(count: int) -> str:
    plural = "s" if count != 1 else ""
    return f"✅ Added {count} movie{plural} from this collection to your queue\\."


__all__ = [
    "BTN_PAUSE",
    "BTN_RESUME",
    "BTN_CANCEL",
    "BTN_CANCEL_DOWNLOAD",
    "BTN_STOP_ALL",
    "BTN_CONFIRM_CANCEL",
    "BTN_DENY_CANCEL",
    "BTN_CONFIRM_CANCEL_ALL",
    "BTN_DENY_CANCEL_ALL",
    "MSG_NO_ACTIVE_DOWNLOAD_PAUSE_RESUME",
    "MSG_NO_ACTIVE_DOWNLOAD_CANCEL",
    "MSG_CONFIRM_CANCEL",
    "MSG_CONFIRM_CANCEL_ALL",
    "MSG_CANCEL_ALL_DONE",
    "MSG_ACTION_EXPIRED_SEND_LINK",
    "MSG_ACTION_EXPIRED_START_OVER",
    "MSG_ACTION_EXPIRED_RESTART_COLLECTION",
    "MSG_NO_MOVIES_SELECTED",
    "MSG_DOWNLOAD_NEXT_IN_LINE",
    "MSG_STARTING_DOWNLOAD",
    "format_download_queue_position",
    "format_season_queue_added",
    "format_collection_queue_added",
    "format_media_summary",
]


def _compose_line(icon: str | None, text: str) -> str:
    """Prefix text with an icon if provided."""
    if icon:
        return f"{icon} {text}"
    return text


def format_media_summary(
    *,
    prefix: str,
    title: str,
    size_label: str | None = None,
    destination_label: str | None = None,
    disk_usage_percent: int | None = None,
    highlight_disk_usage: bool = False,
    title_icon: str | None = None,
    size_icon: str | None = None,
    destination_icon: str | None = None,
    disk_usage_icon: str | None = None,
) -> str:
    """
    Builds a MarkdownV2-safe summary block describing a media item.

    Parameters
    ----------
    prefix:
        The leading line (caller is responsible for emoji / emphasis formatting).
    title:
        Display title for the media item.
    size_label:
        Optional human-readable size string.
    destination_label:
        Optional label or filesystem path describing where the media lives.
    disk_usage_percent:
        Optional filesystem usage percentage for the destination drive.
    highlight_disk_usage:
        Whether to emphasize the disk usage line.
    *_icon:
        Optional emoji to prepend to each line for additional context.
    """

    escaped_title = escape_markdown(title, version=2)
    lines: list[str] = [
        prefix,
        _compose_line(title_icon, escaped_title),
    ]

    if size_label:
        escaped_size = escape_markdown(size_label, version=2)
        lines.append(_compose_line(size_icon, f"Size: {escaped_size}"))

    if destination_label:
        escaped_destination = escape_markdown(destination_label, version=2)
        lines.append(_compose_line(destination_icon, f"Destination: `{escaped_destination}`"))

    if disk_usage_percent is not None:
        percent_text = f"{int(disk_usage_percent)}%"
        if highlight_disk_usage:
            rendered_percent = f"*{percent_text}*"
        else:
            rendered_percent = escape_markdown(percent_text, version=2)
        lines.append(_compose_line(disk_usage_icon, f"Disk Usage: {rendered_percent}"))

    return "\n".join(lines)
