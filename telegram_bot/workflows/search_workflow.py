# telegram_bot/workflows/search_workflow.py

import re
from typing import Any, Literal

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

from ..config import logger, MAX_TORRENT_SIZE_GB
from ..services import search_logic, torrent_service, scraping_service, plex_service
from ..services.media_manager import validate_and_enrich_torrent
from ..utils import safe_edit_message, parse_torrent_name, safe_send_message
from ..ui.views import send_confirmation_prompt
from .search_session import (
    CONTEXT_LOST_MESSAGE,
    SearchSession,
    SearchSessionError,
    SearchStep,
    clear_search_session,
)


def _get_session(context: ContextTypes.DEFAULT_TYPE) -> SearchSession:
    if context.user_data is None:
        context.user_data = {}
    return SearchSession.from_user_data(context.user_data)


def _save_session(context: ContextTypes.DEFAULT_TYPE, session: SearchSession) -> None:
    if context.user_data is None:
        context.user_data = {}
    session.save(context.user_data)


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
    if not query or not query.data or not isinstance(query.message, Message):
        return

    action = query.data

    if action.startswith("search_start_"):
        await _handle_start_button(query, context)
        return

    session = _get_session(context)
    requires_session = action.startswith(
        (
            "search_resolution_",
            "search_tv_scope_",
            "search_select_season_",
            "search_select_episode_",
            "search_select_year_",
        )
    )

    if requires_session and not session.is_active:
        await safe_edit_message(
            query.message,
            text=CONTEXT_LOST_MESSAGE,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    try:
        if action.startswith("search_resolution_"):
            await _handle_resolution_button(query, context, session)
        elif action.startswith("search_tv_scope_"):
            await _handle_tv_scope_selection(query, context, session)
        elif action.startswith("search_select_season_"):
            await _handle_season_selection_button(query, context, session)
        elif action.startswith("search_select_episode_"):
            await _handle_episode_selection_button(query, context, session)
        elif action.startswith("search_select_year_"):
            await _handle_year_selection_button(query, context, session)
        elif action.startswith("search_select_"):
            await _handle_result_selection_button(query, context)
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

    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", query)
    if year_match:
        year = year_match.group(0)
        title_part = re.sub(r"[^\w\s-]", "", query[: year_match.start()]).strip()
        title_part = title_part.title()
        full_title = f"{title_part} ({year})"
        session.media_type = "movie"
        session.set_title(title_part)
        session.set_final_title(full_title)
        session.advance(SearchStep.RESOLUTION)
        _save_session(context, session)
        await _prompt_for_resolution(
            chat_id, context, full_title, media_type="movie", session=session
        )
        return

    title = re.sub(r"[^\w\s-]", "", query).strip()
    title = title.title()
    session.media_type = "movie"
    session.set_title(title)
    _save_session(context, session)

    status_message = await safe_send_message(
        context.bot,
        chat_id,
        f"Looking up release years for *{escape_markdown(title, version=2)}* on Wikipedia\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    try:
        years, corrected = await scraping_service.fetch_movie_years_from_wikipedia(
            title
        )
    except Exception:
        years, corrected = [], None

    if corrected and isinstance(corrected, str) and corrected.strip():
        session.set_title(corrected, resolved_title=corrected)
        display_title = corrected
    else:
        display_title = title

    bot_data = (
        context.bot_data if isinstance(getattr(context, "bot_data", None), dict) else {}
    )
    has_search_config = bool(bot_data.get("SEARCH_CONFIG"))
    effective_years = years if has_search_config else []

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
        await _prompt_for_resolution(
            status_message, context, full_title, media_type="movie", session=session
        )
        return

    results = await search_logic.orchestrate_searches(display_title, "movie", context)
    await _process_preliminary_results(
        status_message, context, results, session=session
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
    await _prompt_for_resolution(
        chat_id, context, full_title, media_type="movie", session=session
    )


async def _handle_tv_title_reply(
    chat_id: int, query: str, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
):
    """Handles user reply for a TV show title."""
    if session.media_type != "tv" or session.step != SearchStep.TITLE:
        return

    sanitized_title = re.sub(r"[^\w\s-]", "", query).strip()
    sanitized_title = sanitized_title.title()
    session.media_type = "tv"
    session.set_title(sanitized_title)
    session.advance(SearchStep.TV_SEASON)
    _save_session(context, session)

    status_message = await safe_send_message(
        context.bot,
        chat_id,
        f"🎞️ Checking seasons for *{escape_markdown(sanitized_title, version=2)}* on Wikipedia\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    try:
        seasons_count = await scraping_service.fetch_total_seasons_from_wikipedia(
            sanitized_title
        )
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
                f"*{escape_markdown(sanitized_title, version=2)}*\\. Please select a season:"
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if isinstance(seasons_count, int) and seasons_count > MAX_SEASON_BUTTONS:
        prompt_text = (
            f"Found *{escape_markdown(str(seasons_count), version=2)}* seasons for "
            f"*{escape_markdown(sanitized_title, version=2)}*\\. Now, please send the season number\\."
        )
    else:
        prompt_text = (
            f"Could not determine the total seasons for *{escape_markdown(sanitized_title, version=2)}*\\.\n"
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
    keyboard = [
        [
            InlineKeyboardButton(
                "Single Episode", callback_data="search_tv_scope_single"
            ),
            InlineKeyboardButton(
                "Entire Season", callback_data="search_tv_scope_season"
            ),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
    ]
    sent_message = await safe_send_message(
        context.bot,
        chat_id,
        prompt_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
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
        data = query.data or ""
        season_str = data.split("_")[3]
        season_num = int(season_str)
    except Exception:
        await safe_edit_message(
            query.message,
            text="❓ An error occurred with your selection\\. Please try again\\.",
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
    keyboard = [
        [
            InlineKeyboardButton(
                "Single Episode", callback_data="search_tv_scope_single"
            ),
            InlineKeyboardButton(
                "Entire Season", callback_data="search_tv_scope_season"
            ),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
    ]
    await safe_edit_message(
        query.message,
        text=prompt_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_episode_selection_button(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    """Handles user selecting an episode via inline buttons in the single-episode flow."""
    if not isinstance(query.message, Message):
        return

    try:
        data = query.data or ""
        episode_str = data.split("_")[3]
        episode_num = int(episode_str)
    except Exception:
        await safe_edit_message(
            query.message,
            text="❓ An error occurred with your selection\\. Please try again\\.",
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

    full_search_term = f"{title} S{int(season):02d}E{episode_num:02d}"
    session.media_type = "tv"
    session.tv_scope = "single"
    session.episode = episode_num
    session.set_final_title(full_search_term)
    session.advance(SearchStep.RESOLUTION)
    _save_session(context, session)

    await _prompt_for_resolution(
        query.message, context, full_search_term, media_type="tv", session=session
    )


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
    full_search_term = f"{title} S{int(season):02d}E{episode:02d}"

    session.media_type = "tv"
    session.tv_scope = "single"
    session.episode = episode
    session.set_final_title(full_search_term)
    session.advance(SearchStep.RESOLUTION)
    _save_session(context, session)

    await _prompt_for_resolution(
        chat_id, context, full_search_term, media_type="tv", session=session
    )


async def _handle_tv_scope_selection(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    """Handles user's choice between single episode or entire season."""
    if not isinstance(query.message, Message):
        return

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

    if query.data == "search_tv_scope_single":
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
                    "Choose an episode below or type the number."
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
            f"Please send the episode number for Season {escape_markdown(str(season), version=2)}.",
            session=session,
        )
        return

    if query.data == "search_tv_scope_season":
        logger.info(
            f"[WIKI] Verifying season details on Wikipedia for '{title}' S{int(season):02d}."
        )
        await safe_edit_message(
            query.message,
            "Verifying season details on Wikipedia...",
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
                "❓ Could not verify episode count. Operation cancelled.",
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
                    f"S{int(season):02d} already exist in your library. Nothing to download."
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
                    "I will only fetch the missing episodes. Choose a resolution:"
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await safe_edit_message(
                query.message,
                text=(
                    f"No existing episodes found in your library for *{escape_markdown(str(title), version=2)}* S{int(season):02d}\\.\n"
                    "You may download a season pack if available. Choose a resolution:"
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )

        await _prompt_for_resolution(
            query.message,
            context,
            f"{title} S{int(season):02d}",
            media_type="tv",
            session=session,
        )


# --- Button Press Handlers ---

# --- Button Press Handlers ---


async def _handle_start_button(query, context):
    """Handles the initial 'Movie' or 'TV Show' button press."""
    if context.user_data is None:
        context.user_data = {}
    context.user_data["active_workflow"] = "search"
    clear_search_session(context.user_data)
    session = SearchSession()

    if query.data == "search_start_movie":
        session.media_type = "movie"
        session.advance(SearchStep.TITLE)
        prompt_text = "🎬 Please send me the title of the movie to search for \\(you can include the year\\)\\."
    else:
        session.media_type = "tv"
        session.advance(SearchStep.TITLE)
        prompt_text = "📺 Please send me the title of the TV show to search for\\."

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


async def _handle_resolution_button(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
):
    """Handles the resolution selection and triggers the appropriate search."""
    if any(x in query.data for x in ("2160p", "4k")):
        resolution = "2160p"
    elif "720p" in query.data:
        resolution = "720p"
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
            text=f"🎯 Searching all sources for *{escape_markdown(final_title, version=2)}* in *{resolution}*\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        year_match = re.search(r"\((\d{4})\)", final_title)
        year = year_match.group(1) if year_match else None
        search_title = final_title.split("(")[0].strip()
        results = await search_logic.orchestrate_searches(
            search_title, "movie", context, year=year, resolution=resolution
        )
        filtered_results = _filter_results_by_resolution(results, resolution)
        await _present_search_results(
            query.message,
            context,
            filtered_results,
            f"{final_title} [{resolution}]",
            max_size_gb=MAX_TORRENT_SIZE_GB,
        )
        return

    if media_type == "tv":
        tv_scope = session.tv_scope
        title = session.effective_title or session.title
        season = session.season
        if tv_scope == "single" and title:
            await safe_edit_message(
                query.message,
                text=f"🎯 Searching all sources for *{escape_markdown(final_title, version=2)}* in *{resolution}*...",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            results = await search_logic.orchestrate_searches(
                final_title, "tv", context, base_query_for_filter=title
            )
            filtered_results = _filter_results_by_resolution(results, resolution)
            await _present_search_results(
                query.message,
                context,
                filtered_results,
                f"{final_title} [{resolution}]",
            )
            return

        if tv_scope == "season" and title and season is not None:
            await safe_edit_message(
                query.message,
                text=(
                    f"🎯 Searching for Season {escape_markdown(str(season), version=2)} "
                    f"of *{escape_markdown(title, version=2)}* in *{resolution}*..."
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            await _perform_tv_season_search_with_resolution(
                query.message, context, title, int(season), resolution
            )
            return

    await safe_edit_message(
        query.message,
        CONTEXT_LOST_MESSAGE,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    clear_search_session(context.user_data)


async def _handle_result_selection_button(query, context):
    """Handles the user selecting a specific torrent from the results list."""
    if context.user_data is None:
        context.user_data = {}
    search_results = context.user_data.pop("search_results", [])
    if not search_results:
        await safe_edit_message(
            query.message,
            "❌ This selection has expired\\. Please start the search again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    try:
        choice_index = int(query.data.split("_")[2])
        selected_result = search_results[choice_index]
        url_to_process = selected_result["page_url"]

        logger.info(
            f"User selected '{selected_result['title']}'. Passing to torrent_service: {url_to_process[:70]}"
        )

        # Handoff to the torrent service to start the download flow
        ti = await torrent_service.process_user_input(
            url_to_process, context, query.message
        )
        if not ti:
            return

        error_message, parsed_info = await validate_and_enrich_torrent(
            ti, query.message
        )
        if error_message or not parsed_info:
            return

        await send_confirmation_prompt(query.message, context, ti, parsed_info)

    except (ValueError, IndexError):
        await safe_edit_message(
            query.message,
            "❌ An error occurred with your selection\\. Please try again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def _handle_year_selection_button(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    """Handles the user selecting a specific year from the presented options."""
    if not query or not query.data:
        logger.warning("Callback query received without data. Ignoring.")
        await query.answer()
        return

    if not isinstance(query.message, Message):
        logger.warning(
            "Could not process year selection: The associated message is inaccessible."
        )
        await query.answer(
            text="❓ Error: The original message could not be modified.",
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
        data = query.data or ""
        selected_year = data.split("_")[3]
        full_title = f"{title} ({selected_year})"
        logger.info(f"User selected year {selected_year} for title '{title}'.")
    except IndexError:
        logger.error(f"Could not parse year from callback data: {query.data}")
        await safe_edit_message(
            query.message,
            text="❓ An error occurred with your selection. Please try again.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    session.set_final_title(full_title)
    session.media_type = "movie"
    session.advance(SearchStep.RESOLUTION)
    _save_session(context, session)

    await _prompt_for_resolution(
        query.message, context, full_title, media_type="movie", session=session
    )


# --- Helper/UI Functions ---

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
) -> None:
    """Summarizes season search results and asks for confirmation."""
    if session is None:
        session = _get_session(context)
    season = session.season
    total_eps = session.season_episode_count

    if not found_torrents:
        await safe_edit_message(
            message,
            text="❓ No torrents found for this season.",
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
        summary = f"Found a season pack for Season {season}."
    else:
        summary = f"Found torrents for {len(found_torrents)} of {target_total} episode(s) in Season {season}."

    if is_pack:
        keyboard = [
            [
                InlineKeyboardButton(
                    "Confirm", callback_data="confirm_season_download"
                ),
                InlineKeyboardButton("Reject", callback_data="reject_season_pack"),
                InlineKeyboardButton("Cancel", callback_data="cancel_operation"),
            ]
        ]
    else:
        keyboard = [
            [
                InlineKeyboardButton(
                    "Confirm", callback_data="confirm_season_download"
                ),
                InlineKeyboardButton("Cancel", callback_data="cancel_operation"),
            ]
        ]

    await safe_edit_message(
        message,
        text=summary,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    context.user_data.setdefault("pending_season_download", list())
    context.user_data["pending_season_download"] = found_torrents


async def _prompt_for_resolution(
    target: Message | int,
    context: ContextTypes.DEFAULT_TYPE,
    full_title: str,
    *,
    media_type: Literal["movie", "tv"] = "movie",
    session: SearchSession | None = None,
) -> None:
    """Asks the user to select a resolution."""
    if session is None:
        session = _get_session(context)

    session.set_final_title(full_title)
    session.media_type = media_type
    session.advance(SearchStep.RESOLUTION)
    _save_session(context, session)

    if media_type == "tv":
        keyboard = [
            [
                InlineKeyboardButton("1080p", callback_data="search_resolution_1080p"),
                InlineKeyboardButton("720p", callback_data="search_resolution_720p"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
        ]
    else:
        keyboard = [
            [
                InlineKeyboardButton("1080p", callback_data="search_resolution_1080p"),
                InlineKeyboardButton(
                    "4K (2160p)", callback_data="search_resolution_4k"
                ),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
        ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text_prompt = f"Got it: `{escape_markdown(full_title, version=2)}`\\. Now, please select your desired resolution:"

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


async def _perform_tv_season_search_with_resolution(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    title: str,
    season: int,
    resolution: str,
    *,
    force_individual_episodes: bool = False,
    session: SearchSession | None = None,
) -> None:
    """
    Searches for a TV season pack or individual episodes, applying a resolution filter.
    On success, presents a confirmation summary to queue the season download.
    """
    # Try a couple of season query variants first
    season_queries = [f"{title} S{season:02d}", f"{title} Season {season}"]
    found_results: list[dict[str, Any]] = []
    for q in season_queries:
        # Use the base title as a stricter filter to avoid spin-off mis-matches
        res = await search_logic.orchestrate_searches(
            q, "tv", context, base_query_for_filter=title
        )
        if res:
            # Do not filter season packs by resolution; many packs omit it in the title
            found_results.extend(res)
        if len(found_results) >= 3:
            break

    torrents_to_queue: list[dict[str, Any]] = []

    # Primary strategy: look for an actual season pack (unless explicitly skipped)
    season_pack_torrent = None
    pack_candidates = []
    # Enforce individual episodes if any are already owned
    if session is None:
        session = _get_session(context)

    existing_owned = set(session.existing_episodes or [])
    must_individual = bool(force_individual_episodes or existing_owned)

    if not must_individual:
        season_token = f"s{season:02d} "
        for item in found_results:
            title_lower = item.get("title", "").lower()
            if (
                any(k in title_lower for k in ["complete", "collection", "season pack"])
                or season_token in title_lower
            ) and not re.search(r"s\d{1,2}e\d{1,2}", title_lower):
                pack_candidates.append(item)

        season_pack_torrent = (
            max(pack_candidates, key=lambda x: x.get("score", 0))
            if pack_candidates
            else None
        )

    if season_pack_torrent and not must_individual:
        # Present all pack-like candidates as a normal results list with details
        query_str = f"{title} S{season:02d} [{resolution}]"
        await _present_search_results(message, context, pack_candidates, query_str)
        return
    else:
        # Fallback: search for each episode individually
        # Guard for Optional user_data to satisfy type checkers/IDEs
        episode_count = int(session.season_episode_count or 0)
        # Determine target episodes:
        # - If season_missing_episode_numbers is explicitly present, use it;
        #   if it's an empty list, then everything is already owned.
        # - If not present at all, default to the full season [1..episode_count].
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
        # Fetch all episode titles once to avoid per-episode Wikipedia lookups
        titles_map: dict[int, str] = {}
        corrected_title: str | None = None
        try:
            logger.info(
                f"[WIKI] Fetching episode titles from Wikipedia for '{title}' S{season:02d}."
            )
            (
                titles_map,
                corrected_title,
            ) = await scraping_service.fetch_episode_titles_for_season(title, season)
            logger.info(
                f"[WIKI] Retrieved {len(titles_map)} episode titles for '{title}' S{season:02d}."
            )
            if corrected_title and corrected_title != title:
                logger.info(
                    f"[WIKI] Title corrected by Wikipedia: '{title}' -> '{corrected_title}'."
                )
            else:
                logger.debug(
                    f"[WIKI] No title correction for '{title}'. Using original."
                )
            logger.debug(
                f"[WIKI] Episode title keys available: {sorted(list(titles_map.keys()))}"
            )
        except Exception:
            titles_map, corrected_title = {}, None

        # Progress feedback: update the message after each episode search
        processed_eps = 0

        def _progress_text(last_ep: int | None) -> str:
            base = (
                f"🔎 Searching for Season {escape_markdown(str(season), version=2)} "
                f"of *{escape_markdown(title, version=2)}* in *{escape_markdown(resolution, version=2)}*\\.\\.\\."
            )
            total_targets = len(targets) if targets else episode_count
            if total_targets:
                return base + f"\nProgress: {processed_eps}/{total_targets}"
            return base

        for ep in targets:
            search_term = f"{title} S{season:02d}E{ep:02d}"
            ep_results = await search_logic.orchestrate_searches(
                search_term, "tv", context, base_query_for_filter=title
            )
            # Do not filter by resolution for episodes in season fallback
            if not ep_results:
                processed_eps += 1
                await safe_edit_message(
                    message,
                    text=_progress_text(ep),
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
                continue
            best = ep_results[0]
            link = best.get("page_url")
            if not link:
                processed_eps += 1
                await safe_edit_message(
                    message,
                    text=_progress_text(ep),
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
                continue
            parsed_info = parse_torrent_name(best.get("title", ""))
            parsed_info["title"] = title
            parsed_info["season"] = season
            parsed_info["episode"] = ep
            parsed_info["type"] = "tv"

            # Use pre-fetched episode titles from Wikipedia (cached per season)
            parsed_info["episode_title"] = titles_map.get(ep)
            if corrected_title:
                parsed_info["title"] = corrected_title

            torrents_to_queue.append({"link": link, "parsed_info": parsed_info})

            # Update progress after successful episode processing
            processed_eps += 1
            await safe_edit_message(
                message,
                text=_progress_text(ep),
                parse_mode=ParseMode.MARKDOWN_V2,
            )

    await _present_season_download_confirmation(
        message, context, torrents_to_queue, session=session
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
    resolution = session.resolution or "1080p"

    if not title or season is None:
        await safe_edit_message(
            query.message,
            text=CONTEXT_LOST_MESSAGE,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        clear_search_session(context.user_data)
        return

    prefix = escape_markdown(
        "🎯 Rejected season pack\\. Collecting single episodes for ", version=2
    )
    title_md = escape_markdown(str(title), version=2)
    message_text = f"{prefix}*{title_md}* S{int(season):02d}."
    await safe_edit_message(
        query.message,
        text=message_text,
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    await _perform_tv_season_search_with_resolution(
        query.message,
        context,
        str(title),
        int(season),
        str(resolution),
        force_individual_episodes=True,
        session=session,
    )


def _filter_results_by_resolution(results: list[dict], resolution: str) -> list[dict]:
    """Filters search results to only include entries matching the desired resolution."""
    res = resolution.lower()
    if res == "2160p":
        patterns = ["2160p", "4k"]
    elif res == "1080p":
        patterns = ["1080p"]
    elif res == "720p":
        patterns = ["720p"]
    else:
        patterns = [res]
    return [
        r for r in results if any(p in r.get("title", "").lower() for p in patterns)
    ]


async def _present_search_results(
    message, context, results, query_str, *, max_size_gb: float | None = None
):
    """Formats and displays the final list of search results, pre-filtered by size."""
    _clear_search_context(context)

    escaped_query = escape_markdown(query_str, version=2)

    if not results:
        await safe_edit_message(
            message,
            text=f"❌ No results found for '`{escaped_query}`' across all configured sites\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # Filter the results list to exclude torrents larger than the configured limit
    # Optionally allow a per-call override (e.g., for 4K movies which are larger).
    size_cap = (
        max_size_gb if isinstance(max_size_gb, (int, float)) else MAX_TORRENT_SIZE_GB
    )
    original_count = len(results)
    filtered_results = [
        r for r in results if r.get("size_gb", float("inf")) <= size_cap
    ]

    # Handle the case where all found results were too large
    if not filtered_results:
        await safe_edit_message(
            message,
            text=(
                f"ℹ️ Found {original_count} result\\(s\\) for '`{escaped_query}`', "
                f"but none were under the *{escape_markdown(str(MAX_TORRENT_SIZE_GB), version=2)} GB* size limit\\."
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # Use the filtered list from this point on
    context.user_data["search_results"] = filtered_results
    keyboard = []

    results_text = f"Found {len(filtered_results)} valid result\\(s\\) for *{escaped_query}*\\. Please select one to download:"

    for i, result in enumerate(filtered_results[:5]):  # Limit to 5 choices
        source_site = result.get("source", "Site")
        source_name = source_site.split(".")[0]
        button_label = f"{result.get('codec', 'N/A')} | S: {result.get('seeders', 0)} | {result.get('size_gb', 0.0):.2f} GB | [{source_name}]"
        keyboard.append(
            [InlineKeyboardButton(button_label, callback_data=f"search_select_{i}")]
        )

    keyboard.append(
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]
    )
    await safe_edit_message(
        message,
        text=results_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


def _clear_search_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clears the persisted search session."""
    clear_search_session(getattr(context, "user_data", None))


async def _process_preliminary_results(
    status_message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    results: list[dict],
    *,
    session: SearchSession | None = None,
) -> None:
    """Analyzes preliminary search results to decide the next step in the movie workflow."""
    if session is None:
        session = _get_session(context)

    title = session.effective_title or session.title or "this movie"
    escaped_title = escape_markdown(title, version=2)

    if not results:
        logger.warning(f"Preliminary search for '{title}' yielded no results.")
        await safe_edit_message(
            status_message,
            text=f"❓ No results found for `'{escaped_title}'`\\. Please check the title and try again\\.",
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
            f"Found one unique year for '{title}': {year}. Proceeding to resolution selection."
        )
        full_title = f"{title} ({year})"
        session.set_final_title(full_title)
        session.advance(SearchStep.RESOLUTION)
        _save_session(context, session)
        await _prompt_for_resolution(
            status_message, context, full_title, media_type="movie", session=session
        )

    else:
        logger.warning(
            f"Found results for '{title}', but could not determine any release years."
        )
        message_text = (
            f"❓ Found results for `'{escaped_title}'`, but could not determine a release year.\n\n"
            f"Please try the search again and include the year manually (e.g., `{escaped_title} 2023`)."
        )
        await safe_edit_message(
            status_message, text=message_text, parse_mode=ParseMode.MARKDOWN_V2
        )
