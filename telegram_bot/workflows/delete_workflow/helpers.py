# telegram_bot/workflows/delete_workflow/helpers.py

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from telegram.helpers import escape_markdown

from ...config import logger
from ...utils import format_bytes

if TYPE_CHECKING:
    pass

BUTTON_LABEL_MAX_LEN = 48
UNKNOWN_SIZE_LABEL = "Unknown size"


def _get_display_name(path: str) -> str:
    """Return a stable display name for a file/folder path."""
    normalized = path.rstrip(os.sep)
    base_name = os.path.basename(normalized)
    return base_name or normalized


def _format_size_label(path: str) -> str:
    """Return a short size label (in GiB) for display purposes."""
    try:
        size_bytes = _calculate_path_size(path)
    except OSError as exc:
        logger.info("Unable to determine file size for '%s': %s", path, exc)
        return UNKNOWN_SIZE_LABEL

    if size_bytes <= 0:
        return "0 GiB"

    size_in_gib = round(size_bytes / (1024**3), 2)
    if size_in_gib == 0:
        return format_bytes(size_bytes)
    return f"{size_in_gib} GiB"


def _calculate_path_size(path: str) -> int:
    """Return the total size for files or directories."""
    target = Path(path)
    if not target.exists():
        raise OSError(f"Path does not exist: {path}")

    if target.is_file():
        return target.stat().st_size

    if target.is_dir():
        total = 0
        stack = [target]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as iterator:
                    for entry in iterator:
                        try:
                            if entry.is_file(follow_symlinks=False):
                                total += entry.stat(follow_symlinks=False).st_size
                            elif entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                        except OSError as entry_exc:
                            logger.info(
                                "Skipping unreadable entry '%s': %s",
                                entry.path,
                                entry_exc,
                            )
            except OSError as dir_exc:
                logger.info(
                    "Unable to scan directory '%s': %s",
                    current,
                    dir_exc,
                    exc_info=False,
                )
        return total

    return 0


def _compose_button_label(path: str) -> str:
    base_name = _get_display_name(path)
    size_label = _format_size_label(path)
    candidate = f"{base_name} | {size_label}"
    if len(candidate) <= BUTTON_LABEL_MAX_LEN:
        return candidate

    fallback = f"{base_name} | {UNKNOWN_SIZE_LABEL}"
    if len(fallback) <= BUTTON_LABEL_MAX_LEN:
        return fallback

    suffix = f" | {UNKNOWN_SIZE_LABEL}"
    available = BUTTON_LABEL_MAX_LEN - len(suffix)
    if available <= 1:
        truncated_name = base_name[: max(0, available)]
        return f"{truncated_name}{suffix}"

    truncated_name = base_name[: available - 1] + "…"
    return f"{truncated_name}{suffix}"


def _has_name_twin(path: str) -> bool:
    """Detect whether another file in the same directory shares the same stem."""
    target = Path(path)
    if not target.exists() or not target.is_file():
        return False

    normalized = target.stem.casefold()
    try:
        for sibling in target.parent.iterdir():
            if sibling == target or not sibling.is_file():
                continue
            if sibling.stem.casefold() == normalized:
                logger.info(
                    "Detected name twin for '%s' inside '%s'.",
                    target.name,
                    target.parent,
                )
                return True
    except FileNotFoundError:
        # Directory disappeared mid-check; treat as no twin.
        return False
    except OSError as exc:
        logger.error(
            "Unable to inspect sibling files for '%s': %s",
            target.name,
            exc,
            exc_info=True,
        )
        raise RuntimeError(
            f"Unable to inspect sibling files for '{target.name}'. Please try again."
        ) from exc

    return False


def _has_valid_plex_credentials(plex_config: dict | None) -> bool:
    """Detect whether Plex credentials look usable."""
    if not plex_config:
        return False
    token = (plex_config.get("token") or "").strip()
    return bool(token) and token.upper() != "PLEX_TOKEN"


def _format_manual_delete_failure(detail: str) -> str:
    """Build a Markdown-ready failure string for manual deletions."""
    if detail == "missing":
        return "? *Deletion Failed*\nThe path no longer exists on the server\\."
    return "? *Manual Deletion Failed*\n`{}`".format(
        escape_markdown(detail or "Unknown error", version=2)
    )
