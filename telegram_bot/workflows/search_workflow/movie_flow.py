# telegram_bot/workflows/search_workflow/movie_flow.py

import re
from typing import Any, Literal

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from ...config import logger, require_scraper_max_torrent_size_gib
from ...services import scraping_service, search_logic
from ...ui.keyboards import single_column_keyboard
from ...utils import (
    safe_edit_message,
    safe_send_message,
)
from ..search_parser import parse_search_query
from ..search_session import (
    CONTEXT_LOST_MESSAGE,
    SearchSession,
    SearchSessionError,
    SearchStep,
)
from .movie_collection_flow import _start_collection_lookup
from .results import FOUR_K_SIZE_MULTIPLIER, _present_search_results
from .state import _end_search_workflow, _get_session, _save_session, _send_prompt

MOVIE_FAST_PATH_RESOLUTIONS = {"1080p", "2160p"}


def _get_scraper_max_size_gib(context: ContextTypes.DEFAULT_TYPE) -> float:
    return require_scraper_max_torrent_size_gib(context.bot_data)


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
    session.resolution = None
    session.results_codec_filter = "all"

    if (
        parsed_query.has_media_preferences
        and parsed_query.resolution in MOVIE_FAST_PATH_RESOLUTIONS
    ):
        session.resolution = parsed_query.resolution
        session.results_codec_filter = SearchSession.normalize_results_codec_filter(
            parsed_query.codec
        )

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

    bot_data = context.bot_data if isinstance(getattr(context, "bot_data", None), dict) else {}
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
            years, corrected = await scraping_service.fetch_movie_years_from_wikipedia(title)
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
        await _end_search_workflow(
            context,
            chat_id,
            CONTEXT_LOST_MESSAGE,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
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
    reply_markup = single_column_keyboard([(year, f"search_select_year_{year}") for year in years])

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
                InlineKeyboardButton("💎 1080p", callback_data="search_resolution_1080p"),
                InlineKeyboardButton("💩 720p", callback_data="search_resolution_720p"),
            ]
        ]
    else:
        keyboard_rows = [
            [
                InlineKeyboardButton("🪙 1080p", callback_data="search_resolution_1080p"),
                InlineKeyboardButton("💎 4K (2160p)", callback_data="search_resolution_4k"),
            ]
        ]
    if allow_detail_change and media_type == "tv":
        keyboard_rows.append(
            [InlineKeyboardButton("🔄️ Change", callback_data="search_tv_change_details")]
        )
    keyboard_rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")])
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
            await _end_search_workflow(
                context,
                target.chat_id,
                exc.user_message,
                source_message=target,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        else:
            await _end_search_workflow(
                context,
                target,
                exc.user_message,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        return

    display_title = escape_markdown(final_title, version=2)
    preferred_resolution = (
        session.resolution if session.resolution in MOVIE_FAST_PATH_RESOLUTIONS else None
    )
    preferred_codec = SearchSession.normalize_results_codec_filter(session.results_codec_filter)
    search_resolutions = (preferred_resolution,) if preferred_resolution else ("1080p", "2160p")

    if preferred_resolution:
        codec_suffix = (
            f" / *{escape_markdown(preferred_codec, version=2)}*"
            if preferred_codec != "all"
            else ""
        )
        progress_text = (
            f"🔍 Searching all sources for *{display_title}* in "
            f"*{escape_markdown(preferred_resolution, version=2)}*{codec_suffix}\\.\\.\\."
        )
    else:
        progress_text = f"🔍 Searching all sources for *{display_title}* in 1080p and 4K\\.\\.\\."

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
    scraper_max_size_gib = _get_scraper_max_size_gib(context)

    combined_results: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for resolution in search_resolutions:
        # Allow size override for 4K
        max_size = scraper_max_size_gib
        if resolution == "2160p":
            max_size *= FOUR_K_SIZE_MULTIPLIER

        results = await search_logic.orchestrate_searches(
            search_title,
            "movie",
            context,
            year=year,
            resolution=resolution,
            max_size_gib=max_size,
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
        (
            f"{final_title} [{preferred_resolution}]"
            if preferred_resolution
            else f"{final_title} [All]"
        ),
        session=session,
        max_size_gib=scraper_max_size_gib,
        initial_resolution=preferred_resolution or "all",
        initial_codec=preferred_codec,
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

    unique_years = sorted({str(result["year"]) for result in results if result.get("year")})

    if len(unique_years) > 1:
        logger.info(f"Found multiple years for '{title}': {unique_years}. Prompting for selection.")
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
        logger.warning(f"Found results for '{title}', but could not determine any release years.")
        message_text = _with_notice(
            rf"❌ Found results for '{escaped_title}', but could not determine a release year\.\n\n"
            rf"Please try the search again and include the year manually \(e.g., '{escaped_title}' 2023\)\."
        )
        await safe_edit_message(status_message, text=message_text, parse_mode=ParseMode.MARKDOWN_V2)
