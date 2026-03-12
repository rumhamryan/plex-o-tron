# telegram_bot/services/media_manager/adapters.py

from __future__ import annotations

import os
import shutil


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def move_file(source: str, destination: str) -> None:
    shutil.move(source, destination)


def get_path_size_bytes(path: str) -> int:
    return os.path.getsize(path)


def get_disk_usage(path: str) -> tuple[int, int, int]:
    usage = shutil.disk_usage(path)
    return usage.total, usage.used, usage.free
