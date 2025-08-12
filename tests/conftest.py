import sys
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from telegram import Update, Message, Chat, User, CallbackQuery

# Ensure root path is available for imports
sys.path.append(str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def user():
    return User(id=123, first_name="Test", is_bot=False)


@pytest.fixture
def chat():
    return Chat(id=456, type="private")


@pytest.fixture
def make_message(user, chat):
    def _make(text: str = "", message_id: int = 1):
        msg = Message(
            message_id=message_id,
            date=datetime.now(),
            chat=chat,
            from_user=user,
            text=text,
        )
        bot = SimpleNamespace(delete_message=AsyncMock(), edit_message_text=AsyncMock())
        msg.set_bot(bot)
        return msg

    return _make


@pytest.fixture
def make_callback_query(user, make_message):
    def _make(data: str, message: Message | None = None):
        if message is None:
            message = make_message()
        return CallbackQuery(
            id="1", from_user=user, chat_instance="1", data=data, message=message
        )

    return _make


@pytest.fixture
def make_update():
    def _make(
        message: Message | None = None,
        callback_query: CallbackQuery | None = None,
        update_id: int = 1,
    ):
        return Update(
            update_id=update_id, message=message, callback_query=callback_query
        )

    return _make


@pytest.fixture
def context(make_message):
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=make_message()),
        delete_message=AsyncMock(),
    )
    return SimpleNamespace(bot=bot, user_data={}, bot_data={})
