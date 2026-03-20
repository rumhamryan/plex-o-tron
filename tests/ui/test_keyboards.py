from telegram_bot.ui.keyboards import (
    cancel_only_keyboard,
    confirm_cancel_keyboard,
    launcher_keyboard,
    number_grid_keyboard,
    stacked_choice_keyboard,
)


def test_launcher_keyboard_uses_shared_two_row_layout():
    markup = launcher_keyboard("Left", "left_cb", "Right", "right_cb")

    assert [[button.text for button in row] for row in markup.inline_keyboard] == [
        ["Left", "Right"],
        ["❌ Cancel"],
    ]
    assert [[button.callback_data for button in row] for row in markup.inline_keyboard] == [
        ["left_cb", "right_cb"],
        ["cancel_operation"],
    ]


def test_cancel_and_confirm_helpers_use_standard_callbacks():
    cancel_markup = cancel_only_keyboard()
    confirm_markup = confirm_cancel_keyboard("✅ Confirm", "confirm_action")

    assert cancel_markup.inline_keyboard[0][0].callback_data == "cancel_operation"
    assert [button.callback_data for button in confirm_markup.inline_keyboard[0]] == [
        "confirm_action",
        "cancel_operation",
    ]


def test_stacked_choice_keyboard_builds_vertical_menu_with_cancel():
    markup = stacked_choice_keyboard([("One", "one"), ("Two", "two")])

    assert [[button.text for button in row] for row in markup.inline_keyboard] == [
        ["One"],
        ["Two"],
        ["❌ Cancel"],
    ]


def test_number_grid_keyboard_uses_four_column_grid_and_cancel_row():
    markup = number_grid_keyboard([1, 2, 3, 4, 5], "pick_", columns=4)

    assert [[button.text for button in row] for row in markup.inline_keyboard] == [
        ["1", "2", "3", "4"],
        ["5"],
        ["❌ Cancel"],
    ]
    assert markup.inline_keyboard[1][0].callback_data == "pick_5"
