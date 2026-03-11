# telegram_bot/services/torrent_service/__init__.py

from .input_handlers import process_user_input
from .metadata_fetch import fetch_metadata_from_magnet

__all__ = [
    "process_user_input",
    "fetch_metadata_from_magnet",
]
