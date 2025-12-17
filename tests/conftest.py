import os
import shutil
import sys
import uuid
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

# Set PTB timedelta before importing telegram types; keep imports at top via noqa
os.environ.setdefault("PTB_TIMEDELTA", "1")
from telegram import Update, Message, Chat, User, CallbackQuery, Bot  # noqa: E402

# Ensure root path is available for imports
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REPO_TMP_ROOT = Path(__file__).resolve().parent / "_tmp"


def _ensure_repo_tmp_root() -> Path:
    REPO_TMP_ROOT.mkdir(exist_ok=True)
    return REPO_TMP_ROOT


class RepoTmpPathFactory:
    """Replacement for pytest's tmp_path_factory constrained to the repo."""

    def __init__(self, root: Path):
        self._root = root
        self._created: list[Path] = []

    def mktemp(self, basename: str, numbered: bool = True) -> Path:
        suffix = f"_{uuid.uuid4().hex}" if numbered else ""
        directory = basename if not suffix else f"{basename}{suffix}"
        path = self._root / directory
        path.mkdir(parents=True, exist_ok=False)
        self._created.append(path)
        return path

    def cleanup(self, path: Path | None = None) -> None:
        targets = [path] if path is not None else list(self._created)
        for target in targets:
            shutil.rmtree(target, ignore_errors=True)
            if target in self._created:
                self._created.remove(target)


@pytest.fixture(scope="session")
def tmp_path_factory() -> Generator[RepoTmpPathFactory, None, None]:
    factory = RepoTmpPathFactory(_ensure_repo_tmp_root())
    yield factory
    factory.cleanup()


@pytest.fixture
def tmp_path(tmp_path_factory: RepoTmpPathFactory) -> Generator[Path, None, None]:
    path = tmp_path_factory.mktemp("tmp")
    try:
        yield path
    finally:
        tmp_path_factory.cleanup(path)


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
        bot = Mock(spec=Bot)
        bot.delete_message = AsyncMock()
        bot.edit_message_text = AsyncMock()
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
