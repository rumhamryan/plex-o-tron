# telegram_bot/ui/messages.py

from __future__ import annotations

from telegram.helpers import escape_markdown


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
    title_icon: str | None = None,
    size_icon: str | None = None,
    destination_icon: str | None = None,
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
        lines.append(
            _compose_line(destination_icon, f"Destination: `{escaped_destination}`")
        )

    return "\n".join(lines)
