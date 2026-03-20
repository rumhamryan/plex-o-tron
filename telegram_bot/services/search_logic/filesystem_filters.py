from __future__ import annotations

import os

IGNORED_SEARCH_DIRECTORY_NAMES = {
    ".trash",
    ".trashes",
    "$recycle.bin",
    "system volume information",
}
IGNORED_SEARCH_DIRECTORY_PREFIXES = (".trash-",)
IGNORED_SEARCH_FILE_SUFFIXES = (".trashinfo",)


def is_ignored_search_directory(path: str) -> bool:
    normalized_path = os.path.abspath(path)
    path_parts = [part.casefold() for part in normalized_path.split(os.sep) if part]
    for part in path_parts:
        if part in IGNORED_SEARCH_DIRECTORY_NAMES:
            return True
        if any(part.startswith(prefix) for prefix in IGNORED_SEARCH_DIRECTORY_PREFIXES):
            return True
    return False


def is_ignored_search_file(path: str) -> bool:
    basename = os.path.basename(path).casefold()
    return any(basename.endswith(suffix) for suffix in IGNORED_SEARCH_FILE_SUFFIXES)
