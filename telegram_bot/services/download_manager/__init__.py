# telegram_bot/services/download_manager/__init__.py

from telegram_bot.services.media_manager import _trigger_plex_scan, handle_successful_download
from telegram_bot.services.plex_service import (
    ensure_collection_contains_movies,
    wait_for_movies_to_be_available,
)
from telegram_bot.state import save_state
from telegram_bot.utils import safe_edit_message, sanitize_collection_name
from telegram_bot.workflows import finalize_movie_collection

from .controls import handle_cancel_all, handle_cancel_request, handle_pause_resume
from .download_core import download_with_progress
from .lifecycle import (
    _finalize_download,
    _requeue_download,
    _update_batch_and_maybe_scan,
    cleanup_download_resources,
    download_task_wrapper,
)
from .progress import ProgressReporter
from .queue import (
    _finalize_owned_collection_batch,
    _start_download_task,
    add_collection_to_queue,
    add_download_to_queue,
    add_season_to_queue,
    process_queue_for_user,
    queue_download_source,
)

__all__ = [
    "ProgressReporter",
    "download_with_progress",
    "download_task_wrapper",
    "_update_batch_and_maybe_scan",
    "_requeue_download",
    "_finalize_download",
    "cleanup_download_resources",
    "process_queue_for_user",
    "_start_download_task",
    "queue_download_source",
    "add_download_to_queue",
    "add_season_to_queue",
    "add_collection_to_queue",
    "_finalize_owned_collection_batch",
    "handle_pause_resume",
    "handle_cancel_request",
    "handle_cancel_all",
    "safe_edit_message",
    "save_state",
    "handle_successful_download",
    "_trigger_plex_scan",
    "ensure_collection_contains_movies",
    "wait_for_movies_to_be_available",
    "finalize_movie_collection",
    "sanitize_collection_name",
]
