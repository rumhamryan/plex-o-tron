# telegram_bot/workflows/search_workflow/handlers.py

import re

from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from ...config import MAX_TORRENT_SIZE_GB, logger
from ...services import search_logic, torrent_service
from ...services.media_manager import validate_and_enrich_torrent
from ...ui.views import send_confirmation_prompt
from ...utils import (
    safe_edit_message,
    safe_send_message,
)
from ..search_session import (
    CONTEXT_LOST_MESSAGE,
    SearchSession,
    SearchSessionError,
    SearchStep,
    clear_search_session,
)
from .movie_collection_flow import (
    _handle_collection_accept,
    _handle_collection_codec_button,
    _handle_collection_confirm,
    _handle_collection_movie_toggle,
    _handle_collection_resolution_button,
)
from .movie_flow import _handle_movie_title_reply, _handle_movie_year_reply, _search_movie_results
from .results import (
    FOUR_K_SIZE_MULTIPLIER,
    _compute_filtered_results,
    _ensure_results_available,
    _get_allowed_resolution_filters,
    _normalize_resolution_filter,
    _present_search_results,
    _render_results_view,
)
from .state import (
    _get_callback_data,
    _get_session,
    _get_user_data_store,
    _save_session,
)
from .tv_flow import (
    _build_tv_scope_keyboard,
    _handle_tv_change_details,
    _handle_tv_episode_reply,
    _handle_tv_scope_selection,
    _handle_tv_season_codec_button,
    _handle_tv_season_reply,
    _handle_tv_season_resolution_button,
    _handle_tv_title_reply,
    _perform_tv_season_search,
    _search_tv_single_results,
    _validate_episode_released,
)


async def handle_search_workflow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            await context.bot.delete_message(chat_id=chat.id, message_id=prompt_message_id)
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


async def handle_search_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        prompt_text = "🎬 Are you searching for a single movie or an entire franchise collection?"
        reply_markup = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Single Movie", callback_data="search_movie_scope_single")],
                [InlineKeyboardButton("Collection", callback_data="search_movie_scope_collection")],
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
        prompt_text = "🎬 Send the title of a movie within the franchise you want to collect\\."

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

    session.results_codec_filter = SearchSession.normalize_results_codec_filter(requested)
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
        logger.warning("Could not process year selection: The associated message is inaccessible.")
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
