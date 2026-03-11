# telegram_bot/services/download_manager/controls.py

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from telegram_bot.config import PERSISTENCE_FILE, logger
from telegram_bot.ui.messages import (
    BTN_CONFIRM_CANCEL,
    BTN_CONFIRM_CANCEL_ALL,
    BTN_DENY_CANCEL,
    BTN_DENY_CANCEL_ALL,
    MSG_CANCEL_ALL_DONE,
    MSG_CONFIRM_CANCEL,
    MSG_CONFIRM_CANCEL_ALL,
    MSG_NO_ACTIVE_DOWNLOAD_CANCEL,
    MSG_NO_ACTIVE_DOWNLOAD_PAUSE_RESUME,
)

from .bot_data_access import get_active_downloads, get_download_queues


async def handle_pause_resume(update, context):
    """Toggle pause or resume for the current download."""
    query = update.callback_query
    chat_id_str = str(query.message.chat_id)
    active_downloads = get_active_downloads(context.bot_data)

    if chat_id_str not in active_downloads:
        from . import safe_edit_message

        await safe_edit_message(
            query.message,
            text=MSG_NO_ACTIVE_DOWNLOAD_PAUSE_RESUME,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    download_data = active_downloads[chat_id_str]
    async with download_data["lock"]:
        handle = download_data.get("handle")
        if handle is None:
            logger.warning(
                "Pause/resume requested but no torrent handle found for user %s.",
                chat_id_str,
            )
            return

        # Use our tracked flag instead of libtorrent flags for compatibility
        is_paused = bool(download_data.get("is_paused"))
        if is_paused:
            handle.resume()
            download_data["is_paused"] = False
            logger.info(f"Resume request processed for user {chat_id_str}.")
        else:
            handle.pause()
            download_data["is_paused"] = True
            logger.info(f"Pause request processed for user {chat_id_str}.")


async def handle_cancel_request(update, context):
    """Handles a user's request to cancel a download."""
    from . import safe_edit_message

    query = update.callback_query
    chat_id_str = str(query.message.chat_id)
    active_downloads = get_active_downloads(context.bot_data)

    if chat_id_str not in active_downloads:
        await safe_edit_message(
            query.message,
            text=MSG_NO_ACTIVE_DOWNLOAD_CANCEL,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    download_data = active_downloads[chat_id_str]
    async with download_data["lock"]:
        if query.data == "cancel_download":
            # Mark this download so progress updates pause during confirmation.
            download_data["cancellation_pending"] = True
            message_text = MSG_CONFIRM_CANCEL
            reply_markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(BTN_CONFIRM_CANCEL, callback_data="cancel_confirm"),
                        InlineKeyboardButton(BTN_DENY_CANCEL, callback_data="cancel_deny"),
                    ]
                ]
            )
            await safe_edit_message(
                query.message,
                text=message_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=reply_markup,
            )

        elif query.data == "cancel_confirm":
            # User confirmed cancellation; clear flag and stop the task.
            download_data.pop("cancellation_pending", None)
            logger.info(f"Cancellation confirmed for user {chat_id_str}.")
            if "task" in download_data and not download_data["task"].done():
                download_data["task"].cancel()

        elif query.data == "cancel_deny":
            # User opted not to cancel; remove the flag so updates resume.
            download_data.pop("cancellation_pending", None)


async def handle_cancel_all(update, context):
    """Two-step cancel-all: confirm, then clear queue and cancel active."""
    from . import safe_edit_message, save_state

    query = update.callback_query
    chat_id = query.message.chat_id
    chat_id_str = str(chat_id)

    action = query.data or ""
    active_downloads = get_active_downloads(context.bot_data)
    download_queues = get_download_queues(context.bot_data)

    # When initiating, set pending flag and ask for confirmation
    if action == "cancel_all":
        if chat_id_str in active_downloads:
            dd = active_downloads[chat_id_str]
            async with dd["lock"]:
                dd["cancellation_pending"] = True

        message_text = MSG_CONFIRM_CANCEL_ALL
        reply_markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        BTN_CONFIRM_CANCEL_ALL, callback_data="cancel_all_confirm"
                    ),
                    InlineKeyboardButton(BTN_DENY_CANCEL_ALL, callback_data="cancel_all_deny"),
                ]
            ]
        )
        await safe_edit_message(
            query.message,
            text=message_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup,
        )
        return

    # Confirm: clear queue and cancel active
    if action == "cancel_all_confirm":
        # Clear all queued downloads for this user
        if chat_id_str in download_queues:
            removed = len(download_queues.get(chat_id_str, []))
            del download_queues[chat_id_str]
            logger.info(f"Cleared {removed} queued downloads for user {chat_id_str}.")

        # Cancel the active download task if present
        if chat_id_str in active_downloads:
            dd = active_downloads[chat_id_str]
            async with dd["lock"]:
                dd.pop("cancellation_pending", None)
                task = dd.get("task")
                if task and not task.done():
                    logger.info(f"Cancelling active download for user {chat_id_str}.")
                    task.cancel()

        # Persist state after clearing the queue
        save_state(PERSISTENCE_FILE, active_downloads, download_queues)

        # Acknowledge; the active task will finalize its own message text
        try:
            await safe_edit_message(
                query.message,
                text=MSG_CANCEL_ALL_DONE,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=None,
            )
        except Exception:
            pass
        return

    # Deny: remove pending flag and resume updates
    if action == "cancel_all_deny":
        if chat_id_str in active_downloads:
            dd = active_downloads[chat_id_str]
            async with dd["lock"]:
                dd.pop("cancellation_pending", None)
        # No immediate re-render; progress updates will resume naturally
        return
