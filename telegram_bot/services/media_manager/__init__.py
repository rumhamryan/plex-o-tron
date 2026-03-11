# telegram_bot/services/media_manager/__init__.py

from .naming import generate_plex_filename, parse_resolution_from_name
from .paths import _get_final_destination_path, _get_path_size_bytes
from .plex_scan import _trigger_plex_scan
from .processing import handle_successful_download
from .validation import get_dominant_file_type, validate_and_enrich_torrent, validate_torrent_files

__all__ = [
    "generate_plex_filename",
    "get_dominant_file_type",
    "parse_resolution_from_name",
    "validate_torrent_files",
    "validate_and_enrich_torrent",
    "handle_successful_download",
    "_get_final_destination_path",
    "_get_path_size_bytes",
    "_trigger_plex_scan",
]
