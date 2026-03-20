# telegram_bot/workflows/delete_workflow/selection.py

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from telegram import (
    Message,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from ...ui.keyboards import confirm_cancel_keyboard, single_column_keyboard
from ...utils import safe_edit_message

if TYPE_CHECKING:
    pass

from .helpers import _compose_button_label


async def _present_delete_results(
    results: str | list[str] | None,
    status_message: Message,
    media_name: str,
    query_text: str,
    context: ContextTypes.DEFAULT_TYPE,
    delete_target_kind: str,
):
    """Presents single, multiple, or no search results to the user for deletion."""
    if context.user_data is None:
        context.user_data = {}

    if isinstance(results, str):
        context.user_data["path_to_delete"] = results
        context.user_data["delete_target_kind"] = delete_target_kind
        base_name = os.path.basename(results)

        # FIX: Normalize path separators for display
        display_path = results.replace(os.sep, "/")

        message_text = (
            f"Found:\n`{escape_markdown(base_name, version=2)}`\n\n"
            f"*Path:*\n`{escape_markdown(display_path, version=2)}`\n\n"
            f"Are you sure you want to permanently delete this item\\?"
        )
        await safe_edit_message(
            status_message,
            text=message_text,
            reply_markup=confirm_cancel_keyboard(
                "✅ Yes, Delete It",
                "confirm_delete",
                cancel_label="❌ No, Cancel",
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    elif isinstance(results, list):
        context.user_data["selection_choices"] = results
        context.user_data["selection_target_kind"] = delete_target_kind
        await safe_edit_message(
            status_message,
            text="Multiple matches found\\. Which one do you want to delete\\?",
            reply_markup=single_column_keyboard(
                [
                    (_compose_button_label(path), f"delete_select_{i}")
                    for i, path in enumerate(results)
                ]
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    else:
        await safe_edit_message(
            status_message,
            text=f"❌ No {media_name} found matching: `{escape_markdown(query_text, version=2)}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
