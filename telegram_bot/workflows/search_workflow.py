# telegram_bot/workflows/search_workflow.py

import asyncio
import os
import re
import shutil
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, MutableMapping

from telegram import (
    Update,
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
)
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.helpers import escape_markdown

from ..config import logger, MAX_TORRENT_SIZE_GB, LOG_SCRAPER_STATS
from ..services import search_logic, torrent_service, scraping_service, plex_service
from ..services.media_manager import validate_and_enrich_torrent, _get_path_size_bytes
from ..ui.messages import format_media_summary
from ..ui.views import send_confirmation_prompt
from ..utils import (
    safe_edit_message,
    parse_torrent_name,
    safe_send_message,
    format_bytes,
)
from .search_session import (
    CONTEXT_LOST_MESSAGE,
    SearchSession,
    SearchSessionError,
    SearchStep,
    clear_search_session,
)
from .search_parser import parse_search_query

RESULTS_PAGE_SIZE = 5
RESULTS_SESSION_TTL_SECONDS = 15 * 60
FOUR_K_SIZE_MULTIPLIER = 2.0
RESOLUTION_FILTERS: tuple[str, ...] = ("all", "720p", "1080p", "2160p")
RESULTS_EXPIRED_MESSAGE = (
    "? These search results have expired\\. Please start a new search\\."
)
EPISODE_CANDIDATE_LIMIT = 3
DEFAULT_SIZE_TARGET_GB = 1.05
RESOLUTION_SIZE_TARGETS = {
    "720p": 0.9,
    "1080p": 1.3,
    "2160p": 5.0,
}
SIZE_VARIANCE_WEIGHT = 12.0
SIZE_DEVIATION_WEIGHT = 8.0
COLLECTION_MOVIE_PREVIEW_LIMIT = 6
COLLECTION_CODEC_CHOICES: tuple[str, ...] = ("x264", "x265", "any")


@dataclass(slots=True)
class EpisodeCandidate:
    episode: int
    link: str
    title: str
    source: str
    uploader: str
    info_url: str | None = None
    size_gb: float | None = None
    seeders: int | None = None
    resolution: str | None = None
    codec: str | None = None
    score: float | None = None

    @property
    def release_key(self) -> tuple[str, str]:
        normalized_source = (self.source or "Unknown").strip() or "Unknown"
        normalized_uploader = (self.uploader or "Anonymous").strip() or "Anonymous"
        return (normalized_source, normalized_uploader)


@dataclass(slots=True)
class SeasonConsistencySummary:
    release_source: str | None
    release_uploader: str | None
    avg_size_gb: float | None
    size_spread_gb: float | None
    matched_count: int
    total_count: int
    fallback_episodes: list[int] = field(default_factory=list)
    resolution: str | None = None

    @property
    def coverage_ratio(self) -> float:
        if self.total_count <= 0:
            return 0.0
        return self.matched_count / self.total_count

    def release_label(self) -> str:
        uploader = (self.release_uploader or "Anonymous").strip() or "Anonymous"
        source = (self.release_source or "Unknown").strip() or "Unknown"
        return f"{uploader} via {source}"


def _normalize_label(value: Any) -> str:
    text = ""
    if isinstance(value, str):
        text = value
    elif value is not None:
        text = str(value)
    return re.sub(r"[\W_]+", "", text.strip()).casefold()


_COLLECTION_SUFFIX_KEYWORDS = (
    "franchise",
    "film series",
    "films",
    "collection",
    "cinematic universe",
    "universe",
)


def _sanitize_collection_name(value: str | None) -> str:
    invalid_chars = '<>:"/\\|?*'
    safe_value = "".join(c for c in (value or "") if c not in invalid_chars).strip()
    if not safe_value:
        return "Collection"
    suffix_pattern = re.compile(
        r"\((?:[^)]*\b(?:franchise|collection|film series|films|cinematic universe|universe)\b[^)]*)\)",
        re.IGNORECASE,
    )
    safe_value = suffix_pattern.sub("", safe_value).strip()
    for keyword in _COLLECTION_SUFFIX_KEYWORDS:
        keyword_pattern = re.compile(rf"\b{keyword}\b\.?$", re.IGNORECASE)
        new_value = keyword_pattern.sub("", safe_value).strip()
        safe_value = new_value or safe_value
    safe_value = safe_value.strip("-_ ,")
    return safe_value or "Collection"


def _format_collection_movie_label(movie: dict[str, Any]) -> str:
    title = str(movie.get("title") or "Untitled").strip() or "Untitled"
    year = movie.get("year")
    if isinstance(year, int):
        return f"{title} ({year})"
    return title


def _ensure_identifier(movie: dict[str, Any], index: int) -> str:
    title = str(movie.get("title") or "")
    year = movie.get("year")
    base = _normalize_label(title) or f"movie{index}"
    if isinstance(year, int):
        base = f"{base}{year}"
    return f"{base}-{index}"


def _parse_release_iso(value: Any) -> date | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return None
    return None


def _get_user_data_store(
    context: ContextTypes.DEFAULT_TYPE,
) -> MutableMapping[str, Any]:
    if context.user_data is None:
        context.user_data = {}
    return context.user_data


def _get_session(context: ContextTypes.DEFAULT_TYPE) -> SearchSession:
    return SearchSession.from_user_data(_get_user_data_store(context))


def _save_session(context: ContextTypes.DEFAULT_TYPE, session: SearchSession) -> None:
    session.save(_get_user_data_store(context))


def _get_callback_data(query: CallbackQuery) -> str:
    """Returns callback data as a string even when Telegram omits it."""
    return query.data or ""


async def handle_search_workflow(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Manages text-based replies for the search workflow."""
    if not isinstance(update.message, Message) or not update.message.text:
        return
    session = _get_session(context)

    chat = update.effective_chat
    if not chat:
        return

    query = update.message.text.strip()
    prompt_message_id = session.consume_prompt_message_id()

    try:
        if prompt_message_id:
            await context.bot.delete_message(
                chat_id=chat.id, message_id=prompt_message_id
            )
        await update.message.delete()
    except BadRequest:
        pass
    else:
        _save_session(context, session)

    if not session.is_active:
        return

    try:
        if session.media_type == "movie":
            if session.step == SearchStep.TITLE:
                await _handle_movie_title_reply(chat.id, query, context, session)
            elif session.step == SearchStep.YEAR:
                await _handle_movie_year_reply(chat.id, query, context, session)
        elif session.media_type == "tv":
            if session.step == SearchStep.TITLE:
                await _handle_tv_title_reply(chat.id, query, context, session)
            elif session.step == SearchStep.TV_SEASON:
                await _handle_tv_season_reply(chat.id, query, context, session)
            elif session.step == SearchStep.TV_EPISODE:
                await _handle_tv_episode_reply(chat.id, query, context, session)
    except SearchSessionError as exc:
        await safe_send_message(
            context.bot,
            chat.id,
            exc.user_message,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        clear_search_session(context.user_data)


async def handle_search_buttons(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handles all button presses related to the search workflow."""
    query = update.callback_query
    if not query or not isinstance(query.message, Message):
        return

    action = _get_callback_data(query)
    if not action:
        return

    if action.startswith("search_start_"):
        await _handle_start_button(query, context)
        return

    session = _get_session(context)
    requires_session = (
        action.startswith(
            (
                "search_movie_scope_",
                "search_resolution_",
                "search_tv_scope_",
                "search_tv_season_resolution_",
                "search_tv_season_codec_",
                "search_select_season_",
                "search_select_episode_",
                "search_select_year_",
            )
        )
        or action.startswith("search_select_")
        or action.startswith("search_results_page_")
        or action.startswith("search_results_filter_resolution_")
        or action.startswith("search_results_filter_codec_")
        or action.startswith("search_collection_resolution_")
        or action.startswith("search_collection_codec_")
        or action.startswith("search_collection_toggle_")
        or action
        in {
            "search_collection_accept",
            "search_collection_confirm",
        }
        or action == "search_tv_change_details"
    )

    if requires_session and not session.is_active:
        await safe_edit_message(
            query.message,
            text=CONTEXT_LOST_MESSAGE,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    try:
        if action.startswith("search_movie_scope_"):
            await _handle_movie_scope_button(query, context, session)
        elif action == "search_collection_accept":
            await _handle_collection_accept(query, context, session)
        elif action.startswith("search_collection_resolution_"):
            await _handle_collection_resolution_button(query, context, session)
        elif action.startswith("search_collection_codec_"):
            await _handle_collection_codec_button(query, context, session)
        elif action.startswith("search_collection_toggle_"):
            await _handle_collection_movie_toggle(query, context, session)
        elif action == "search_collection_confirm":
            await _handle_collection_confirm(query, context, session)
        elif action.startswith("search_resolution_"):
            await _handle_resolution_button(query, context, session)
        elif action.startswith("search_tv_scope_"):
            await _handle_tv_scope_selection(query, context, session)
        elif action.startswith("search_tv_season_resolution_"):
            await _handle_tv_season_resolution_button(query, context, session)
        elif action.startswith("search_tv_season_codec_"):
            await _handle_tv_season_codec_button(query, context, session)
        elif action.startswith("search_select_season_"):
            await _handle_season_selection_button(query, context, session)
        elif action.startswith("search_select_episode_"):
            await _handle_episode_selection_button(query, context, session)
        elif action.startswith("search_select_year_"):
            await _handle_year_selection_button(query, context, session)
        elif action == "search_tv_change_details":
            await _handle_tv_change_details(query, context, session)
        elif action.startswith("search_select_"):
            await _handle_result_selection_button(query, context, session)
        elif action.startswith("search_results_page_"):
            await _handle_results_page_button(query, context, session)
        elif action.startswith("search_results_filter_resolution_"):
            await _handle_results_filter_button(query, context, session)
        elif action.startswith("search_results_filter_codec_"):
            await _handle_results_codec_filter_button(query, context, session)
        else:
            logger.warning(f"Received unhandled search callback: {action}")
    except SearchSessionError as exc:
        await safe_edit_message(
            query.message,
            text=exc.user_message,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        clear_search_session(context.user_data)


# --- Text Reply Handlers ---


async def _handle_movie_title_reply(
    chat_id: int, query: str, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
):
    """Handles the user's reply when asked for a movie title."""
    if session.media_type != "movie" or session.step != SearchStep.TITLE:
        return

    parsed_query = parse_search_query(query)
    fallback_title = re.sub(r"[^\w\s-]", "", query).strip()
    base_title = (parsed_query.title or fallback_title).strip()
    title = base_title.title()
    if not title:
        await _send_prompt(
            chat_id,
            context,
            "I couldn't recognize a movie title from that message. Please try again.",
            session=session,
        )
        return

    session.media_type = "movie"
    session.set_title(title)

    if session.collection_mode:
        _save_session(context, session)
        await _start_collection_lookup(chat_id, context, session, title)
        return

    if parsed_query.year:
        full_title = f"{title} ({parsed_query.year})"
        session.set_final_title(full_title)
        session.advance(SearchStep.RESOLUTION)
        _save_session(context, session)
        await _search_movie_results(chat_id, context, session)
        return
    _save_session(context, session)

    bot_data = (
        context.bot_data if isinstance(getattr(context, "bot_data", None), dict) else {}
    )
    has_search_config = bool(bot_data.get("SEARCH_CONFIG"))
    used_cached_years = False
    missing_config_notice_needed = False

    status_message = await safe_send_message(
        context.bot,
        chat_id,
        f"Looking up release years for *{escape_markdown(title, version=2)}* on Wikipedia\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    years: list[int] = []
    corrected: str | None = None

    if has_search_config:
        try:
            years, corrected = await scraping_service.fetch_movie_years_from_wikipedia(
                title
            )
        except Exception:
            years, corrected = [], None
    else:
        cached = scraping_service.get_cached_movie_years(title)
        if cached:
            years, corrected = cached
            used_cached_years = True
        else:
            missing_config_notice_needed = True

    if corrected and isinstance(corrected, str) and corrected.strip():
        session.set_title(corrected, resolved_title=corrected)
        display_title = corrected
    else:
        display_title = title

    wiki_notice: str | None = None
    if missing_config_notice_needed:
        wiki_notice = (
            "⚠️ Search configuration unavailable; skipping Wikipedia hints for "
            f"*{escape_markdown(display_title, version=2)}*."
        )

    allow_year_prompts = has_search_config or used_cached_years
    effective_years = years if allow_year_prompts else []

    if isinstance(effective_years, list) and len(effective_years) > 1:
        unique_years = [str(y) for y in sorted({int(y) for y in effective_years})]
        session.advance(SearchStep.YEAR)
        session.prompt_message_id = status_message.message_id
        _save_session(context, session)
        await _prompt_for_year_selection(
            status_message, context, display_title, unique_years, session=session
        )
        return
    if isinstance(effective_years, list) and len(effective_years) == 1:
        full_title = f"{display_title} ({effective_years[0]})"
        session.set_final_title(full_title)
        session.advance(SearchStep.RESOLUTION)
        _save_session(context, session)
        await _search_movie_results(status_message, context, session)
        return

    results = await search_logic.orchestrate_searches(display_title, "movie", context)
    await _process_preliminary_results(
        status_message, context, results, session=session, notice=wiki_notice
    )


async def _handle_movie_year_reply(
    chat_id: int, query: str, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
):
    """Handles the user's reply when asked for a movie year."""
    if session.media_type != "movie" or session.step != SearchStep.YEAR:
        return

    title = session.effective_title
    if not title:
        await safe_send_message(
            context.bot,
            chat_id,
            CONTEXT_LOST_MESSAGE,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        clear_search_session(context.user_data)
        return

    if not (query.isdigit() and len(query) == 4):
        error_text = f"That doesn't look like a valid 4\\-digit year\\. Please try again for *{escape_markdown(title, version=2)}* or cancel\\."
        await _send_prompt(chat_id, context, error_text, session=session)
        return

    full_title = f"{title} ({query})"
    session.set_final_title(full_title)
    session.advance(SearchStep.RESOLUTION)
    _save_session(context, session)
    await _search_movie_results(chat_id, context, session)


async def _start_collection_lookup(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    session: SearchSession,
    display_title: str,
) -> None:
    """Initiates a franchise lookup for collection mode."""
    status_message = await safe_send_message(
        context.bot,
        chat_id,
        (
            f"🧩 Searching Wikipedia for franchises that include "
            f"*{escape_markdown(display_title, version=2)}*…"
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    try:
        result = await scraping_service.fetch_movie_franchise_details(display_title)
    except Exception as exc:  # noqa: BLE001
        logger.error("Franchise lookup failed for '%s': %s", display_title, exc)
        result = None

    if not result:
        await safe_edit_message(
            status_message,
            text=(
                f"⚠️ No franchise information was found for "
                f"*{escape_markdown(display_title, version=2)}*\\.\n"
                "Please send another title or cancel the operation\\."
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        session.prompt_message_id = status_message.message_id
        _save_session(context, session)
        return

    franchise_name, movies = result
    normalized_movies: list[dict[str, Any]] = []
    today = date.today()
    unreleased_count = 0
    for idx, raw_movie in enumerate(movies or []):
        raw_title = raw_movie.get("title") or raw_movie.get("name") or display_title
        title_str = str(raw_title).strip() or display_title
        year_value = raw_movie.get("year")
        parsed_year = _coerce_int(year_value)
        release_date = _parse_release_iso(raw_movie.get("release_date"))
        if release_date is None or release_date > today:
            unreleased_count += 1
            continue
        entry = {
            "title": title_str,
            "year": parsed_year,
            "identifier": _ensure_identifier(
                {"title": title_str, "year": parsed_year}, idx
            ),
            "owned": False,
            "queued": False,
            "release_date": release_date.isoformat(),
        }
        normalized_movies.append(entry)

    if not normalized_movies:
        if unreleased_count:
            message_text = (
                f"⚠️ The detected franchise for "
                f"*{escape_markdown(display_title, version=2)}* has no released titles available yet\\.\n"
                "Please try again once those movies premiere or pick another franchise\\."
            )
        else:
            message_text = (
                f"⚠️ The detected franchise for "
                f"*{escape_markdown(display_title, version=2)}* contains no movies I can queue\\."
            )
        await safe_edit_message(
            status_message,
            text=message_text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        session.prompt_message_id = status_message.message_id
        _save_session(context, session)
        return

    session.collection_name = franchise_name
    session.collection_fs_name = _sanitize_collection_name(franchise_name)
    session.collection_movies = normalized_movies
    session.collection_exclusions = []
    session.collection_resolution = None
    session.collection_codec = None
    session.collection_seed_size_gb = None
    session.collection_seed_uploader = None
    session.collection_owned_count = 0
    session.prompt_message_id = status_message.message_id
    _save_session(context, session)

    await _prompt_collection_confirmation(status_message, context, session)


async def _prompt_collection_confirmation(
    message: Message, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    franchise = session.collection_name or "Franchise"
    movies = session.collection_movies or []
    preview_lines: list[str] = []
    limited_movies = movies[:COLLECTION_MOVIE_PREVIEW_LIMIT]
    for movie in limited_movies:
        label = escape_markdown(_format_collection_movie_label(movie), version=2)
        preview_lines.append(f"• {label}")
    remaining = max(len(movies) - len(limited_movies), 0)
    if remaining > 0:
        remaining_label = escape_markdown(f"…and {remaining} more", version=2)
        preview_lines.append(f"• {remaining_label}")

    summary = "\n".join(preview_lines)
    text = (
        f"🎬 *{escape_markdown(franchise, version=2)}* contains "
        f"*{len(movies)}* film{'s' if len(movies) != 1 else ''}\\.\n"
        f"{summary}\n\n"
        "Use this collection?"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Use Collection", callback_data="search_collection_accept"
                )
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
        ]
    )
    await safe_edit_message(
        message,
        text=text,
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_tv_title_reply(
    chat_id: int, query: str, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
):
    """Handles user reply for a TV show title."""
    if session.media_type != "tv" or session.step != SearchStep.TITLE:
        return

    parsed_query = parse_search_query(query)
    fallback_title = re.sub(r"[^\w\s-]", "", query).strip()
    base_title = (parsed_query.title or fallback_title).strip()
    sanitized_title = base_title.title()
    if not sanitized_title:
        await _send_prompt(
            chat_id,
            context,
            "I couldn't recognize a TV show title from that message. Please try again.",
            session=session,
        )
        return

    session.media_type = "tv"
    session.set_title(sanitized_title)

    if parsed_query.title:
        if parsed_query.season and parsed_query.episode:
            await _fast_track_tv_episode_resolution(
                chat_id,
                context,
                session,
                sanitized_title,
                parsed_query.season,
                parsed_query.episode,
                allow_detail_change=True,
            )
            return
        if parsed_query.season:
            await _fast_track_tv_scope_selection(
                chat_id,
                context,
                session,
                sanitized_title,
                parsed_query.season,
            )
            return
        if parsed_query.episode:
            await _fast_track_episode_with_assumed_season(
                chat_id,
                context,
                session,
                sanitized_title,
                parsed_query.episode,
            )
            return

    session.advance(SearchStep.TV_SEASON)
    _save_session(context, session)
    await _prompt_for_tv_season_selection(
        chat_id, context, session, display_title=sanitized_title
    )


async def _prompt_for_tv_season_selection(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    session: SearchSession,
    *,
    display_title: str | None = None,
) -> None:
    """Fetches season counts (when possible) and prompts the user to pick a season."""
    title = display_title or session.effective_title or session.title
    if not title:
        raise SearchSessionError()

    status_message = await safe_send_message(
        context.bot,
        chat_id,
        f"🎞️ Checking seasons for *{escape_markdown(title, version=2)}* on Wikipedia\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    try:
        seasons_count = await scraping_service.fetch_total_seasons_from_wikipedia(title)
    except Exception:
        seasons_count = None

    SEASON_COLUMNS = 4
    MAX_SEASON_BUTTONS = 40

    if (
        isinstance(seasons_count, int)
        and seasons_count > 0
        and seasons_count <= MAX_SEASON_BUTTONS
    ):
        buttons = [
            InlineKeyboardButton(str(i), callback_data=f"search_select_season_{i}")
            for i in range(1, seasons_count + 1)
        ]
        keyboard = [
            buttons[i : i + SEASON_COLUMNS]
            for i in range(0, len(buttons), SEASON_COLUMNS)
        ]
        keyboard.append(
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]
        )
        session.prompt_message_id = status_message.message_id
        _save_session(context, session)
        await safe_edit_message(
            status_message,
            text=(
                f"Found *{escape_markdown(str(seasons_count), version=2)}* season\\(s\\) for "
                f"*{escape_markdown(title, version=2)}*\\. Please select a season:"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if isinstance(seasons_count, int) and seasons_count > MAX_SEASON_BUTTONS:
        prompt_text = (
            f"Found *{escape_markdown(str(seasons_count), version=2)}* seasons for "
            f"*{escape_markdown(title, version=2)}*\\. Now, please send the season number\\."
        )
    else:
        prompt_text = (
            f"Could not determine the total seasons for *{escape_markdown(title, version=2)}*\\.\n"
            "Please send the season number\\."
        )

    session.prompt_message_id = status_message.message_id
    _save_session(context, session)
    await safe_edit_message(
        status_message,
        text=prompt_text,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


def _build_tv_scope_keyboard(
    *, include_change_button: bool = False
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                "Single Episode", callback_data="search_tv_scope_single"
            ),
            InlineKeyboardButton(
                "Entire Season", callback_data="search_tv_scope_season"
            ),
        ]
    ]
    if include_change_button:
        rows.append(
            [InlineKeyboardButton("Change", callback_data="search_tv_change_details")]
        )
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")])
    return InlineKeyboardMarkup(rows)


async def _fast_track_tv_scope_selection(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    session: SearchSession,
    title: str,
    season: int,
) -> None:
    """Skips the season prompt and jumps straight to TV scope selection."""
    session.season = int(season)
    session.tv_scope = None
    session.advance(SearchStep.TV_SCOPE)
    _save_session(context, session)

    prompt_text = (
        f"Detected Season *{escape_markdown(str(season), version=2)}* for "
        f"*{escape_markdown(title, version=2)}*\\.\n"
        "Do you want a single episode or the entire season?"
    )
    sent_message = await safe_send_message(
        context.bot,
        chat_id,
        prompt_text,
        reply_markup=_build_tv_scope_keyboard(include_change_button=True),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    session.prompt_message_id = sent_message.message_id
    _save_session(context, session)


async def _validate_episode_released(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    title: str,
    season: int,
    episode: int,
) -> bool:
    """Checks if an episode has been released yet. Returns False if unreleased."""
    try:
        titles_map, _ = await scraping_service.fetch_episode_titles_for_season(
            title, season
        )
        meta = titles_map.get(episode)
        if meta and meta.get("release_date"):
            current_date = datetime.now().date().isoformat()
            if meta["release_date"] > current_date:
                logger.info(
                    "[SEARCH] Guard: Episode %s S%02dE%02d is unreleased (Released: %s)",
                    title,
                    season,
                    episode,
                    meta["release_date"],
                )
                await safe_send_message(
                    context.bot,
                    chat_id,
                    text=f"❌ Episode *{escape_markdown(str(episode), version=2)}* of "
                    f"*{escape_markdown(title, version=2)}* S{int(season):02d} "
                    f"is scheduled for release on *{escape_markdown(meta['release_date'], version=2)}* "
                    "and cannot be searched for yet\\.",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
                return False
    except Exception as e:
        logger.debug("[SEARCH] Error during release date check: %s", e)
    return True


async def _fast_track_tv_episode_resolution(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    session: SearchSession,
    title: str,
    season: int,
    episode: int,
    *,
    notice: str | None = None,
    allow_detail_change: bool = False,
) -> None:
    """Skip directly to resolution selection for a detected episode."""
    if not await _validate_episode_released(chat_id, context, title, season, episode):
        clear_search_session(context.user_data)
        return

    final_title = f"{title} S{int(season):02d}E{int(episode):02d}"
    session.media_type = "tv"
    session.tv_scope = "single"
    session.season = int(season)
    session.episode = int(episode)
    session.set_final_title(final_title)
    session.advance(SearchStep.RESOLUTION)
    _save_session(context, session)

    detection_text = notice or (
        f"Detected Season *{escape_markdown(str(season), version=2)}* "
        f"Episode *{escape_markdown(str(episode), version=2)}* for "
        f"*{escape_markdown(title, version=2)}*\\."
    )

    session.allow_detail_change = allow_detail_change
    _save_session(context, session)

    await _search_tv_single_results(
        chat_id,
        context,
        session,
        notice=detection_text,
    )


async def _fast_track_episode_with_assumed_season(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    session: SearchSession,
    title: str,
    episode: int,
) -> None:
    """Handles the rare 'episode only' hints by assuming Season 1."""
    assumed_season = 1
    notice = (
        f"Detected Episode *{escape_markdown(str(episode), version=2)}* for "
        f"*{escape_markdown(title, version=2)}* and assumed Season *01*."
    )
    await _fast_track_tv_episode_resolution(
        chat_id,
        context,
        session,
        title,
        assumed_season,
        episode,
        notice=notice,
        allow_detail_change=True,
    )


async def _handle_tv_season_reply(
    chat_id: int, query: str, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
):
    """Handles user reply for a season number."""
    if session.media_type != "tv" or session.step != SearchStep.TV_SEASON:
        return

    title = session.effective_title or ""
    if not query.isdigit():
        error_text = f"That doesn't look like a valid number\\. Please send the season number for *{escape_markdown(title, version=2)}*\\."
        await _send_prompt(chat_id, context, error_text, session=session)
        return

    session.season = int(query)
    session.advance(SearchStep.TV_SCOPE)
    _save_session(context, session)

    prompt_text = (
        f"Season *{escape_markdown(query, version=2)}* selected\\. "
        "Do you want a single episode or the entire season?"
    )
    keyboard = _build_tv_scope_keyboard()
    sent_message = await safe_send_message(
        context.bot,
        chat_id,
        prompt_text,
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    session.prompt_message_id = sent_message.message_id
    _save_session(context, session)


async def _handle_season_selection_button(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    """Handles the user selecting a season from inline buttons (<=5 seasons case)."""
    if not isinstance(query.message, Message):
        return

    try:
        data = _get_callback_data(query)
        season_str = data.split("_")[3]
        season_num = int(season_str)
    except Exception:
        await safe_edit_message(
            query.message,
            text="❌ An error occurred with your selection\\. Please try again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    session.season = season_num
    session.advance(SearchStep.TV_SCOPE)
    _save_session(context, session)

    prompt_text = (
        f"Season *{escape_markdown(str(season_num), version=2)}* selected\\. "
        "Do you want a single episode or the entire season?"
    )
    keyboard = _build_tv_scope_keyboard()
    await safe_edit_message(
        query.message,
        text=prompt_text,
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_episode_selection_button(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    """Handles user selecting an episode via inline buttons in the single-episode flow."""
    if not isinstance(query.message, Message):
        return

    try:
        data = _get_callback_data(query)
        episode_str = data.split("_")[3]
        episode_num = int(episode_str)
    except Exception:
        await safe_edit_message(
            query.message,
            text="❌ An error occurred with your selection\\. Please try again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    title = session.effective_title or session.title
    season = session.season
    if not title or season is None:
        await safe_edit_message(
            query.message,
            text=CONTEXT_LOST_MESSAGE,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        clear_search_session(context.user_data)
        return

    if not await _validate_episode_released(
        query.message.chat_id, context, title, season, episode_num
    ):
        return

    full_search_term = f"{title} S{int(season):02d}E{episode_num:02d}"
    session.media_type = "tv"
    session.tv_scope = "single"
    session.episode = episode_num
    session.set_final_title(full_search_term)
    session.advance(SearchStep.RESOLUTION)
    _save_session(context, session)

    session.allow_detail_change = False
    _save_session(context, session)

    await _search_tv_single_results(query.message, context, session)


async def _handle_tv_episode_reply(
    chat_id: int, query: str, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
):
    """Handles user reply for an episode number. Then prompts for quality."""
    if session.media_type != "tv" or session.step != SearchStep.TV_EPISODE:
        return

    title = session.effective_title or session.title
    season = session.season
    if not title or season is None or not query.isdigit():
        error_text = f"That doesn't look like a valid number\\. Please send the episode number for *{escape_markdown(title or '', version=2)} S{int(season or 0):02d}*\\."
        await _send_prompt(chat_id, context, error_text, session=session)
        return

    episode = int(query)
    if not await _validate_episode_released(chat_id, context, title, season, episode):
        return

    full_search_term = f"{title} S{int(season):02d}E{episode:02d}"

    session.media_type = "tv"
    session.tv_scope = "single"
    session.episode = episode
    session.set_final_title(full_search_term)
    session.advance(SearchStep.RESOLUTION)
    _save_session(context, session)

    session.allow_detail_change = False
    _save_session(context, session)

    await _search_tv_single_results(chat_id, context, session)


async def _handle_tv_change_details(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    """Allows users to abandon the fast-path hints and return to manual entry."""
    if not isinstance(query.message, Message):
        return

    title = session.effective_title or session.title
    if not title:
        await safe_edit_message(
            query.message,
            CONTEXT_LOST_MESSAGE,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        clear_search_session(context.user_data)
        return

    session.season = None
    session.episode = None
    session.tv_scope = None
    session.resolution = None
    session.prompt_message_id = None
    session.allow_detail_change = False
    session.advance(SearchStep.TV_SEASON)
    _save_session(context, session)

    await safe_edit_message(
        query.message,
        text="Okay, let's adjust the details\\. Please pick a season\\.",
        reply_markup=None,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    await _prompt_for_tv_season_selection(
        query.message.chat_id, context, session, display_title=title
    )


async def _handle_tv_scope_selection(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    """Handles user's choice between single episode or entire season."""
    if not isinstance(query.message, Message):
        return

    action = _get_callback_data(query)
    title = session.effective_title or session.title
    season = session.season
    if not title or season is None:
        await safe_edit_message(
            query.message,
            CONTEXT_LOST_MESSAGE,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        clear_search_session(context.user_data)
        return

    if action == "search_tv_scope_single":
        session.tv_scope = "single"
        session.advance(SearchStep.TV_EPISODE)
        _save_session(context, session)
        EPISODE_COLUMNS = 4
        MAX_EPISODE_BUTTONS = 40

        episode_count: int | None = None
        if context.bot_data.get("SEARCH_CONFIG"):
            try:
                episode_count = (
                    await scraping_service.fetch_season_episode_count_from_wikipedia(
                        str(title), int(season)
                    )
                )
            except Exception:
                episode_count = None

        if isinstance(episode_count, int) and 0 < episode_count <= MAX_EPISODE_BUTTONS:
            buttons = [
                InlineKeyboardButton(str(i), callback_data=f"search_select_episode_{i}")
                for i in range(1, episode_count + 1)
            ]
            keyboard = [
                buttons[i : i + EPISODE_COLUMNS]
                for i in range(0, len(buttons), EPISODE_COLUMNS)
            ]
            keyboard.append(
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]
            )
            await safe_edit_message(
                query.message,
                text=(
                    f"Season *{escape_markdown(str(season), version=2)}* selected\\. "
                    "Choose an episode below or type the number\\."
                ),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        await safe_edit_message(
            query.message,
            text=f"Season *{escape_markdown(str(season), version=2)}* selected\\.",
            reply_markup=None,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await _send_prompt(
            query.message.chat_id,
            context,
            f"Please send the episode number for Season {escape_markdown(str(season), version=2)}\\.",
            session=session,
        )
        return

    if action == "search_tv_scope_season":
        logger.info(
            f"[WIKI] Verifying season details on Wikipedia for '{title}' S{int(season):02d}."
        )
        await safe_edit_message(
            query.message,
            "Verifying season details on Wikipedia\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        episode_count = (
            await scraping_service.fetch_season_episode_count_from_wikipedia(
                title, season
            )
        )
        logger.info(
            f"[WIKI] Episode count lookup complete for '{title}' S{int(season):02d}: {episode_count}."
        )
        if not episode_count:
            await safe_edit_message(
                query.message,
                "❌ Could not verify episode count\\. Operation cancelled\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        existing_eps = await plex_service.get_existing_episodes_for_season(
            context, str(title), int(season)
        )
        missing_list = [
            i for i in range(1, int(episode_count) + 1) if i not in existing_eps
        ]

        session.season_episode_count = int(episode_count)
        session.existing_episodes = sorted(existing_eps)
        session.missing_episode_numbers = missing_list
        session.tv_scope = "season"
        session.media_type = "tv"
        session.advance(SearchStep.RESOLUTION)
        _save_session(context, session)

        if len(missing_list) == 0:
            await safe_edit_message(
                query.message,
                text=(
                    f"All episodes for *{escape_markdown(str(title), version=2)}* "
                    f"S{int(season):02d} already exist in your library\\. Nothing to download\\."
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        have = len(existing_eps)
        total = int(episode_count)
        if have > 0:
            await safe_edit_message(
                query.message,
                text=(
                    f"Detected {have}/{total} episodes already in your library for *{escape_markdown(str(title), version=2)}* S{int(season):02d}\\.\n"
                    "I will only fetch the missing episodes in 720p/1080p\\."
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await safe_edit_message(
                query.message,
                text=(
                    f"🔍 Searching for a season pack or matching episodes for *{escape_markdown(str(title), version=2)}* S{int(season):02d} in 720p/1080p\\.\n"
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )

        session.allow_detail_change = False
        _save_session(context, session)
        await _prompt_tv_season_resolution(query.message, context, session)


async def _prompt_tv_season_resolution(
    message: Message, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    text = (
        "Choose the target resolution for this season pack or episode batch\\.\n"
        "This helps me bias torrent selection toward consistent releases\\."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "1080p", callback_data="search_tv_season_resolution_1080p"
                ),
                InlineKeyboardButton(
                    "2160p / 4K", callback_data="search_tv_season_resolution_2160p"
                ),
            ],
            [
                InlineKeyboardButton(
                    "Best Available", callback_data="search_tv_season_resolution_all"
                )
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
        ]
    )
    await safe_edit_message(
        message,
        text=text,
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_tv_season_resolution_button(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    if not isinstance(query.message, Message):
        return

    choice = _get_callback_data(query).split("_")[-1]
    if choice not in {"1080p", "2160p", "all"}:
        choice = "1080p"
    session.resolution = choice
    _save_session(context, session)
    await _prompt_tv_season_codec(query.message, context, session)


async def _prompt_tv_season_codec(
    message: Message, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    text = (
        "Select your preferred codec for this season\\.\n"
        'Choosing "Either" allows the best match per episode/pack\\.'
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "x264 / AVC", callback_data="search_tv_season_codec_x264"
                ),
                InlineKeyboardButton(
                    "x265 / HEVC", callback_data="search_tv_season_codec_x265"
                ),
            ],
            [
                InlineKeyboardButton(
                    "Either Codec", callback_data="search_tv_season_codec_any"
                )
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
        ]
    )
    await safe_edit_message(
        message,
        text=text,
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_tv_season_codec_button(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    if not isinstance(query.message, Message):
        return

    choice = _get_callback_data(query).split("_")[-1].lower()
    if choice not in COLLECTION_CODEC_CHOICES:
        choice = "any"
    session.tv_codec = choice
    _save_session(context, session)

    title = session.require_title()
    season = session.require_season()

    await _perform_tv_season_search(
        query.message,
        context,
        str(title),
        int(season),
        session=session,
    )


# --- Button Press Handlers ---


async def _handle_start_button(query, context):
    """Handles the initial 'Movie' or 'TV Show' button press."""
    store = _get_user_data_store(context)
    store["active_workflow"] = "search"
    clear_search_session(store)
    session = SearchSession()

    action = _get_callback_data(query)
    if action == "search_start_movie":
        session.media_type = "movie"
        session.movie_scope = None
        session.collection_mode = False
        session.collection_name = None
        session.collection_movies = []
        session.collection_exclusions = []
        session.advance(SearchStep.MOVIE_SCOPE)
        prompt_text = (
            "🎬 Are you searching for a single movie or an entire franchise collection?"
        )
        reply_markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Single Movie", callback_data="search_movie_scope_single"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Collection", callback_data="search_movie_scope_collection"
                    )
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
            ]
        )
    else:
        session.media_type = "tv"
        session.advance(SearchStep.TITLE)
        prompt_text = "📺 Please send me the title of the TV show to search for\\."
        reply_markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
        )

    session.prompt_message_id = query.message.message_id
    _save_session(context, session)

    await safe_edit_message(
        query.message,
        text=prompt_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_resolution_button(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
):
    """Handles the resolution selection and triggers the appropriate search."""
    if not isinstance(query.message, Message):
        return

    callback_data = _get_callback_data(query)
    if any(token in callback_data for token in ("2160p", "4k")):
        resolution = "2160p"
    else:
        resolution = "1080p"

    session.resolution = resolution
    _save_session(context, session)

    try:
        final_title = session.require_final_title()
        media_type = session.require_media_type()
    except SearchSessionError as exc:
        await safe_edit_message(
            query.message,
            text=exc.user_message,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        clear_search_session(context.user_data)
        return

    if media_type == "movie":
        await safe_edit_message(
            query.message,
            text=f"🔍 Searching all sources for *{escape_markdown(final_title, version=2)}* in *{resolution}*\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        year_match = re.search(r"\((\d{4})\)", final_title)
        year = year_match.group(1) if year_match else None
        search_title = final_title.split("(")[0].strip()

        # Allow size override for 4K
        max_size: float = float(MAX_TORRENT_SIZE_GB)
        if resolution == "2160p":
            max_size *= FOUR_K_SIZE_MULTIPLIER

        results = await search_logic.orchestrate_searches(
            search_title,
            "movie",
            context,
            year=year,
            resolution=resolution,
            max_size_gb=max_size,
        )
        await _present_search_results(
            query.message,
            context,
            results,
            f"{final_title} [{resolution}]",
            session=session,
            max_size_gb=MAX_TORRENT_SIZE_GB,
            initial_resolution=resolution,
        )
        return

    if media_type == "tv":
        tv_scope = session.tv_scope
        title = session.effective_title or session.title
        season = session.season
        if tv_scope == "single" and title:
            await safe_edit_message(
                query.message,
                text=f"🔍 Searching all sources for *{escape_markdown(final_title, version=2)}* in *{resolution}*\\.\\.\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            results = await search_logic.orchestrate_searches(
                final_title, "tv", context, base_query_for_filter=title
            )
            await _present_search_results(
                query.message,
                context,
                results,
                f"{final_title} [{resolution}]",
                session=session,
                initial_resolution=resolution,
            )
            return

        if tv_scope == "season" and title and season is not None:
            await safe_edit_message(
                query.message,
                text=(
                    f"🔍 Searching for Season {escape_markdown(str(season), version=2)} "
                    f"of *{escape_markdown(title, version=2)}* in *{resolution}*\\.\\.\\."
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            await _perform_tv_season_search(query.message, context, title, int(season))
            return

    await safe_edit_message(
        query.message,
        CONTEXT_LOST_MESSAGE,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    clear_search_session(context.user_data)


async def _handle_movie_scope_button(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    """Handles the movie scope selection between single and collection."""
    if not isinstance(query.message, Message):
        return

    scope = _get_callback_data(query).split("_")[-1]
    session.final_title = None
    session.title = None
    session.resolution = None
    session.collection_name = None
    session.collection_fs_name = None
    session.collection_movies = []
    session.collection_exclusions = []
    session.collection_resolution = None
    session.collection_codec = None
    session.collection_seed_size_gb = None
    session.collection_seed_uploader = None
    session.collection_owned_count = 0

    if scope == "single":
        session.movie_scope = "single"
        session.collection_mode = False
        session.advance(SearchStep.TITLE)
        prompt_text = "🎬 Please send me the title of the movie to search for \\(you can include the year\\)\\."
    else:
        session.movie_scope = "collection"
        session.collection_mode = True
        session.advance(SearchStep.TITLE)
        prompt_text = (
            "🎬 Send the title of a movie within the franchise you want to collect\\."
        )

    session.prompt_message_id = query.message.message_id
    _save_session(context, session)
    await safe_edit_message(
        query.message,
        text=prompt_text,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_collection_accept(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    """Prepares filesystem state and prompts for resolution selection."""
    if not isinstance(query.message, Message):
        return

    if not session.collection_movies:
        await safe_edit_message(
            query.message,
            text="⚠️ Collection data expired\\. Please send the franchise title again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        clear_search_session(context.user_data)
        return

    try:
        owned_count = await _prepare_collection_directory(context, session)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to prepare collection directory: %s", exc)
        await safe_edit_message(
            query.message,
            text="⚠️ Could not prepare the collection directory\\. Please try again later\\.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    session.collection_owned_count = owned_count
    session.prompt_message_id = query.message.message_id
    session.advance(SearchStep.RESOLUTION)
    _save_session(context, session)
    await _prompt_collection_resolution(query.message, context, session)


async def _prompt_collection_resolution(
    message: Message, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    owned_note = ""
    if session.collection_owned_count:
        owned_note = (
            f"\n📁 {session.collection_owned_count} film{'s' if session.collection_owned_count != 1 else ''} "
            "already exist in your library and will be skipped\\."
        )
    text = (
        "Choose the target resolution for this collection run\\."
        f"{owned_note}\n\nThis helps me bias torrent selection toward consistent releases\\."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "1080p", callback_data="search_collection_resolution_1080p"
                ),
                InlineKeyboardButton(
                    "2160p / 4K", callback_data="search_collection_resolution_2160p"
                ),
            ],
            [
                InlineKeyboardButton(
                    "Best Available", callback_data="search_collection_resolution_all"
                )
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
        ]
    )
    await safe_edit_message(
        message,
        text=text,
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_collection_resolution_button(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    if not isinstance(query.message, Message):
        return

    choice = _get_callback_data(query).split("_")[-1]
    if choice not in {"1080p", "2160p", "all"}:
        choice = "1080p"
    session.collection_resolution = choice
    _save_session(context, session)
    await _prompt_collection_codec(query.message, context, session)


async def _prompt_collection_codec(
    message: Message, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    text = (
        "Select your preferred codec for this collection\\.\n"
        'Choosing "Either" allows the best match per movie\\.'
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "x264 / AVC", callback_data="search_collection_codec_x264"
                ),
                InlineKeyboardButton(
                    "x265 / HEVC", callback_data="search_collection_codec_x265"
                ),
            ],
            [
                InlineKeyboardButton(
                    "Either Codec", callback_data="search_collection_codec_any"
                )
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
        ]
    )
    await safe_edit_message(
        message,
        text=text,
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_collection_codec_button(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    if not isinstance(query.message, Message):
        return

    choice = _get_callback_data(query).split("_")[-1].lower()
    if choice not in COLLECTION_CODEC_CHOICES:
        choice = "any"
    session.collection_codec = choice
    _save_session(context, session)
    await _render_collection_movie_picker(query.message, context, session)


async def _render_collection_movie_picker(
    message: Message, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    movies = session.collection_movies or []
    if not movies:
        await safe_edit_message(
            message,
            text="⚠️ Collection data expired\\. Please restart the workflow\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        clear_search_session(context.user_data)
        return

    chat_id = message.chat_id
    _update_collection_movie_status(context, session, chat_id)

    available = [
        m
        for m in movies
        if not m.get("owned")
        and not m.get("queued")
        and m["identifier"] not in session.collection_exclusions
    ]
    downloadable = [m for m in movies if not m.get("owned") and not m.get("queued")]
    owned_count = sum(1 for m in movies if m.get("owned"))
    queued_count = sum(1 for m in movies if m.get("queued"))
    franchise = session.collection_name or "Franchise"

    text_lines = [
        f"🎬 Preparing *{escape_markdown(franchise, version=2)}* collection\\.",
        "Tap a title to remove it from this run\\.",
        f"Ready to download: *{len(available)}* / {len(downloadable)} remaining movies\\.",
    ]
    if owned_count:
        text_lines.append(f"📁 Owned: {owned_count}")
    if queued_count:
        text_lines.append(f"⏳ Already queued: {queued_count}")
    if not downloadable:
        text_lines.append("Everything in this franchise already exists or is queued\\.")
    text = "\n".join(text_lines)

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for movie in movies:
        label = _format_collection_movie_label(movie)
        identifier = movie["identifier"]
        prefix = ""
        if movie.get("owned"):
            prefix = "📁 "
        elif movie.get("queued"):
            prefix = "⏳ "
        elif identifier in session.collection_exclusions:
            prefix = "🔴 "
        else:
            prefix = "🟢 "
        button_text = f"{prefix}{label}"
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    button_text,
                    callback_data=f"search_collection_toggle_{identifier}",
                )
            ]
        )

    if downloadable:
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    "✅ Confirm Selection", callback_data="search_collection_confirm"
                )
            ]
        )
    keyboard_rows.append(
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]
    )

    session.prompt_message_id = message.message_id
    _save_session(context, session)
    await safe_edit_message(
        message,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard_rows),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_collection_movie_toggle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    if not isinstance(query.message, Message):
        return

    identifier = _get_callback_data(query).split("search_collection_toggle_")[-1]
    movie_map = {m["identifier"]: m for m in session.collection_movies or []}
    movie = movie_map.get(identifier)
    if not movie or movie.get("owned") or movie.get("queued"):
        try:
            await query.answer(text="This title is already handled.", show_alert=False)
        except RuntimeError:
            pass
        return

    exclusions = set(session.collection_exclusions or [])
    if identifier in exclusions:
        exclusions.remove(identifier)
    else:
        exclusions.add(identifier)
    session.collection_exclusions = list(exclusions)
    _save_session(context, session)
    await _render_collection_movie_picker(query.message, context, session)


async def _handle_collection_confirm(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    if not isinstance(query.message, Message):
        return

    movies = [
        m
        for m in session.collection_movies or []
        if not m.get("owned")
        and not m.get("queued")
        and m["identifier"] not in (session.collection_exclusions or [])
    ]
    owned_summaries = await _collect_owned_collection_summaries(context, session)
    if not movies and not owned_summaries:
        await query.answer(
            text="Select at least one movie to continue.", show_alert=True
        )
        return

    await safe_edit_message(
        query.message,
        text="🔍 Gathering torrents for the selected movies…",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    if movies:
        pending, missing = await _collect_collection_torrents(
            query.message, context, session, movies
        )
        if not pending:
            await safe_edit_message(
                query.message,
                text="⚠️ I couldn't find suitable torrents for the selected movies\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
    else:
        pending = {"items": [], "franchise": _build_franchise_metadata(session)}
        missing = []

    if owned_summaries:
        pending["owned_summaries"] = owned_summaries

    pending_store = _get_user_data_store(context)
    pending_store["pending_collection_download"] = pending
    await _present_collection_download_confirmation(
        query.message, context, session, pending, missing
    )
    clear_search_session(context.user_data)


async def _handle_result_selection_button(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    session: SearchSession,
) -> None:
    """Handles the user selecting a specific torrent from the results list."""
    if not isinstance(query.message, Message):
        return

    _ensure_results_available(session)

    filtered_results = _compute_filtered_results(session)
    if not filtered_results:
        await safe_edit_message(
            query.message,
            "❌ No results match the current filters\\. Please adjust them or restart the search\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    data = _get_callback_data(query)
    try:
        choice_index = int(data.split("_")[2])
    except (ValueError, IndexError):
        await safe_edit_message(
            query.message,
            "❌ An error occurred with your selection\\. Please try again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if not (0 <= choice_index < len(filtered_results)):
        await safe_edit_message(
            query.message,
            "❌ This selection has expired\\. Please start the search again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        clear_search_session(context.user_data)
        return

    selected_result = filtered_results[choice_index]
    url_to_process = selected_result.get("page_url")
    info_url = selected_result.get("info_url")

    display_link = info_url or url_to_process
    logger.info(f"[SEARCH] User selected torrent: {display_link}")

    if not url_to_process:
        await safe_edit_message(
            query.message,
            "❌ Unable to open that result\\. Please choose another option\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    logger.info(
        f"User selected '{selected_result.get('title')}'. Passing to torrent_service: {url_to_process[:70]}"
    )

    # Prevent duplicate selections during the download handoff.
    clear_search_session(context.user_data)

    ti = await torrent_service.process_user_input(
        url_to_process, context, query.message, info_url=info_url
    )
    if not ti:
        return

    error_message, parsed_info = await validate_and_enrich_torrent(ti, query.message)
    if error_message or not parsed_info:
        return

    await send_confirmation_prompt(query.message, context, ti, parsed_info)


async def _handle_results_page_button(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    session: SearchSession,
) -> None:
    if not isinstance(query.message, Message):
        return

    _ensure_results_available(session)

    payload = _get_callback_data(query)
    try:
        target_page = int(payload.split("_")[-1])
    except (ValueError, IndexError):
        await safe_edit_message(
            query.message,
            "❌ Unable to change pages right now\\. Please try again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    session.results_page = max(target_page, 0)
    _save_session(context, session)
    await _render_results_view(query.message, context, session)


async def _handle_results_filter_button(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    session: SearchSession,
) -> None:
    if not isinstance(query.message, Message):
        return
    _ensure_results_available(session)
    allowed_filters = _get_allowed_resolution_filters(session)
    requested = _get_callback_data(query).split("_")[-1]
    normalized = _normalize_resolution_filter(requested)
    if normalized not in allowed_filters:
        try:
            await query.answer(
                text="This resolution is unavailable for this search.",
                show_alert=False,
            )
        except RuntimeError:
            pass
        return
    session.results_resolution_filter = normalized
    session.results_page = 0
    _save_session(context, session)
    await _render_results_view(query.message, context, session)


async def _handle_results_codec_filter_button(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    session: SearchSession,
) -> None:
    """Handles codec filter toggles in the search results view."""
    if not isinstance(query.message, Message):
        return

    _ensure_results_available(session)
    requested = _get_callback_data(query).split("_")[-1].lower()
    if requested not in {"all", "x264", "x265"}:
        try:
            await query.answer(
                text="Unsupported codec filter for these results.",
                show_alert=False,
            )
        except RuntimeError:
            pass
        return

    session.results_codec_filter = SearchSession.normalize_results_codec_filter(
        requested
    )
    session.results_page = 0
    _save_session(context, session)
    await _render_results_view(query.message, context, session)


async def _handle_year_selection_button(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    """Handles the user selecting a specific year from the presented options."""
    if not query:
        return

    payload = _get_callback_data(query)
    if not payload:
        logger.warning("Callback query received without data. Ignoring.")
        await query.answer()
        return

    if not isinstance(query.message, Message):
        logger.warning(
            "Could not process year selection: The associated message is inaccessible."
        )
        await query.answer(
            text="❌ Error: The original message could not be modified.",
            show_alert=True,
        )
        return

    try:
        title = session.require_title()
    except SearchSessionError as exc:
        await safe_edit_message(
            query.message,
            text=exc.user_message,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        clear_search_session(context.user_data)
        return

    try:
        selected_year = payload.split("_")[3]
        full_title = f"{title} ({selected_year})"
        logger.info(f"User selected year {selected_year} for title '{title}'.")
    except IndexError:
        logger.error(f"Could not parse year from callback data: {payload!r}")
        await safe_edit_message(
            query.message,
            text="❌ An error occurred with your selection\\. Please try again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    session.set_final_title(full_title)
    session.media_type = "movie"
    session.advance(SearchStep.RESOLUTION)
    _save_session(context, session)

    await _search_movie_results(query.message, context, session)


# --- Helper/UI Functions ---


async def _send_prompt(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    session: SearchSession | None = None,
):
    """Sends a conversational prompt and stores its ID for later cleanup."""
    prompt_message = await safe_send_message(
        context.bot,
        chat_id,
        text,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    if session is None:
        session = _get_session(context)
    session.prompt_message_id = prompt_message.message_id
    _save_session(context, session)


async def _prompt_for_year_selection(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    title: str,
    years: list[str],
    session: SearchSession | None = None,
) -> None:
    """
    Edits a message to ask the user to select a year from a list of options.
    """
    if context.user_data is None:
        context.user_data = {}

    escaped_title = escape_markdown(title, version=2)
    message_text = (
        f"Found multiple possible release years for `'{escaped_title}'`\\. "
        f"Please select the correct one to continue:"
    )

    # Create a button for each year found
    keyboard = [
        [InlineKeyboardButton(year, callback_data=f"search_select_year_{year}")]
        for year in years
    ]
    keyboard.append(
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]
    )
    reply_markup = InlineKeyboardMarkup(keyboard)

    await safe_edit_message(
        message,
        text=message_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    if session is None:
        session = _get_session(context)
    session.prompt_message_id = message.message_id
    _save_session(context, session)


async def _present_season_download_confirmation(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    found_torrents: list[dict[str, Any]],
    session: SearchSession | None = None,
    *,
    consistency_summary: SeasonConsistencySummary | None = None,
) -> None:
    """Summarizes season search results and asks for confirmation."""
    if session is None:
        session = _get_session(context)
    season = session.season
    total_eps = session.season_episode_count

    if not found_torrents:
        await safe_edit_message(
            message,
            text="❌ No torrents found for this season.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    is_pack = len(found_torrents) == 1 and found_torrents[0].get("parsed_info", {}).get(
        "is_season_pack"
    )

    existing = set(session.existing_episodes or [])
    missing_targets = session.missing_episode_numbers
    if isinstance(missing_targets, list) and missing_targets:
        target_total = len(missing_targets)
    else:
        target_total = int(total_eps or 0)

    if is_pack and existing:
        is_pack = False

    if is_pack:
        summary_text = escape_markdown(
            f"Found a season pack for Season {season}.", version=2
        )
    else:
        summary_text = escape_markdown(
            f"Found torrents for {len(found_torrents)} of {target_total} episode(s) "
            f"in Season {season}.",
            version=2,
        )

    extra_summary = _format_consistency_summary(consistency_summary)
    lines = [summary_text]
    if extra_summary:
        lines.append("")
        lines.append(extra_summary)
    rendered_summary = "\n".join(lines)

    if is_pack:
        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Confirm", callback_data="confirm_season_download"
                ),
                InlineKeyboardButton("⛔ Reject", callback_data="reject_season_pack"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation"),
            ]
        ]
    else:
        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Confirm", callback_data="confirm_season_download"
                ),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation"),
            ]
        ]

    await safe_edit_message(
        message,
        text=rendered_summary,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    store = _get_user_data_store(context)
    store["pending_season_download"] = found_torrents


def _normalize_release_field(value: Any, default: str) -> str:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or default
    return default


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _infer_resolution_from_title(title: Any) -> str | None:
    if not isinstance(title, str):
        return None
    lowered = title.lower()
    if "2160p" in lowered or "4k" in lowered or "uhd" in lowered:
        return "2160p"
    if "1080p" in lowered:
        return "1080p"
    if "720p" in lowered:
        return "720p"
    return None


def _target_size_for_resolution(resolution: str | None) -> float:
    if not resolution:
        return DEFAULT_SIZE_TARGET_GB
    return RESOLUTION_SIZE_TARGETS.get(resolution, DEFAULT_SIZE_TARGET_GB)


def _select_consistent_episode_set(
    candidates_by_episode: dict[int, list[EpisodeCandidate]],
) -> tuple[list[EpisodeCandidate], SeasonConsistencySummary | None]:
    if not candidates_by_episode:
        return [], None

    episodes = sorted(candidates_by_episode)
    default_selection = [candidates_by_episode[ep][0] for ep in episodes]

    release_keys: set[tuple[str, str]] = set()
    for candidates in candidates_by_episode.values():
        for candidate in candidates:
            release_keys.add(candidate.release_key)

    total_eps = len(episodes)
    best_entry: (
        tuple[
            tuple[str, str],
            float,
            list[EpisodeCandidate],
            float | None,
            float,
            str | None,
        ]
        | None
    ) = None

    for key in release_keys:
        matched: list[EpisodeCandidate] = []
        sizes: list[float] = []
        resolution_counter: Counter[str] = Counter()
        for ep in episodes:
            matched_candidate: EpisodeCandidate | None = next(
                (cand for cand in candidates_by_episode[ep] if cand.release_key == key),
                None,
            )
            if matched_candidate is None:
                continue
            matched.append(matched_candidate)
            if matched_candidate.size_gb is not None:
                sizes.append(matched_candidate.size_gb)
            if matched_candidate.resolution:
                resolution_counter[matched_candidate.resolution] += 1

        if not matched:
            continue

        coverage_ratio = len(matched) / total_eps
        avg_size = sum(sizes) / len(sizes) if sizes else None
        spread = (max(sizes) - min(sizes)) if len(sizes) > 1 else 0.0
        primary_resolution = (
            resolution_counter.most_common(1)[0][0] if resolution_counter else None
        )
        target_size = _target_size_for_resolution(primary_resolution)
        size_deviation = abs(avg_size - target_size) if avg_size is not None else 0.0
        score = (
            coverage_ratio * 100
            - spread * SIZE_VARIANCE_WEIGHT
            - size_deviation * SIZE_DEVIATION_WEIGHT
        )
        if best_entry is None or score > best_entry[1]:
            best_entry = (
                key,
                score,
                matched,
                avg_size,
                float(spread),
                primary_resolution,
            )

    if best_entry is None:
        logger.info(
            "[SEARCH] No consistent release found across episodes. Using top results."
        )
        return default_selection, None

    release_key, score, matched, avg_size, spread, resolution = best_entry
    logger.info(
        "[SEARCH] Best consistent release: %s via %s (score: %.1f, coverage: %d/%d)",
        release_key[1],
        release_key[0],
        score,
        len(matched),
        total_eps,
    )
    matched_map = {cand.episode: cand for cand in matched}
    final_selection: list[EpisodeCandidate] = []
    fallback_eps: list[int] = []
    for ep in episodes:
        selected: EpisodeCandidate | None = matched_map.get(ep)
        if selected is None:
            available = candidates_by_episode.get(ep, [])
            keys_found = [c.release_key for c in available]
            logger.info(
                "[SEARCH] E%02d missing from consistent set %s. Available candidates: %s",
                ep,
                release_key,
                keys_found,
            )
            fallback_candidate = candidates_by_episode[ep][0]
            selected = fallback_candidate
            fallback_eps.append(ep)
        assert selected is not None
        final_selection.append(selected)

    summary = SeasonConsistencySummary(
        release_source=release_key[0],
        release_uploader=release_key[1],
        avg_size_gb=avg_size,
        size_spread_gb=spread,
        matched_count=len(matched),
        total_count=total_eps,
        fallback_episodes=fallback_eps,
        resolution=resolution,
    )
    return final_selection, summary


def _format_consistency_summary(
    summary: SeasonConsistencySummary | None,
) -> str | None:
    if summary is None or summary.matched_count == 0:
        return None

    label = escape_markdown(summary.release_label().split()[-1], version=2)
    coverage = f"{summary.matched_count}/{summary.total_count}"
    details: list[str] = []
    if summary.resolution:
        details.append(
            "\nResolution: " + escape_markdown(summary.resolution.lower(), version=2)
        )
    if summary.avg_size_gb is not None:
        details.append(
            escape_markdown(f"\nAverage size: {summary.avg_size_gb:.2f} GB", version=2)
        )
        total_size = escape_markdown(
            str(round(float(summary.avg_size_gb) * float(summary.matched_count), 2)),
            version=2,
        )
        details.append(escape_markdown(f"\nTotal Size: {total_size} GB"))
    base_line = f"Consistency: {label}" f" \\({escape_markdown(coverage, version=2)}\\)"
    if details:
        base_line += " ".join(details)

    lines = [base_line]
    if summary.fallback_episodes:
        fallback_str = ", ".join(f"E{num:02d}" for num in summary.fallback_episodes)
        lines.append(f"⚠️ Fallback episodes: {escape_markdown(fallback_str, version=2)}")
    return "\n".join(lines)


async def _prompt_for_resolution(
    target: Message | int,
    context: ContextTypes.DEFAULT_TYPE,
    full_title: str,
    *,
    media_type: Literal["movie", "tv"] = "movie",
    session: SearchSession | None = None,
    detected_context: str | None = None,
    allow_detail_change: bool = False,
) -> None:
    """Asks the user to select a resolution, optionally surfacing detection hints."""
    if session is None:
        session = _get_session(context)

    session.set_final_title(full_title)
    session.media_type = media_type
    session.advance(SearchStep.RESOLUTION)
    _save_session(context, session)

    if media_type == "tv":
        keyboard_rows = [
            [
                InlineKeyboardButton(
                    "💎 1080p", callback_data="search_resolution_1080p"
                ),
                InlineKeyboardButton("💩 720p", callback_data="search_resolution_720p"),
            ]
        ]
    else:
        keyboard_rows = [
            [
                InlineKeyboardButton(
                    "🪙 1080p", callback_data="search_resolution_1080p"
                ),
                InlineKeyboardButton(
                    "💎 4K (2160p)", callback_data="search_resolution_4k"
                ),
            ]
        ]
    if allow_detail_change and media_type == "tv":
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    "🔄️ Change", callback_data="search_tv_change_details"
                )
            ]
        )
    keyboard_rows.append(
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]
    )
    reply_markup = InlineKeyboardMarkup(keyboard_rows)

    text_prompt = f"Got it: `{escape_markdown(full_title, version=2)}`\\. Now, please select your desired resolution:"
    if detected_context:
        text_prompt = f"{text_prompt}"

    if isinstance(target, Message):
        await safe_edit_message(
            target,
            text=text_prompt,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    elif isinstance(target, int):
        prompt_message = await safe_send_message(
            context.bot,
            chat_id=target,
            text=text_prompt,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        session.prompt_message_id = prompt_message.message_id
        _save_session(context, session)


async def _search_movie_results(
    target: Message | int,
    context: ContextTypes.DEFAULT_TYPE,
    session: SearchSession,
) -> None:
    """Searches for movie torrents across 1080p and 4K in a single scrape."""
    try:
        final_title = session.require_final_title()
    except SearchSessionError as exc:
        if isinstance(target, Message):
            await safe_edit_message(
                target,
                text=exc.user_message,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await safe_send_message(
                context.bot,
                chat_id=target,
                text=exc.user_message,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        clear_search_session(context.user_data)
        return

    display_title = escape_markdown(final_title, version=2)
    progress_text = (
        f"🔍 Searching all sources for *{display_title}* in 1080p and 4K\\.\\.\\."
    )

    if isinstance(target, Message):
        status_message = target
        await safe_edit_message(
            status_message,
            text=progress_text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        status_message = await safe_send_message(
            context.bot,
            chat_id=target,
            text=progress_text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    search_title = final_title.split("(")[0].strip() or final_title
    year_match = re.search(r"\((\d{4})\)", final_title)
    year = year_match.group(1) if year_match else None

    combined_results: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for resolution in ("1080p", "2160p"):
        # Allow size override for 4K
        max_size: float = float(MAX_TORRENT_SIZE_GB)
        if resolution == "2160p":
            max_size *= FOUR_K_SIZE_MULTIPLIER

        results = await search_logic.orchestrate_searches(
            search_title,
            "movie",
            context,
            year=year,
            resolution=resolution,
            max_size_gb=max_size,
        )
        for item in results or []:
            key = item.get("page_url") or item.get("magnet") or item.get("title")
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            combined_results.append(item)

    await _present_search_results(
        status_message,
        context,
        combined_results,
        f"{final_title} [All]",
        session=session,
        max_size_gb=MAX_TORRENT_SIZE_GB,
        initial_resolution="all",
    )


async def _search_tv_single_results(
    target: Message | int,
    context: ContextTypes.DEFAULT_TYPE,
    session: SearchSession,
    *,
    notice: str | None = None,
) -> None:
    """Searches for a single TV episode without requiring a resolution prompt."""
    try:
        final_title = session.require_final_title()
    except SearchSessionError as exc:
        if isinstance(target, Message):
            await safe_edit_message(
                target,
                text=exc.user_message,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await safe_send_message(
                context.bot,
                chat_id=target,
                text=exc.user_message,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        clear_search_session(context.user_data)
        return

    base_title = session.effective_title or session.title or ""
    display_title = escape_markdown(final_title, version=2)
    status_lines = []
    if notice:
        status_lines.append(notice)
    status_lines.append(
        f"🔎 Searching all sources for *{display_title}* in 720p and 1080p\\.\\.\\."
    )
    progress_text = "\n".join(status_lines)

    if isinstance(target, Message):
        status_message = target
        await safe_edit_message(
            status_message,
            text=progress_text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        status_message = await safe_send_message(
            context.bot,
            chat_id=target,
            text=progress_text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    results = await search_logic.orchestrate_searches(
        final_title,
        "tv",
        context,
        base_query_for_filter=base_title or None,
    )

    await _present_search_results(
        status_message,
        context,
        results,
        f"{final_title} [All]",
        session=session,
        initial_resolution="all",
    )


async def _perform_tv_season_search(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    title: str,
    season: int,
    *,
    force_individual_episodes: bool = False,
    session: SearchSession | None = None,
) -> None:
    """
    Searches for a TV season pack or individual episodes, respecting session resolution/codec.
    On success, presents a confirmation summary to queue the season download.
    """
    if session is None:
        session = _get_session(context)

    target_res = session.resolution or "all"
    target_codec = session.tv_codec or "all"

    # Propagate filters to session state for _present_search_results usage
    session.results_resolution_filter = target_res
    session.results_codec_filter = SearchSession.normalize_results_codec_filter(
        target_codec
    )
    _save_session(context, session)

    season_queries = [f"{title} S{season:02d}", f"{title} Season {season}"]
    found_results: list[dict[str, Any]] = []

    # Add resolution hints to query if specific
    if target_res in ("1080p", "2160p"):
        season_queries = [f"{q} {target_res}" for q in season_queries] + season_queries

    for q in season_queries:
        res = await search_logic.orchestrate_searches(
            q, "tv", context, base_query_for_filter=title
        )
        if res:
            found_results.extend(res)
        if len(found_results) >= 5:
            break

    existing_owned = set(session.existing_episodes or [])
    must_individual = bool(force_individual_episodes or existing_owned)

    season_pack_torrent = None
    pack_candidates = []
    if not must_individual:
        season_token = f"s{season:02d} "
        for item in found_results:
            title_lower = item.get("title", "").lower()
            if (
                any(k in title_lower for k in ["complete", "collection", "season pack"])
                or season_token in title_lower
            ) and not re.search(r"s\d{1,2}e\d{1,2}", title_lower):
                pack_candidates.append(item)

        # Filter pack candidates by resolution/codec if possible
        filtered_packs = _filter_results_by_resolution(pack_candidates, target_res)
        if target_codec != "all":
            filtered_packs = [
                p
                for p in filtered_packs
                if (p.get("codec") or "").lower() == target_codec.lower()
            ]

        # Fallback to unfiltered if strict filtering yields nothing
        candidates_to_use = filtered_packs if filtered_packs else pack_candidates

        season_pack_torrent = (
            max(candidates_to_use, key=lambda x: x.get("score", 0))
            if candidates_to_use
            else None
        )

    if season_pack_torrent and not must_individual:
        await _present_search_results(
            message,
            context,
            pack_candidates,  # Pass all candidates, the UI will filter via session settings
            f"{title} S{int(season):02d} [All]",
            session=session,
            initial_resolution=target_res,
        )
        return

    episode_count = int(session.season_episode_count or 0)
    raw_targets = session.missing_episode_numbers
    if isinstance(raw_targets, list) and raw_targets:
        targets = list(raw_targets)
    elif isinstance(raw_targets, list):
        await safe_edit_message(
            message,
            text=(
                f"All episodes for *{escape_markdown(title, version=2)}* "
                f"S{int(season):02d} already exist in your library\\."
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    else:
        targets = list(range(1, episode_count + 1))

    titles_map: dict[int, dict[str, Any]] = {}
    corrected_title: str | None = None
    try:
        (
            titles_map,
            corrected_title,
        ) = await scraping_service.fetch_episode_titles_for_season(title, season)
    except Exception:
        titles_map, corrected_title = {}, None

    # Filter out episodes that haven't been released yet
    current_date = datetime.now().date().isoformat()
    filtered_targets = []
    for ep in targets:
        meta = titles_map.get(ep)
        if meta and meta.get("release_date"):
            if meta["release_date"] > current_date:
                logger.info(
                    "[SEARCH] Skipping unreleased episode %s S%02dE%02d (Released: %s)",
                    title,
                    season,
                    ep,
                    meta["release_date"],
                )
                continue
        filtered_targets.append(ep)

    if targets and not filtered_targets:
        logger.warning(
            "[SEARCH] All requested episodes for %s S%02d are unreleased.",
            title,
            season,
        )
        await safe_edit_message(
            message,
            text=f"❌ No episodes for *{escape_markdown(title, version=2)}* S{int(season):02d} have been released yet or could be verified\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    targets = filtered_targets

    episode_candidates: dict[int, list[EpisodeCandidate]] = {}
    missing_candidates: list[int] = []
    processed_eps = 0

    def _progress_text(last_ep: int | None) -> str:
        base = (
            f"🔎 Searching for Season {escape_markdown(str(season), version=2)} "
            f"of *{escape_markdown(title, version=2)}* in {escape_markdown(target_res, version=2)}\\.\\.\\."
        )
        total_targets = len(targets) if targets else episode_count
        if total_targets:
            return base + f"\nProgress: {processed_eps}/{total_targets}"
        return base

    for ep in targets:
        search_term = f"{title} S{season:02d}E{ep:02d}"
        # Hint resolution in query
        if target_res in ("1080p", "2160p"):
            search_term += f" {target_res}"

        ep_results = await search_logic.orchestrate_searches(
            search_term, "tv", context, base_query_for_filter=title
        )

        if LOG_SCRAPER_STATS:
            _log_aggregated_results(search_term, ep_results)

        # Apply Filters
        filtered_eps = _filter_results_by_resolution(ep_results, target_res)
        if target_codec != "all":
            filtered_eps = [
                r
                for r in filtered_eps
                if (r.get("codec") or "").lower() == target_codec.lower()
            ]

        # If strict filtering yields nothing, fallback to relaxed
        if not filtered_eps:
            filtered_eps = ep_results

        normalized_candidates: list[EpisodeCandidate] = []
        if filtered_eps:
            for raw in filtered_eps:
                link = raw.get("page_url")
                if not link:
                    continue
                candidate = EpisodeCandidate(
                    episode=ep,
                    link=link,
                    title=str(raw.get("title", "")),
                    source=_normalize_release_field(raw.get("source"), "Unknown"),
                    uploader=_normalize_release_field(raw.get("uploader"), "Anonymous"),
                    info_url=raw.get("info_url"),
                    size_gb=_coerce_float(raw.get("size_gb")),
                    seeders=_coerce_int(raw.get("seeders")),
                    resolution=_infer_resolution_from_title(raw.get("title")),
                    codec=raw.get("codec"),
                    score=_coerce_float(raw.get("score")),
                )
                normalized_candidates.append(candidate)
                if len(normalized_candidates) >= EPISODE_CANDIDATE_LIMIT:
                    break
        if normalized_candidates:
            episode_candidates[ep] = normalized_candidates
        else:
            missing_candidates.append(ep)
        processed_eps += 1
        await safe_edit_message(
            message,
            text=_progress_text(ep),
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    if missing_candidates:
        logger.warning(
            "[SEARCH] No torrents found for %s S%02d episodes: %s",
            title,
            season,
            ", ".join(f"E{num:02d}" for num in missing_candidates),
        )

    torrents_to_queue: list[dict[str, Any]] = []
    consistency_summary: SeasonConsistencySummary | None = None
    selected_candidates: list[EpisodeCandidate] = []
    if episode_candidates:
        selected_candidates, consistency_summary = _select_consistent_episode_set(
            episode_candidates
        )

    for candidate in selected_candidates:
        fallback_tag = ""
        if (
            consistency_summary
            and candidate.episode in consistency_summary.fallback_episodes
        ):
            # Check if it's a true fallback (codec/res mismatch) or just different uploader
            res_mismatch = (
                target_res != "all"
                and (candidate.resolution or "").lower() != target_res.lower()
            )
            codec_mismatch = (
                target_codec != "all"
                and (candidate.codec or "").lower() != target_codec.lower()
            )

            if res_mismatch or codec_mismatch:
                fallback_tag = " (FALLBACK)"
            else:
                fallback_tag = " (DIFFERENT_UPLOADER)"

        display_link = candidate.info_url or candidate.link
        logger.info(
            "[SEARCH] Selected torrent for E%02d: %s%s",
            candidate.episode,
            display_link,
            fallback_tag,
        )
        parsed_info = parse_torrent_name(candidate.title)
        parsed_info["title"] = corrected_title or title
        parsed_info["season"] = season
        parsed_info["episode"] = candidate.episode
        parsed_info["type"] = "tv"
        meta = titles_map.get(candidate.episode)
        parsed_info["episode_title"] = meta.get("title") if meta else None
        torrents_to_queue.append(
            {
                "link": candidate.link,
                "parsed_info": parsed_info,
                "info_url": candidate.info_url,
                "source": candidate.source,
                "uploader": candidate.uploader,
                "size_gb": candidate.size_gb,
                "resolution": candidate.resolution,
            }
        )

    if consistency_summary and consistency_summary.fallback_episodes:
        logger.warning(
            "[SEARCH] Mixed sources for %s S%02d due to missing releases on %s.",
            title,
            season,
            consistency_summary.release_label(),
        )

    await _present_season_download_confirmation(
        message,
        context,
        torrents_to_queue,
        session=session,
        consistency_summary=consistency_summary,
    )


async def handle_reject_season_pack(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handles rejection of a season pack by collecting single episodes instead."""
    query = update.callback_query
    if not query or not isinstance(query.message, Message):
        return

    session = _get_session(context)
    title = session.effective_title or session.title
    season = session.season
    if not title or season is None:
        await safe_edit_message(
            query.message,
            text=CONTEXT_LOST_MESSAGE,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        clear_search_session(context.user_data)
        return

    prefix = escape_markdown(
        "⛔ Rejected season pack\\. Collecting single episodes for ", version=2
    )
    title_md = escape_markdown(str(title), version=2)
    message_text = f"{prefix}*{title_md}* S{int(season):02d}."
    await safe_edit_message(
        query.message,
        text=message_text,
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    await _perform_tv_season_search(
        query.message,
        context,
        str(title),
        int(season),
        force_individual_episodes=True,
        session=session,
    )


# --- Collection utilities ---


async def _prepare_collection_directory(
    context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> int:
    """Checks for existing movies in the library to mark them as owned."""
    movies_root, _ = _resolve_collection_paths(
        context, session.collection_name, session.collection_fs_name
    )

    owned = 0
    for movie in session.collection_movies or []:
        label = _format_collection_movie_label(movie)
        # Check if the movie already exists in the main movies directory
        existing = await asyncio.to_thread(
            _locate_existing_movie_entry, movies_root, label
        )
        movie["owned"] = bool(existing)
        if existing:
            owned += 1
    _save_session(context, session)
    return owned


async def _ensure_existing_movie_in_collection(
    root_path: str, franchise_dir: str, label: str
) -> bool:
    """Moves an existing movie file/folder into the collection directory if present."""
    existing = await asyncio.to_thread(
        _locate_existing_movie_entry, franchise_dir, label
    )
    if existing:
        return await _flatten_movie_entry(existing, franchise_dir)

    existing = await asyncio.to_thread(_locate_existing_movie_entry, root_path, label)
    if not existing:
        return False

    return await _flatten_movie_entry(existing, franchise_dir)


def _resolve_collection_paths(
    context: ContextTypes.DEFAULT_TYPE,
    collection_name: str | None,
    collection_fs_name: str | None,
) -> tuple[str, str]:
    bot_data = context.bot_data or {}
    save_paths = bot_data.get("SAVE_PATHS", {})
    movies_root = save_paths.get("movies") or save_paths.get("default")
    if not movies_root:
        raise RuntimeError("Movies path is not configured.")

    final_name = collection_fs_name or collection_name or "Collection"
    safe_name = _sanitize_collection_name(final_name)
    franchise_dir = os.path.join(movies_root, safe_name)
    return movies_root, franchise_dir


async def finalize_movie_collection(
    context: ContextTypes.DEFAULT_TYPE, collection_meta: dict[str, Any]
) -> None:
    """
    Finalizes a movie collection run by creating the franchise directory
    and moving all associated movies (new and existing) into it.
    """
    try:
        collection_name = collection_meta.get("name")
        collection_fs_name = collection_meta.get("fs_name")
        movies = collection_meta.get("movies") or []

        movies_root, franchise_dir = _resolve_collection_paths(
            context, collection_name, collection_fs_name
        )

        # 1. Create the directory only now
        await asyncio.to_thread(os.makedirs, franchise_dir, exist_ok=True)

        # 2. Find and move every movie in the collection
        for movie in movies:
            label = _format_collection_movie_label(movie)
            # Check root first
            existing = await asyncio.to_thread(
                _locate_existing_movie_entry, movies_root, label
            )
            if existing:
                await _flatten_movie_entry(existing, franchise_dir)
            else:
                # Check if it's already there (shouldn't hurt)
                existing = await asyncio.to_thread(
                    _locate_existing_movie_entry, franchise_dir, label
                )
                if existing:
                    await _flatten_movie_entry(existing, franchise_dir)

        logger.info(
            "[COLLECTION] Finalized reorganization for '%s' into %s",
            collection_name,
            franchise_dir,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("[COLLECTION] Reorganization failed: %s", exc)


async def _collect_owned_collection_summaries(
    context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> list[str]:
    owned_movies = [m for m in session.collection_movies or [] if m.get("owned")]
    if not owned_movies:
        return []

    try:
        movies_root, franchise_dir = _resolve_collection_paths(
            context, session.collection_name, session.collection_fs_name
        )
    except RuntimeError:
        return []

    summaries: list[str] = []
    # plex_entries will be handled during finalization once files are actually moved
    for movie in owned_movies:
        label = _format_collection_movie_label(movie)

        # Locate where it is currently
        entry_path = await asyncio.to_thread(
            _locate_existing_movie_entry, movies_root, label
        )

        if not entry_path:
            # Fallback: maybe it's already in the franchise dir (manual or previous run)
            entry_path = await asyncio.to_thread(
                _locate_existing_movie_entry, franchise_dir, label
            )

        size_label: str | None = None
        if entry_path:
            size_bytes = _get_path_size_bytes(entry_path)
            if size_bytes:
                size_label = format_bytes(size_bytes)

        summaries.append(
            format_media_summary(
                prefix="✅ *Already Available*",
                title=label,
                size_label=size_label,
                destination_label=franchise_dir,
                title_icon="🎬",
                size_icon="📦",
                destination_icon="📁",
            )
        )
    return summaries


def _locate_existing_movie_entry(root_path: str, label: str) -> str | None:
    """Finds a directory or file in the movies root that matches the target folder."""
    if not root_path or not os.path.isdir(root_path):
        return None
    normalized = _normalize_label(label)
    if not normalized:
        return None
    try:
        for entry in os.listdir(root_path):
            entry_path = os.path.join(root_path, entry)
            entry_key = _normalize_label(entry)
            if entry_key == normalized:
                return entry_path
            stem, _ = os.path.splitext(entry)
            if stem and _normalize_label(stem) == normalized:
                return entry_path
    except FileNotFoundError:
        return None
    return None


async def _flatten_movie_entry(existing: str, franchise_dir: str) -> bool:
    """Moves/normalizes a movie entry so it lives directly under the franchise folder."""
    try:
        if os.path.isdir(existing):
            entries = os.listdir(existing)
            for entry in entries:
                source_path = os.path.join(existing, entry)
                target_path = os.path.join(franchise_dir, entry)
                if os.path.abspath(source_path) == os.path.abspath(target_path):
                    continue
                await asyncio.to_thread(shutil.move, source_path, target_path)
            if entries:
                await asyncio.to_thread(shutil.rmtree, existing, ignore_errors=True)
            return True

        parent_dir = os.path.dirname(existing)
        if os.path.abspath(parent_dir) == os.path.abspath(franchise_dir):
            return True

        target_path = os.path.join(franchise_dir, os.path.basename(existing))
        await asyncio.to_thread(shutil.move, existing, target_path)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed moving '%s' into collection folder: %s", existing, exc)
        return False


def _update_collection_movie_status(
    context: ContextTypes.DEFAULT_TYPE, session: SearchSession, chat_id: int
) -> None:
    """Marks movies as queued if they already exist in the active queue."""
    bot_data = context.bot_data or {}
    active_downloads = bot_data.get("active_downloads", {})
    download_queues = bot_data.get("download_queues", {})
    chat_id_str = str(chat_id)

    parsed_entries: list[dict[str, Any]] = []
    active_entry = active_downloads.get(chat_id_str)
    if isinstance(active_entry, dict):
        parsed = (active_entry.get("source_dict", {}) or {}).get("parsed_info")
        if isinstance(parsed, dict):
            parsed_entries.append(parsed)

    for queued in download_queues.get(chat_id_str, []):
        parsed = (queued.get("source_dict", {}) or {}).get("parsed_info")
        if isinstance(parsed, dict):
            parsed_entries.append(parsed)

    franchise_name = session.collection_name
    for movie in session.collection_movies or []:
        title_norm = _normalize_label(movie.get("title"))
        year_value = movie.get("year")
        movie["queued"] = any(
            _movie_matches(parsed, title_norm, year_value, franchise_name)
            for parsed in parsed_entries
        )


def _movie_matches(
    parsed_info: dict[str, Any],
    title_norm: str,
    year_value: int | None,
    franchise_name: str | None,
) -> bool:
    if parsed_info.get("type") != "movie":
        return False
    parsed_title = _normalize_label(parsed_info.get("title"))
    if parsed_title != title_norm:
        return False
    parsed_year = parsed_info.get("year")
    try:
        parsed_year_int = int(parsed_year) if parsed_year is not None else None
    except (TypeError, ValueError):
        parsed_year_int = None
    if isinstance(year_value, int) and parsed_year_int is not None:
        if parsed_year_int != year_value:
            return False
    collection_meta = parsed_info.get("collection") or {}
    if franchise_name and collection_meta.get("name") == franchise_name:
        return True
    return True


async def _collect_collection_torrents(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    session: SearchSession,
    movies: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Searches and selects torrent entries for the chosen movies."""
    pending_items: list[dict[str, Any]] = []
    missing: list[str] = []
    total = len(movies)

    for idx, movie in enumerate(movies, 1):
        label = _format_collection_movie_label(movie)
        await safe_edit_message(
            message,
            text=(
                f"🔍 Searching for *{escape_markdown(label, version=2)}* "
                f"\\({idx}/{total}\\)…"
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )

        year_value = movie.get("year")
        year_kw = str(year_value) if isinstance(year_value, int) else None

        # Allow size override for 4K
        max_size: float = float(MAX_TORRENT_SIZE_GB)
        if session.collection_resolution == "2160p":
            max_size *= FOUR_K_SIZE_MULTIPLIER

        results = await search_logic.orchestrate_searches(
            label, "movie", context, year=year_kw, max_size_gb=max_size
        )

        if LOG_SCRAPER_STATS:
            _log_aggregated_results(label, results)

        candidate = _pick_collection_candidate(
            results,
            session.collection_resolution,
            session.collection_codec,
            session.collection_seed_size_gb,
            session.collection_seed_uploader,
        )
        if not candidate:
            missing.append(label)
            continue
        link = candidate.get("page_url")
        if not link:
            missing.append(label)
            continue

        size_value = _coerce_float(candidate.get("size_gb"))
        if session.collection_seed_size_gb is None and size_value is not None:
            session.collection_seed_size_gb = size_value
        uploader_value = _normalize_release_field(
            candidate.get("uploader"), "Anonymous"
        )
        if (
            session.collection_seed_uploader is None
            and uploader_value
            and uploader_value != "Anonymous"
        ):
            session.collection_seed_uploader = uploader_value

        parsed_info = parse_torrent_name(candidate.get("title", ""))
        parsed_info["type"] = "movie"
        parsed_info["title"] = movie.get("title")
        if year_value is not None:
            parsed_info["year"] = year_value
        parsed_info["collection"] = {
            "name": session.collection_name,
            "fs_name": session.collection_fs_name,
            "resolution": session.collection_resolution,
            "codec": session.collection_codec,
        }

        pending_items.append(
            {
                "link": link,
                "parsed_info": parsed_info,
                "info_url": candidate.get("info_url"),
                "source": candidate.get("source"),
                "uploader": candidate.get("uploader"),
                "size_gb": candidate.get("size_gb"),
                "resolution": session.collection_resolution,
                "movie": {
                    "title": movie.get("title"),
                    "year": movie.get("year"),
                },
            }
        )

    pending_payload = {
        "items": pending_items,
        "franchise": _build_franchise_metadata(session),
    }
    _save_session(context, session)
    return pending_payload, missing


def _build_franchise_metadata(session: SearchSession) -> dict[str, Any]:
    return {
        "name": session.collection_name,
        "fs_name": session.collection_fs_name,
        "resolution": session.collection_resolution,
        "codec": session.collection_codec,
        "movies": [
            {"title": movie.get("title"), "year": movie.get("year")}
            for movie in session.collection_movies or []
        ],
    }


def _pick_collection_candidate(
    results: list[dict[str, Any]],
    preferred_resolution: str | None,
    preferred_codec: str | None,
    template_size: float | None,
    template_uploader: str | None,
) -> dict[str, Any] | None:
    """
    Selects the best candidate using a strict tiered fallback hierarchy.

    Hierarchy:
    1. Requested Resolution + Requested Codec
    2. Requested Resolution + Alternate Codec (Codec is the 1st compromise)
    3. Fallback Resolution + Requested Codec
    4. Fallback Resolution + Alternate Codec
    5. Best in Requested Resolution (Any Codec)
    6. Best Overall (Score-based)
    """
    if not results:
        return None

    target_res = _normalize_resolution_filter(preferred_resolution or "all")
    target_codec = (preferred_codec or "any").lower()

    # Define fallbacks
    fallback_res: str | None = None
    if target_res == "2160p":
        fallback_res = "1080p"
    elif target_res == "1080p":
        fallback_res = "720p"

    alt_codec: str | None = None
    if target_codec == "x265":
        alt_codec = "x264"
    elif target_codec == "x264":
        alt_codec = "x265"

    def _score(item: dict[str, Any]) -> float:
        base_score = float(item.get("score") or 0)

        # Bias toward uploader consistency
        uploader_val = _normalize_release_field(item.get("uploader"), "Anonymous")
        if template_uploader and uploader_val == template_uploader:
            base_score += 15

        # Reward size consistency
        size_val = _coerce_float(item.get("size_gb"))
        if template_size and size_val:
            try:
                deviation = abs(size_val - template_size) / template_size
            except ZeroDivisionError:
                deviation = 1.0

            if deviation <= 0.1:
                base_score += 10
            else:
                base_score -= deviation * 5

        # Tie breaker
        seeders = _coerce_int(item.get("seeders")) or 0
        base_score += min(seeders, 50) * 0.1

        return base_score

    def _get_best(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        return max(candidates, key=_score) if candidates else None

    # Tier 1: Target Res + Target Codec
    t1 = (
        _filter_results_by_resolution(results, target_res)
        if target_res != "all"
        else list(results)
    )
    if target_codec != "any":
        t1 = [r for r in t1 if (r.get("codec") or "").lower() == target_codec]
    best = _get_best(t1)
    if best:
        return best

    # Tier 2: Target Res + Alt Codec (Codec compromise)
    if alt_codec:
        t2 = (
            _filter_results_by_resolution(results, target_res)
            if target_res != "all"
            else list(results)
        )
        t2 = [r for r in t2 if (r.get("codec") or "").lower() == alt_codec]
        best = _get_best(t2)
        if best:
            return best

    # Tier 3: Fallback Res + Target Codec
    if fallback_res:
        t3 = _filter_results_by_resolution(results, fallback_res)
        if target_codec != "any":
            t3 = [r for r in t3 if (r.get("codec") or "").lower() == target_codec]
        best = _get_best(t3)
        if best:
            return best

    # Tier 4: Fallback Res + Alt Codec
    if fallback_res and alt_codec:
        t4 = _filter_results_by_resolution(results, fallback_res)
        t4 = [r for r in t4 if (r.get("codec") or "").lower() == alt_codec]
        best = _get_best(t4)
        if best:
            return best

    # Tier 5: Absolute Fallback (Requested Resolution, Any Codec)
    t5 = (
        _filter_results_by_resolution(results, target_res)
        if target_res != "all"
        else []
    )
    best = _get_best(t5)
    if best:
        return best

    # Tier 6: Final Resort (Best Score Overall)
    return _get_best(results)


async def _present_collection_download_confirmation(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    session: SearchSession,
    pending: dict[str, Any],
    missing: list[str],
) -> None:
    items = pending.get("items", [])
    franchise = pending.get("franchise", {}) or {}
    franchise_name = franchise.get("name") or session.collection_name or "Collection"
    resolution = session.collection_resolution or "best"
    codec = (session.collection_codec or "any").upper()
    selected_lines = [
        f"• {escape_markdown(_format_collection_movie_label(entry.get('movie', {})), version=2)}"
        for entry in items
    ]
    if selected_lines:
        movies_text = "\n".join(selected_lines)
    else:
        movies_text = "_No downloads needed_"
    text = (
        f"✅ Ready to queue *{len(items)}* movie{'s' if len(items) != 1 else ''} "
        f"for *{escape_markdown(franchise_name, version=2)}*\\.\n"
        f"Template: *{escape_markdown(resolution.upper(), version=2)}* / *{escape_markdown(codec, version=2)}*\\.\n"
        f"Movies in this run\\:\n{movies_text}"
    )
    owned_summaries = pending.get("owned_summaries") or []
    if owned_summaries:
        text += (
            f"\n\nAlready owned: *{len(owned_summaries)}* title"
            f"{'s' if len(owned_summaries) != 1 else ''} will be organized and added to Plex\\."
        )
    if missing:
        skipped = "\n".join(
            f"• {escape_markdown(label, version=2)}" for label in missing
        )
        text += f"\n\n⚠️ No suitable torrent was found for:\n{skipped}"

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Confirm Collection", callback_data="confirm_collection_download"
                )
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
        ]
    )
    await safe_edit_message(
        message,
        text=text,
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


def _filter_results_by_resolution(results: list[dict], resolution: str) -> list[dict]:
    """Filters search results using word boundaries to ensure precise matching."""
    res = resolution.lower()
    if res == "2160p":
        patterns = [r"2160p", r"\b4k\b", r"\buhd\b"]
    elif res == "1080p":
        patterns = [r"1080p", r"\bfhd\b"]
    elif res == "720p":
        patterns = [r"720p", r"\bhd\b"]
    else:
        patterns = [re.escape(res)]

    regex = re.compile("|".join(patterns), re.IGNORECASE)
    return [r for r in results if regex.search(r.get("title", ""))]


def _log_aggregated_results(query_str: str, results: list[dict[str, Any]]) -> None:
    """Logs aggregated search stats and a detailed breakdown of results."""
    count = len(results)
    if count == 0:
        return

    avg_seeds = sum(_safe_int(r.get("seeders")) for r in results) / count
    avg_leeches = sum(_safe_int(r.get("leechers")) for r in results) / count

    header = f"--- Aggregated Results for '{query_str}' ---"
    lines = [
        header,
        f"Total Results: {count}",
        f"Avg Seeders: {avg_seeds:.1f}, Avg Leechers: {avg_leeches:.1f}",
        "-" * 20,
    ]

    ordered_fields = [
        "title",
        "info_url",
        "score",
        "source",
        "uploader",
        "size_gb",
        "codec",
        "seeders",
        "leechers",
        "year",
    ]

    for idx, result in enumerate(results, start=1):
        lines.append(f"Result {idx}:")
        for key in ordered_fields:
            val = result.get(key)
            if key in ("seeders", "leechers"):
                val = _safe_int(val)
            lines.append(f"  {key}: {val}")
        lines.append("-" * 20)

    logger.info("\n".join(lines))


async def _present_search_results(
    message,
    context,
    results,
    query_str,
    *,
    session: SearchSession | None = None,
    max_size_gb: float | None = None,
    initial_resolution: str | None = None,
):
    """Persists result metadata on the session and renders the first page."""
    if session is None:
        session = _get_session(context)

    # Sort results by score (descending) to ensure aggregation hasn't broken ordering
    results.sort(key=lambda x: x.get("score", 0), reverse=True)

    escaped_query = escape_markdown(query_str, version=2)

    if not results:
        await safe_edit_message(
            message,
            text=f"❌ No results found for '`{escaped_query}`' across all configured sites\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if LOG_SCRAPER_STATS:
        _log_aggregated_results(query_str, results)

    resolution_filter = _normalize_resolution_filter(initial_resolution)
    allowed_filters = _get_allowed_resolution_filters(session)
    if resolution_filter not in allowed_filters:
        resolution_filter = "all"
    session.advance(SearchStep.CONFIRMATION)
    session.results = list(results)
    session.results_query = query_str
    session.results_page = 0
    session.results_resolution_filter = resolution_filter
    session.results_codec_filter = "all"
    session.results_max_size_gb = (
        float(max_size_gb) if isinstance(max_size_gb, (int, float)) else None
    )
    session.results_generated_at = time.time()
    _save_session(context, session)

    await _render_results_view(message, context, session)


def _normalize_resolution_filter(value: str | None) -> str:
    if not value:
        return "all"
    lowered = str(value).lower()
    if lowered in RESOLUTION_FILTERS:
        return lowered
    if lowered in ("4k", "uhd", "2160"):
        return "2160p"
    return "all"


def _get_allowed_resolution_filters(session: SearchSession) -> list[str]:
    if session.media_type == "movie":
        return ["all", "1080p", "2160p"]
    if session.media_type == "tv":
        return ["all", "720p", "1080p"]
    return list(RESOLUTION_FILTERS)


def _ensure_results_available(session: SearchSession) -> None:
    if not session.results or not session.results_query:
        raise SearchSessionError(RESULTS_EXPIRED_MESSAGE)
    generated_at = session.results_generated_at
    if not isinstance(generated_at, (int, float)):
        raise SearchSessionError(RESULTS_EXPIRED_MESSAGE)
    if time.time() - float(generated_at) > RESULTS_SESSION_TTL_SECONDS:
        raise SearchSessionError(RESULTS_EXPIRED_MESSAGE)


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _determine_size_cap(
    session: SearchSession, resolution_filter: str | None = None
) -> float | None:
    cap = session.results_max_size_gb
    if cap is None:
        return None
    active = _normalize_resolution_filter(
        resolution_filter or session.results_resolution_filter
    )
    if active == "2160p":
        return cap * FOUR_K_SIZE_MULTIPLIER
    return cap


def _compute_filtered_results(session: SearchSession) -> list[dict[str, Any]]:
    working = list(session.results or [])
    if not working:
        return []

    codec_filter = SearchSession.normalize_results_codec_filter(
        session.results_codec_filter
    )
    if codec_filter != "all":
        filtered_by_codec: list[dict[str, Any]] = []
        for item in working:
            codec_value = item.get("codec")
            if isinstance(codec_value, str) and codec_value.lower() == codec_filter:
                filtered_by_codec.append(item)
        working = filtered_by_codec

    resolution_filter = _normalize_resolution_filter(session.results_resolution_filter)
    if resolution_filter != "all":
        working = _filter_results_by_resolution(working, resolution_filter)

    size_cap = _determine_size_cap(session, resolution_filter)
    if size_cap is not None:
        limited: list[dict[str, Any]] = []
        for item in working:
            size_value = _safe_float(item.get("size_gb"))
            if size_value is None or size_value <= size_cap:
                limited.append(item)
        working = limited

    return working


def _format_result_button_label(result: dict[str, Any]) -> str:
    codec = result.get("codec") or "N/A"
    seeders = _safe_int(result.get("seeders"))
    size_value = _safe_float(result.get("size_gb"))
    size_text = f"{size_value:.2f} GB" if size_value is not None else "? GB"
    source_site = result.get("source") or "source"
    source_name = source_site.split(".")[0]
    return f"{codec} | S:{seeders} | {size_text} | [{source_name}]"


def _build_results_keyboard(
    session: SearchSession,
    filtered_results: list[dict[str, Any]],
    total_pages: int,
) -> list[list[InlineKeyboardButton]]:
    keyboard: list[list[InlineKeyboardButton]] = []
    start = session.results_page * RESULTS_PAGE_SIZE
    end = min(start + RESULTS_PAGE_SIZE, len(filtered_results))

    for idx in range(start, end):
        keyboard.append(
            [
                InlineKeyboardButton(
                    _format_result_button_label(filtered_results[idx]),
                    callback_data=f"search_select_{idx}",
                )
            ]
        )

    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []
        if session.results_page > 0:
            nav_row.append(
                InlineKeyboardButton(
                    "< Prev",
                    callback_data=f"search_results_page_{session.results_page - 1}",
                )
            )
        if session.results_page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton(
                    "Next >",
                    callback_data=f"search_results_page_{session.results_page + 1}",
                )
            )
        if nav_row:
            keyboard.append(nav_row)

    res_row: list[InlineKeyboardButton] = []
    allowed_filters = _get_allowed_resolution_filters(session)
    label_map = {
        "all": "All",
        "720p": "720p",
        "1080p": "1080p",
        "2160p": "2160p",
    }
    active_filter = _normalize_resolution_filter(session.results_resolution_filter)
    if active_filter not in allowed_filters:
        active_filter = "all"
    for value in allowed_filters:
        label = label_map.get(value, value.upper())
        prefix = "🟢" if active_filter == value else ""
        res_row.append(
            InlineKeyboardButton(
                f"{prefix}{label}",
                callback_data=f"search_results_filter_resolution_{value}",
            )
        )
    keyboard.append(res_row)

    codec_row: list[InlineKeyboardButton] = []
    active_codec = SearchSession.normalize_results_codec_filter(
        session.results_codec_filter
    )
    for value, label in (("all", "All"), ("x264", "x264"), ("x265", "x265")):
        prefix = "🟢" if active_codec == value else ""
        codec_row.append(
            InlineKeyboardButton(
                f"{prefix}{label}",
                callback_data=f"search_results_filter_codec_{value}",
            )
        )
    keyboard.append(codec_row)

    if (
        session.allow_detail_change
        and session.media_type == "tv"
        and session.tv_scope == "single"
    ):
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🔄️ Change", callback_data="search_tv_change_details"
                )
            ]
        )

    keyboard.append(
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]
    )
    return keyboard


def _clear_search_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clears persisted search session state (legacy helper for tests)."""
    clear_search_session(getattr(context, "user_data", None))


async def _render_results_view(
    message: Message, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    filtered_results = _compute_filtered_results(session)
    total_all = len(session.results or [])
    total_filtered = len(filtered_results)

    total_pages = (
        max(1, (total_filtered + RESULTS_PAGE_SIZE - 1) // RESULTS_PAGE_SIZE)
        if total_filtered
        else 1
    )
    if total_filtered == 0:
        session.results_page = 0
    else:
        session.results_page = min(
            max(session.results_page, 0), max(total_pages - 1, 0)
        )

    _save_session(context, session)

    raw_query = session.results_query or "this title"
    trim = slice(-6)
    processed_query = raw_query[trim]
    escaped_query = escape_markdown(processed_query, version=2)
    active_codec = SearchSession.normalize_results_codec_filter(
        session.results_codec_filter
    )
    codec_label = "  All  " if active_codec == "all" else active_codec
    resolution_label = (
        "   All    "
        if session.results_resolution_filter == "all"
        else session.results_resolution_filter.lower()
    )
    filters_text = (
        f"Resolution: *{resolution_label}* \\| " f"Codec filter: *{codec_label}*"
    )

    if total_filtered == 0:
        results_text = (
            f"Found {total_all} result\\(s\\) for *{escaped_query}*, but none match the current filters\\.\n"
            f"{filters_text}\n"
            "Use the buttons below to adjust the filters or restart the search\\."
        )
    else:
        results_text = (
            f"Found {total_all} result\\(s\\) for:\n"
            f"*{escaped_query}*\n"
            f"{filters_text}\n"
            "Choose a torrent to continue:"
        )

    keyboard = _build_results_keyboard(session, filtered_results, total_pages)
    await safe_edit_message(
        message,
        text=results_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _process_preliminary_results(
    status_message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    results: list[dict],
    *,
    session: SearchSession | None = None,
    notice: str | None = None,
) -> None:
    """Analyzes preliminary search results to decide the next step in the movie workflow."""
    if session is None:
        session = _get_session(context)

    title = session.effective_title or session.title or "this movie"
    escaped_title = escape_markdown(title, version=2)
    notice_text = notice or ""

    def _with_notice(text: str) -> str:
        if not notice_text:
            return text
        return f"{notice_text}\n\n{text}"

    if not results:
        logger.warning(f"Preliminary search for '{title}' yielded no results.")
        await safe_edit_message(
            status_message,
            text=_with_notice(
                rf"❌ No results found for '{escaped_title}'\. Please check the title and try again\."
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    unique_years = sorted(
        {str(result["year"]) for result in results if result.get("year")}
    )

    if len(unique_years) > 1:
        logger.info(
            f"Found multiple years for '{title}': {unique_years}. Prompting for selection."
        )
        session.advance(SearchStep.YEAR)
        session.prompt_message_id = status_message.message_id
        _save_session(context, session)
        await _prompt_for_year_selection(
            status_message, context, title, unique_years, session=session
        )

    elif len(unique_years) == 1:
        year = unique_years[0]
        logger.info(
            f"Found one unique year for '{title}': {year}. Gathering multi-resolution search results."
        )
        full_title = f"{title} ({year})"
        session.set_final_title(full_title)
        session.advance(SearchStep.RESOLUTION)
        _save_session(context, session)
        await _search_movie_results(status_message, context, session)

    else:
        logger.warning(
            f"Found results for '{title}', but could not determine any release years."
        )
        message_text = _with_notice(
            rf"❌ Found results for '{escaped_title}', but could not determine a release year\.\n\n"
            rf"Please try the search again and include the year manually \(e.g., '{escaped_title}' 2023\)\."
        )
        await safe_edit_message(
            status_message, text=message_text, parse_mode=ParseMode.MARKDOWN_V2
        )
