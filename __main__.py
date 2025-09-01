# telegram_bot/__main__.py

import re

# Ensure PTB env flags are set before importing python-telegram-bot
from telegram_bot import _ptb_env  # noqa: F401
import libtorrent as lt
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# --- Refactored Imports ---
from telegram_bot.config import get_configuration, logger
from telegram_bot.handlers.callback_handlers import button_handler
from telegram_bot.handlers.command_handlers import (
    delete_command,
    help_command,
    links_command,
    plex_restart_command,
    plex_status_command,
    search_command,
)
from telegram_bot.handlers.message_handlers import (
    handle_link_message,
    handle_search_message,
)
from telegram_bot.state import post_init, post_shutdown
from telegram_bot.handlers.error_handler import global_error_handler


def register_handlers(application: Application) -> None:
    """
    Registers all the command, message, and callback handlers for the bot.
    This keeps the main function clean and focused on initialization.
    """
    # Command Handlers are registered with a case-insensitive regex filter
    # to catch commands with or without a leading slash.
    application.add_handler(
        MessageHandler(
            filters.Regex(re.compile(r"^/?search$", re.IGNORECASE)), search_command
        )
    )
    application.add_handler(
        MessageHandler(
            filters.Regex(re.compile(r"^/?links$", re.IGNORECASE)), links_command
        )
    )
    application.add_handler(
        MessageHandler(
            filters.Regex(re.compile(r"^/?help$", re.IGNORECASE)), help_command
        )
    )
    application.add_handler(
        MessageHandler(
            filters.Regex(re.compile(r"^/?start$", re.IGNORECASE)), help_command
        )
    )  # /start redirects to help
    application.add_handler(
        MessageHandler(
            filters.Regex(re.compile(r"^/?status$", re.IGNORECASE)), plex_status_command
        )
    )
    application.add_handler(
        MessageHandler(
            filters.Regex(re.compile(r"^/?restart$", re.IGNORECASE)),
            plex_restart_command,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.Regex(re.compile(r"^/?delete$", re.IGNORECASE)), delete_command
        )
    )

    # Callback Query Handler for all button presses
    application.add_handler(CallbackQueryHandler(button_handler))

    # Message Handler specifically for magnet/http links, ensuring commands are ignored
    link_filter = filters.Regex(r"^(magnet:|https?://)")
    application.add_handler(
        MessageHandler(link_filter & ~filters.COMMAND, handle_link_message)
    )

    # General Text Handler for conversational workflows (e.g., search/delete replies)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search_message)
    )

    application.add_error_handler(global_error_handler)

    logger.info("All handlers have been registered.")


def main() -> None:
    """
    Main function to initialize and run the Telegram bot.
    """
    logger.info("Starting bot...")

    # Load configuration from config.ini.
    token, save_paths, allowed_ids, plex_config, search_config = get_configuration()

    # The Application object is the heart of the bot. We use `bot_data` to store
    # application-level state and configurations, making them accessible
    # in any handler via the context object.
    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(
            post_init
        )  # Function to run after initialization (e.g., resume downloads)
        .post_shutdown(post_shutdown)  # Function to run on graceful shutdown
        .build()
    )

    # Populate the bot's shared context data using the loaded configuration.
    # This data is accessed by handlers during normal bot operation.
    application.bot_data["SAVE_PATHS"] = save_paths
    application.bot_data["PLEX_CONFIG"] = plex_config
    application.bot_data["SEARCH_CONFIG"] = search_config
    application.bot_data["ALLOWED_USER_IDS"] = allowed_ids
    # The 'persistence_file' key is removed from here as it's no longer needed.
    application.bot_data.setdefault("active_downloads", {})
    application.bot_data.setdefault("download_queues", {})
    application.bot_data.setdefault("is_shutting_down", False)

    # Initialize a single, long-lived libtorrent session for the application.
    logger.info("Creating global libtorrent session for the application.")
    application.bot_data["TORRENT_SESSION"] = lt.session(  # type: ignore
        {
            "listen_interfaces": "0.0.0.0:6881",
            "dht_bootstrap_nodes": "router.utorrent.com:6881,router.bittorrent.com:6881,dht.transmissionbt.com:6881",
        }
    )

    # Register all handlers.
    register_handlers(application)

    logger.info("Bot startup complete. Starting polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
