# telegram_bot/state.py

import asyncio
import json
import os
from typing import Dict, Tuple

from telegram.ext import Application

# --- Corrected Import: Import the constant directly ---
from .config import logger, PERSISTENCE_FILE


def save_state(file_path: str, active_downloads: Dict, download_queues: Dict) -> None:
    """Saves the state of active and queued downloads to a JSON file."""
    # Create a serializable copy of the active downloads, removing non-serializable objects
    serializable_active = {}
    for chat_id, download_data in active_downloads.items():
        # Only persist downloads that are not being cancelled for shutdown
        if not download_data.get("requeued"):
            data_copy = download_data.copy()
            # These objects cannot be serialized to JSON
            data_copy.pop("task", None)
            data_copy.pop("lock", None)
            data_copy.pop("handle", None)
            serializable_active[chat_id] = data_copy

    data_to_save = {
        "active_downloads": serializable_active,
        "download_queues": download_queues,
    }

    try:
        with open(file_path, "w") as f:
            json.dump(data_to_save, f, indent=4)

        queued_count = sum(len(q) for q in download_queues.values())
        logger.info(
            f"Saved state: {len(serializable_active)} active, {queued_count} queued downloads."
        )
    except Exception as e:
        logger.error(f"Could not save persistence file to '{file_path}': {e}")


def load_state(file_path: str) -> Tuple[Dict, Dict]:
    """Loads the state of active and queued downloads from a JSON file."""
    if not os.path.exists(file_path):
        logger.info(
            f"Persistence file '{file_path}' not found. Starting with a fresh state."
        )
        return {}, {}

    try:
        with open(file_path, "r") as f:
            data = json.load(f)
            active = data.get("active_downloads", {})
            queued = data.get("download_queues", {})

            queued_count = sum(len(q) for q in queued.values())
            logger.info(
                f"Loaded state: {len(active)} active, {queued_count} queued downloads."
            )
            return active, queued
    except (json.JSONDecodeError, IOError) as e:
        logger.error(
            f"Could not read or parse persistence file '{file_path}': {e}. Starting fresh."
        )
        return {}, {}


async def post_init(application: Application) -> None:
    """
    Resumes any active downloads after the bot has been initialized.
    This function is called by the ApplicationBuilder.
    """
    from .services.download_manager import (
        download_task_wrapper,
    )  # Avoid circular import

    logger.info("--- Loading persisted state and resuming downloads ---")
    # --- Fix: Use the imported constant directly ---
    persistence_file = PERSISTENCE_FILE

    active_downloads, download_queues = load_state(persistence_file)

    application.bot_data["active_downloads"] = active_downloads
    application.bot_data["download_queues"] = download_queues

    if not active_downloads:
        logger.info("No active downloads to resume.")
        return

    for chat_id_str, download_data in active_downloads.items():
        logger.info(f"Resuming download for chat_id {chat_id_str}...")
        # Re-create the non-serializable parts and restart the task
        download_data["lock"] = asyncio.Lock()
        task = asyncio.create_task(download_task_wrapper(download_data, application))
        download_data["task"] = task

    logger.info("--- Resume process finished ---")


async def post_shutdown(application: Application) -> None:
    """
    Gracefully signals active download tasks to stop before the bot shuts down.
    This function is called by the ApplicationBuilder.
    """
    logger.info("--- Shutting down: Signalling active tasks to stop ---")

    # Set a global flag to indicate shutdown is in progress
    application.bot_data["is_shutting_down"] = True

    active_downloads = application.bot_data.get("active_downloads", {})

    tasks_to_cancel = [
        download_data["task"]
        for download_data in active_downloads.values()
        if "task" in download_data and not download_data["task"].done()
    ]

    if not tasks_to_cancel:
        logger.info("No active tasks to stop.")
    else:
        logger.info(f"Cancelling {len(tasks_to_cancel)} active download tasks...")
        for task in tasks_to_cancel:
            task.cancel()

        # Wait for all tasks to acknowledge cancellation
        await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

    # Final state save before exiting
    # --- Fix: Use the imported constant directly ---
    save_state(
        PERSISTENCE_FILE,
        application.bot_data.get("active_downloads", {}),
        application.bot_data.get("download_queues", {}),
    )

    logger.info("--- All active tasks stopped. Shutdown complete. ---")
