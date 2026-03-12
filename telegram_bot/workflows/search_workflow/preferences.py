from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.constants import ParseMode

from ...utils import safe_edit_message

PreferenceOption = tuple[str, str]


def _build_preference_row(
    options: tuple[PreferenceOption, ...],
    selected_value: str | None,
    callback_prefix: str,
) -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    normalized = (selected_value or "").lower()
    for value, label in options:
        prefix = "🟢" if normalized == value.lower() else ""
        row.append(
            InlineKeyboardButton(
                f"{prefix}{label}",
                callback_data=f"{callback_prefix}{value}",
            )
        )
    return row


async def _render_search_preferences_prompt(
    message: Message,
    *,
    text: str,
    selected_resolution: str | None,
    resolution_options: tuple[PreferenceOption, ...],
    resolution_callback_prefix: str,
    selected_codec: str | None,
    codec_options: tuple[PreferenceOption, ...],
    codec_callback_prefix: str,
    continue_callback_data: str,
    continue_label: str = "Continue",
) -> None:
    keyboard = InlineKeyboardMarkup(
        [
            _build_preference_row(
                resolution_options,
                selected_resolution,
                resolution_callback_prefix,
            ),
            _build_preference_row(
                codec_options,
                selected_codec,
                codec_callback_prefix,
            ),
            [InlineKeyboardButton(f"➡️ {continue_label}", callback_data=continue_callback_data)],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
        ]
    )
    await safe_edit_message(
        message,
        text=text,
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
