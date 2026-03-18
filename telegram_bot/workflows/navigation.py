from __future__ import annotations

from collections.abc import MutableMapping
from datetime import datetime, timezone
from typing import Any, Literal, cast

from telegram import Message
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ..config import logger
from ..utils import safe_send_message
from .search_session import SearchSession, clear_search_session

NavigationState = Literal["idle", "search", "delete", "link"]

CHAT_NAVIGATION_KEY = "chat_navigation"
LEGACY_HOME_MENU_MESSAGES_KEY = "home_menu_messages"
DEFAULT_NAVIGATION_STATE: NavigationState = "idle"
_UNSET = object()

DELETE_WORKFLOW_KEYS = (
    "show_path_to_delete",
    "next_action",
    "prompt_message_id",
    "season_to_delete_num",
    "selection_choices",
    "selection_target_kind",
    "path_to_delete",
    "delete_target_kind",
)

LINK_WORKFLOW_KEYS = (
    "pending_magnet_link",
    "pending_info_url",
    "torrent_file_path",
    "temp_magnet_choices_details",
    "pending_torrent",
    "link_prompt_message_id",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _coerce_message_id(value: Any) -> int | None:
    return int(value) if isinstance(value, int) else None


def _normalize_navigation_state(value: Any) -> NavigationState:
    if value in {"idle", "search", "delete", "link"}:
        return cast(NavigationState, value)
    return DEFAULT_NAVIGATION_STATE


def _default_navigation_record(*, home_menu_message_id: int | None = None) -> dict[str, Any]:
    now = _utc_now_iso()
    return {
        "state": DEFAULT_NAVIGATION_STATE,
        "home_menu_message_id": home_menu_message_id,
        "home_menu_last_rendered_at": now if home_menu_message_id is not None else None,
        "active_prompt_message_id": None,
        "last_transition_at": now,
    }


def _normalize_navigation_record(
    record: MutableMapping[str, Any],
    *,
    legacy_home_menu_message_id: int | None = None,
) -> dict[str, Any]:
    normalized = _default_navigation_record(home_menu_message_id=legacy_home_menu_message_id)
    normalized["state"] = _normalize_navigation_state(record.get("state"))
    normalized["home_menu_message_id"] = _coerce_message_id(
        record.get("home_menu_message_id", legacy_home_menu_message_id)
    )
    normalized["home_menu_last_rendered_at"] = record.get("home_menu_last_rendered_at")
    normalized["active_prompt_message_id"] = _coerce_message_id(
        record.get("active_prompt_message_id")
    )
    normalized["last_transition_at"] = (
        record.get("last_transition_at") or normalized["last_transition_at"]
    )
    return normalized


def _get_bot_data_from_application(application: Any) -> MutableMapping[str, Any]:
    bot_data = getattr(application, "bot_data", None)
    if not isinstance(bot_data, MutableMapping):
        raise TypeError("application.bot_data must be a mutable mapping.")
    return bot_data


def _get_application_from_context(context: ContextTypes.DEFAULT_TYPE) -> Any:
    application = getattr(context, "application", None)
    if application is not None:
        return application

    fallback = type("ApplicationFallback", (), {})()
    setattr(fallback, "bot_data", getattr(context, "bot_data", {}))
    return fallback


def _ensure_legacy_home_menu_store(bot_data: MutableMapping[str, Any]) -> dict[int, int]:
    raw_store = bot_data.get(LEGACY_HOME_MENU_MESSAGES_KEY)
    if isinstance(raw_store, dict):
        return raw_store

    store: dict[int, int] = {}
    bot_data[LEGACY_HOME_MENU_MESSAGES_KEY] = store
    return store


def _ensure_chat_navigation_store(bot_data: MutableMapping[str, Any]) -> dict[int, dict[str, Any]]:
    raw_store = bot_data.get(CHAT_NAVIGATION_KEY)
    if isinstance(raw_store, dict):
        return raw_store

    store: dict[int, dict[str, Any]] = {}
    bot_data[CHAT_NAVIGATION_KEY] = store
    return store


def get_application_chat_navigation_state(application: Any, chat_id: int) -> dict[str, Any]:
    bot_data = _get_bot_data_from_application(application)
    store = _ensure_chat_navigation_store(bot_data)
    normalized_chat_id = int(chat_id)
    raw_record = store.get(normalized_chat_id)

    legacy_store = _ensure_legacy_home_menu_store(bot_data)
    legacy_home_menu_message_id = _coerce_message_id(legacy_store.get(normalized_chat_id))

    if isinstance(raw_record, MutableMapping):
        normalized_record = _normalize_navigation_record(
            raw_record,
            legacy_home_menu_message_id=legacy_home_menu_message_id,
        )
    else:
        normalized_record = _default_navigation_record(
            home_menu_message_id=legacy_home_menu_message_id,
        )

    store[normalized_chat_id] = normalized_record
    if normalized_record["home_menu_message_id"] is None:
        legacy_store.pop(normalized_chat_id, None)
    else:
        legacy_store[normalized_chat_id] = normalized_record["home_menu_message_id"]
    return normalized_record


def get_chat_navigation_state(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> dict[str, Any]:
    application = _get_application_from_context(context)
    return get_application_chat_navigation_state(application, chat_id)


def get_home_menu_message_id_from_application(application: Any, chat_id: int) -> int | None:
    return _coerce_message_id(
        get_application_chat_navigation_state(application, chat_id).get("home_menu_message_id")
    )


def set_home_menu_message_id_for_application(
    application: Any,
    chat_id: int,
    message_id: int,
) -> None:
    record = get_application_chat_navigation_state(application, chat_id)
    normalized_message_id = int(message_id)
    record["home_menu_message_id"] = normalized_message_id
    record["home_menu_last_rendered_at"] = _utc_now_iso()
    bot_data = _get_bot_data_from_application(application)
    _ensure_legacy_home_menu_store(bot_data)[int(chat_id)] = normalized_message_id


def clear_home_menu_message_id_for_application(application: Any, chat_id: int) -> None:
    record = get_application_chat_navigation_state(application, chat_id)
    record["home_menu_message_id"] = None
    record["home_menu_last_rendered_at"] = None
    bot_data = _get_bot_data_from_application(application)
    _ensure_legacy_home_menu_store(bot_data).pop(int(chat_id), None)


def set_chat_navigation_state(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    state: NavigationState,
    *,
    home_menu_message_id: int | None | object = _UNSET,
    active_prompt_message_id: int | None | object = _UNSET,
) -> dict[str, Any]:
    record = get_chat_navigation_state(context, chat_id)
    record["state"] = _normalize_navigation_state(state)
    record["last_transition_at"] = _utc_now_iso()

    if home_menu_message_id is not _UNSET:
        record["home_menu_message_id"] = _coerce_message_id(home_menu_message_id)
        record["home_menu_last_rendered_at"] = (
            _utc_now_iso() if isinstance(home_menu_message_id, int) else None
        )
        application = _get_application_from_context(context)
        if isinstance(home_menu_message_id, int):
            set_home_menu_message_id_for_application(application, chat_id, home_menu_message_id)
        else:
            clear_home_menu_message_id_for_application(application, chat_id)

    if active_prompt_message_id is not _UNSET:
        record["active_prompt_message_id"] = _coerce_message_id(active_prompt_message_id)
    elif state == DEFAULT_NAVIGATION_STATE:
        record["active_prompt_message_id"] = None

    user_data = getattr(context, "user_data", None)
    if isinstance(user_data, MutableMapping):
        if state == DEFAULT_NAVIGATION_STATE:
            user_data.pop("active_workflow", None)
        else:
            user_data["active_workflow"] = state

    return record


def mark_chat_idle(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    *,
    home_menu_message_id: int | None | object = _UNSET,
) -> dict[str, Any]:
    return set_chat_navigation_state(
        context,
        chat_id,
        DEFAULT_NAVIGATION_STATE,
        home_menu_message_id=home_menu_message_id,
        active_prompt_message_id=None,
    )


def mark_chat_workflow_active(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    workflow: Literal["search", "delete", "link"],
    *,
    prompt_message_id: int | None = None,
) -> dict[str, Any]:
    return set_chat_navigation_state(
        context,
        chat_id,
        workflow,
        active_prompt_message_id=prompt_message_id,
    )


def set_active_prompt_message_id(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int | None,
) -> None:
    record = get_chat_navigation_state(context, chat_id)
    record["active_prompt_message_id"] = _coerce_message_id(message_id)


def get_active_prompt_message_id(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> int | None:
    return _coerce_message_id(
        get_chat_navigation_state(context, chat_id).get("active_prompt_message_id")
    )


def clear_chat_navigation_state(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    application = _get_application_from_context(context)
    bot_data = _get_bot_data_from_application(application)
    _ensure_chat_navigation_store(bot_data).pop(int(chat_id), None)
    _ensure_legacy_home_menu_store(bot_data).pop(int(chat_id), None)


def get_user_data_store(context: ContextTypes.DEFAULT_TYPE) -> MutableMapping[str, Any]:
    if context.user_data is None:
        context.user_data = {}
    return context.user_data


def clear_delete_workflow_state(user_data: MutableMapping[str, Any] | None) -> None:
    if not isinstance(user_data, MutableMapping):
        return
    for key in DELETE_WORKFLOW_KEYS:
        user_data.pop(key, None)


def clear_link_workflow_state(user_data: MutableMapping[str, Any] | None) -> None:
    if not isinstance(user_data, MutableMapping):
        return
    for key in LINK_WORKFLOW_KEYS:
        user_data.pop(key, None)


def clear_all_workflow_state(user_data: MutableMapping[str, Any] | None) -> None:
    if not isinstance(user_data, MutableMapping):
        return
    clear_search_session(user_data)
    clear_delete_workflow_state(user_data)
    clear_link_workflow_state(user_data)
    user_data.pop("active_workflow", None)


def _resolve_active_prompt_message_id(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    user_data: MutableMapping[str, Any],
) -> int | None:
    navigation_prompt_message_id = get_active_prompt_message_id(context, chat_id)
    search_prompt_message_id = SearchSession.from_user_data(user_data).prompt_message_id
    delete_prompt_message_id = user_data.get("prompt_message_id")
    link_prompt_message_id = user_data.get("link_prompt_message_id")

    for candidate in (
        search_prompt_message_id,
        delete_prompt_message_id,
        link_prompt_message_id,
        navigation_prompt_message_id,
    ):
        if isinstance(candidate, int):
            return candidate
    return None


async def return_to_home(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    *,
    source_message: Message | None = None,
    message_text: str | None = None,
    message_parse_mode: str | None = ParseMode.MARKDOWN_V2,
    replace_home_menu: bool = False,
) -> None:
    """
    Resets conversational workflow state and renders the shared home menu.

    Best-effort cleanup is intentionally non-blocking; failures should not prevent recovery.
    """
    from ..ui.home_menu import delete_home_menu_message, show_home_menu

    user_data = get_user_data_store(context)
    active_prompt_message_id = _resolve_active_prompt_message_id(chat_id, context, user_data)
    clear_all_workflow_state(user_data)
    mark_chat_idle(context, chat_id)

    if isinstance(source_message, Message):
        try:
            await source_message.delete()
        except TelegramError:
            pass

    if isinstance(active_prompt_message_id, int) and active_prompt_message_id != getattr(
        source_message, "message_id", None
    ):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=active_prompt_message_id)
        except TelegramError:
            pass

    if replace_home_menu:
        await delete_home_menu_message(context, chat_id)
        mark_chat_idle(context, chat_id, home_menu_message_id=None)

    if message_text:
        await safe_send_message(
            context.bot,
            chat_id=chat_id,
            text=message_text,
            parse_mode=message_parse_mode,
        )

    try:
        rendered_message = await show_home_menu(context, chat_id)
    except Exception:
        logger.exception("Failed to render home menu for chat %s", chat_id)
        return

    mark_chat_idle(context, chat_id, home_menu_message_id=rendered_message.message_id)
