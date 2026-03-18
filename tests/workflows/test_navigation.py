from telegram_bot.workflows.navigation import (
    clear_chat_navigation_state,
    get_active_prompt_message_id,
    get_chat_navigation_state,
    mark_chat_idle,
    mark_chat_workflow_active,
)


def test_navigation_record_created_on_first_access(context):
    state = get_chat_navigation_state(context, 456)

    assert state["state"] == "idle"
    assert state["home_menu_message_id"] is None
    assert state["active_prompt_message_id"] is None
    assert context.bot_data["chat_navigation"][456]["state"] == "idle"


def test_idle_to_workflow_transition_overwrites_prior_state_cleanly(context):
    mark_chat_idle(context, 456, home_menu_message_id=12)

    state = mark_chat_workflow_active(context, 456, "search", prompt_message_id=34)

    assert state["state"] == "search"
    assert state["home_menu_message_id"] == 12
    assert state["active_prompt_message_id"] == 34
    assert context.user_data["active_workflow"] == "search"
    assert get_active_prompt_message_id(context, 456) == 34


def test_workflow_to_idle_transition_overwrites_prior_state_cleanly(context):
    mark_chat_workflow_active(context, 456, "delete", prompt_message_id=44)

    state = mark_chat_idle(context, 456, home_menu_message_id=55)

    assert state["state"] == "idle"
    assert state["home_menu_message_id"] == 55
    assert state["active_prompt_message_id"] is None
    assert "active_workflow" not in context.user_data


def test_clearing_navigation_state_removes_chat_record(context):
    mark_chat_workflow_active(context, 456, "link", prompt_message_id=66)

    clear_chat_navigation_state(context, 456)

    assert 456 not in context.bot_data["chat_navigation"]


def test_navigation_state_isolated_per_chat(context):
    mark_chat_workflow_active(context, 111, "search", prompt_message_id=1)
    mark_chat_idle(context, 222, home_menu_message_id=2)

    assert context.bot_data["chat_navigation"][111]["state"] == "search"
    assert context.bot_data["chat_navigation"][222]["state"] == "idle"
    assert context.bot_data["chat_navigation"][111]["home_menu_message_id"] is None
    assert context.bot_data["chat_navigation"][222]["home_menu_message_id"] == 2
