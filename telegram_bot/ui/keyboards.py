from __future__ import annotations

from collections.abc import Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

CANCEL_LABEL = "❌ Cancel"
CANCEL_CALLBACK = "cancel_operation"
KeyboardOption = tuple[str, str]


def _cancel_row(
    *,
    cancel_label: str = CANCEL_LABEL,
    cancel_callback: str = CANCEL_CALLBACK,
) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(cancel_label, callback_data=cancel_callback)]


def _single_button_rows(options: Sequence[KeyboardOption]) -> list[list[InlineKeyboardButton]]:
    return [[InlineKeyboardButton(label, callback_data=callback)] for label, callback in options]


def cancel_only_keyboard() -> InlineKeyboardMarkup:
    """Returns the shared single-button escape hatch used by text-entry prompts."""
    return InlineKeyboardMarkup([_cancel_row()])


def launcher_keyboard(
    left_label: str,
    left_callback: str,
    right_label: str,
    right_callback: str,
) -> InlineKeyboardMarkup:
    """Returns the standard two-choice launcher layout plus a trailing cancel row."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(left_label, callback_data=left_callback),
                InlineKeyboardButton(right_label, callback_data=right_callback),
            ],
            _cancel_row(),
        ]
    )


def stacked_choice_keyboard(
    options: Sequence[KeyboardOption],
    *,
    include_cancel: bool = True,
) -> InlineKeyboardMarkup:
    """Returns a vertical stack of choices for scope or mode selection prompts."""
    return single_column_keyboard(options, include_cancel=include_cancel)


def confirm_cancel_keyboard(
    confirm_label: str,
    confirm_callback: str,
    *,
    cancel_label: str = CANCEL_LABEL,
    cancel_callback: str = CANCEL_CALLBACK,
) -> InlineKeyboardMarkup:
    """Returns the standard binary action row used by confirm/cancel prompts."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(confirm_label, callback_data=confirm_callback),
                InlineKeyboardButton(cancel_label, callback_data=cancel_callback),
            ]
        ]
    )


def single_column_keyboard(
    options: Sequence[KeyboardOption],
    *,
    include_cancel: bool = True,
) -> InlineKeyboardMarkup:
    """Returns a single-column button list, optionally followed by a cancel row."""
    rows = _single_button_rows(options)
    if include_cancel:
        rows.append(_cancel_row())
    if not rows:
        raise ValueError(
            "single_column_keyboard requires at least one option or include_cancel=True"
        )
    return InlineKeyboardMarkup(rows)


def number_grid_keyboard(
    values: Sequence[int],
    callback_prefix: str,
    *,
    columns: int = 4,
    include_cancel: bool = True,
) -> InlineKeyboardMarkup:
    """Returns a compact numeric grid with a trailing cancel row by default."""
    if columns <= 0:
        raise ValueError("columns must be greater than zero")

    buttons = [
        InlineKeyboardButton(str(value), callback_data=f"{callback_prefix}{value}")
        for value in values
    ]
    rows = [buttons[index : index + columns] for index in range(0, len(buttons), columns)]
    if include_cancel:
        rows.append(_cancel_row())
    if not rows:
        raise ValueError("number_grid_keyboard requires at least one value or include_cancel=True")
    return InlineKeyboardMarkup(rows)
