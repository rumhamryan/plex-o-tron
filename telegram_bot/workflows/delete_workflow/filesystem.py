# telegram_bot/workflows/delete_workflow/filesystem.py

from __future__ import annotations

import asyncio
import os
import shutil
from typing import TYPE_CHECKING

from ...config import logger

if TYPE_CHECKING:
    pass


async def _delete_from_filesystem(path: str) -> tuple[bool, str]:
    """Remove a file or directory from disk."""

    def _remove() -> str:
        if os.path.isfile(path):
            os.remove(path)
            return "file"
        if os.path.isdir(path):
            shutil.rmtree(path)
            return "directory"
        raise FileNotFoundError(path)

    try:
        removed_kind = await asyncio.to_thread(_remove)
        logger.info("Removed %s from filesystem: %s", removed_kind, path)
        return True, removed_kind
    except FileNotFoundError:
        logger.warning("Filesystem deletion skipped because the path no longer exists: %s", path)
        return False, "missing"
    except Exception as exc:
        logger.error("Filesystem deletion failed for '%s': %s", path, exc, exc_info=True)
        return False, str(exc)
