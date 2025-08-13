# telegram_bot/workflows/search_workflow.py

import re
from typing import Dict, List, Union

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
from ..services import search_logic, torrent_service
from ..services.media_manager import validate_and_enrich_torrent
from ..utils import safe_edit_message
from ..ui.views import send_confirmation_prompt


async def handle_search_workflow(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Manages text-based replies for the search workflow."""
    if not isinstance(update.message, Message) or not update.message.text:
        return
    if context.user_data is None:
        context.user_data = {}

    chat = update.effective_chat
    if not chat:
        return

    query = update.message.text.strip()
    next_action = context.user_data.get("next_action")

    # Clean up previous messages
    prompt_message_id = context.user_data.pop("prompt_message_id", None)
    try:
        if prompt_message_id:
            await context.bot.delete_message(
                chat_id=chat.id, message_id=prompt_message_id
            )
        await update.message.delete()
    except BadRequest:
        pass

    # --- MOVIE WORKFLOW ---
    if next_action == "search_movie_get_title":
        await _handle_movie_title_reply(chat.id, query, context)
    elif next_action == "search_movie_get_year":
        await _handle_movie_year_reply(chat.id, query, context)

    # --- TV SHOW WORKFLOW ---
    elif next_action == "search_tv_get_title":
        await _handle_tv_title_reply(chat.id, query, context)
    elif next_action == "search_tv_get_season":
        await _handle_tv_season_reply(chat.id, query, context)
    elif next_action == "search_tv_get_episode":
        await _handle_tv_episode_reply(chat.id, query, context)


async def handle_search_buttons(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handles all button presses related to the search workflow."""
    query = update.callback_query
    if (
        not query
        or not query.data
        or not isinstance(query.message, Message)
        or context.user_data is None
    ):
        return

    action = query.data

    if action.startswith("search_start_"):
        await _handle_start_button(query, context)
    elif action.startswith("search_resolution_"):
        await _handle_resolution_button(query, context)
    elif action.startswith("search_select_year_"):  # <-- NEWLY ADDED BLOCK
        await _handle_year_selection_button(query, context)
    elif action.startswith("search_select_"):
        await _handle_result_selection_button(query, context)
    else:
        logger.warning(f"Received unhandled search callback: {action}")


# --- Text Reply Handlers ---


async def _handle_movie_title_reply(chat_id, query, context):
    """Handles the user's reply when asked for a movie title."""
    if context.user_data.get("next_action") != "search_movie_get_title":
        return

    year_match = re.search(r"\b(19\d{2}|20\d{2})\b", query)
    if year_match:
        year = year_match.group(0)
        title_part = re.sub(r"[^\w\s]", "", query[: year_match.start()]).strip()
        full_title = f"{title_part} ({year})"
        context.user_data["search_media_type"] = "movie"
        await _prompt_for_resolution(chat_id, context, full_title)
    else:
        title = re.sub(r"[^\w\s]", "", query).strip()
        context.user_data["search_query_title"] = title

        status_message = await context.bot.send_message(
            chat_id,
            f"🔎 Searching for available years for *{escape_markdown(title, version=2)}*\\.\\.\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

        results = await search_logic.orchestrate_searches(title, "movie", context)
        await _process_preliminary_results(status_message, context, results)


async def _handle_movie_year_reply(chat_id, query, context):
    """Handles the user's reply when asked for a movie year."""
    if context.user_data.get("next_action") != "search_movie_get_year":
        return

    title = context.user_data.get("search_query_title")
    if not title:
        await context.bot.send_message(
            chat_id, "❌ Search context was lost. Please start over."
        )
        return

    if not (query.isdigit() and len(query) == 4):
        context.user_data["next_action"] = "search_movie_get_year"
        error_text = f"That doesn't look like a valid 4\\-digit year\\. Please try again for *{escape_markdown(title, version=2)}* or cancel\\."
        await _send_prompt(chat_id, context, error_text)
        return

    full_title = f"{title} ({query})"
    context.user_data["search_media_type"] = "movie"
    await _prompt_for_resolution(chat_id, context, full_title)


async def _handle_tv_title_reply(chat_id, query, context):
    """Handles user reply for a TV show title."""
    if context.user_data.get("next_action") != "search_tv_get_title":
        return

    sanitized_title = re.sub(r"[^\w\s]", "", query).strip()
    context.user_data["search_query_title"] = sanitized_title
    context.user_data["next_action"] = "search_tv_get_season"
    prompt_text = f"Got it: *{escape_markdown(sanitized_title, version=2)}*\\. Now, please send the season number\\."
    await _send_prompt(chat_id, context, prompt_text)


async def _handle_tv_season_reply(chat_id, query, context):
    """Handles user reply for a season number."""
    if context.user_data.get("next_action") != "search_tv_get_season":
        return

    title = context.user_data.get("search_query_title")
    if not title or not query.isdigit():
        error_text = f"That doesn't look like a valid number\\. Please send the season number for *{escape_markdown(title or '', version=2)}*\\."
        await _send_prompt(chat_id, context, error_text)
        context.user_data["next_action"] = "search_tv_get_season"
        return

    context.user_data["search_season_number"] = int(query)
    context.user_data["next_action"] = "search_tv_get_episode"
    prompt_text = f"Season *{escape_markdown(query, version=2)}* selected\\. Now, please send the episode number\\."
    await _send_prompt(chat_id, context, prompt_text)


async def _handle_tv_episode_reply(chat_id, query, context):
    """Handles user reply for an episode number and triggers the search."""
    if context.user_data.get("next_action") != "search_tv_get_episode":
        return

    title = context.user_data.get("search_query_title")
    season = context.user_data.get("search_season_number")
    if not title or season is None or not query.isdigit():
        error_text = f"That doesn't look like a valid number\\. Please send the episode number for *{escape_markdown(title or '', version=2)} S{season or 0:02d}*\\."
        await _send_prompt(chat_id, context, error_text)
        context.user_data["next_action"] = "search_tv_get_episode"
        return

    episode = int(query)
    full_search_term = f"{title} S{season:02d}E{episode:02d}"
    status_message = await context.bot.send_message(
        chat_id,
        f"🔎 Searching all sources for *{escape_markdown(full_search_term, version=2)}*\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    results = await search_logic.orchestrate_searches(full_search_term, "tv", context)
    await _present_search_results(status_message, context, results, full_search_term)


# --- Button Press Handlers ---


async def _handle_start_button(query, context):
    """Handles the initial 'Movie' or 'TV Show' button press."""
    context.user_data["active_workflow"] = "search"
    if query.data == "search_start_movie":
        context.user_data["next_action"] = "search_movie_get_title"
        prompt_text = "🎬 Please send me the title of the movie to search for \\(you can include the year\\)\\."
    else:  # search_start_tv
        context.user_data["next_action"] = "search_tv_get_title"
        prompt_text = "📺 Please send me the title of the TV show to search for\\."

    await safe_edit_message(
        query.message,
        text=prompt_text,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    context.user_data["prompt_message_id"] = query.message.message_id


async def _handle_resolution_button(query, context):
    """Handles the resolution selection and triggers the movie search."""
    resolution = "2160p" if "2160p" in query.data or "4k" in query.data else "1080p"
    final_title = context.user_data.get("search_final_title")
    media_type = context.user_data.get("search_media_type")

    if not final_title or media_type != "movie":
        await safe_edit_message(
            query.message,
            "❌ Search context has expired\\. Please start over\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    await safe_edit_message(
        query.message,
        text=f"🔎 Searching all sources for *{escape_markdown(final_title, version=2)}* in *{resolution}*\\.\\.\\.",
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
        query.message, context, filtered_results, f"{final_title} [{resolution}]"
    )


async def _handle_result_selection_button(query, context):
    """Handles the user selecting a specific torrent from the results list."""
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
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handles the user selecting a specific year from the presented options."""
    # 1. Ensure the callback query itself and its data exist.
    if not query or not query.data:
        logger.warning("Callback query received without data. Ignoring.")
        # Silently answer the query to remove the "loading" state on the user's client.
        await query.answer()
        return

    # 2. Ensure the associated message is accessible to edit.
    if not isinstance(query.message, Message):
        logger.warning(
            "Could not process year selection: The associated message is inaccessible."
        )
        await query.answer(
            text="❌ Error: The original message could not be modified.",
            show_alert=True,
        )
        return

    # 3. Ensure the user_data context has not been lost.
    if context.user_data is None:
        logger.error(
            "Callback received but user_data was None. Aborting year selection."
        )
        await safe_edit_message(
            query.message,
            text="❌ An error occurred and your session was lost\\. Please start over\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    title = context.user_data.get("search_query_title")
    if not title:
        await safe_edit_message(
            query.message,
            "❌ Search context has expired\\. Please start over\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    try:
        # At this point, the type checker knows query.data is a string.
        selected_year = query.data.split("_")[3]
        full_title = f"{title} ({selected_year})"
        logger.info(f"User selected year {selected_year} for title '{title}'.")

        context.user_data["search_final_title"] = full_title
        context.user_data["search_media_type"] = "movie"

        await _prompt_for_resolution(query.message, context, full_title)

    except IndexError:
        logger.error(f"Could not parse year from callback data: {query.data}")
        await safe_edit_message(
            query.message,
            "❌ An error occurred with your selection\\. Please try again\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


# --- Helper/UI Functions ---


async def _send_prompt(chat_id, context, text):
    """Sends a conversational prompt and stores its ID for later cleanup."""
    prompt_message = await context.bot.send_message(
        chat_id,
        text,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")]]
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    context.user_data["prompt_message_id"] = prompt_message.message_id


async def _prompt_for_year_selection(
    message: Message, context: ContextTypes.DEFAULT_TYPE, title: str, years: List[str]
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


async def _prompt_for_resolution(
    target: Union[Message, int], context: ContextTypes.DEFAULT_TYPE, full_title: str
) -> None:
    """
    Asks the user to select a resolution, either by sending a new message
    or editing an existing one.
    """
    # --- Guard Clause: Ensure user_data exists before use ---
    if context.user_data is None:
        logger.error("Cannot prompt for resolution because user_data is None.")
        # Determine how to send an error message based on the target type
        if isinstance(target, Message):
            await safe_edit_message(
                target,
                text="❌ An error occurred and your session was lost\\. Please start over\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        elif isinstance(target, int):
            await context.bot.send_message(
                target,
                text="❌ An error occurred and your session was lost\\. Please start over\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        return

    context.user_data["search_final_title"] = full_title
    context.user_data["search_media_type"] = "movie"

    keyboard = [
        [
            InlineKeyboardButton("1080p", callback_data="search_resolution_1080p"),
            InlineKeyboardButton("4K (2160p)", callback_data="search_resolution_4k"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = f"Got it: `{escape_markdown(full_title, version=2)}`\\. Now, please select your desired resolution:"

    if isinstance(target, Message):
        await safe_edit_message(
            target,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    elif isinstance(target, int):
        prompt_message = await context.bot.send_message(
            chat_id=target,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        context.user_data["prompt_message_id"] = prompt_message.message_id


def _filter_results_by_resolution(results: List[Dict], resolution: str) -> List[Dict]:
    """Filters search results to only include entries matching the desired resolution."""
    res = resolution.lower()
    patterns = ["2160p", "4k"] if res == "2160p" else ["1080p"]
    return [
        r for r in results if any(p in r.get("title", "").lower() for p in patterns)
    ]


async def _present_search_results(message, context, results, query_str):
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
    original_count = len(results)
    filtered_results = [
        r for r in results if r.get("size_gb", float("inf")) <= MAX_TORRENT_SIZE_GB
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

    for i, result in enumerate(filtered_results[:10]):  # Limit to 10 choices
        button_label = f"{result.get('codec', 'N/A')} | {result.get('size_gb', 0.0):.2f} GB | S: {result.get('seeders', 0)}"
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


def _clear_search_context(context):
    """Clears all search-related keys from user_data."""
    keys_to_clear = [
        "active_workflow",
        "next_action",
        "search_query_title",
        "search_final_title",
        "search_media_type",
        "search_season_number",
    ]
    for key in keys_to_clear:
        context.user_data.pop(key, None)


async def _process_preliminary_results(
    status_message: Message, context: ContextTypes.DEFAULT_TYPE, results: List[Dict]
) -> None:
    """
    Analyzes preliminary search results to decide the next step in the movie workflow.
    """
    # --- Guard Clause: Ensure user_data exists before use ---
    if context.user_data is None:
        logger.error("Cannot process preliminary results because user_data is None.")
        await safe_edit_message(
            status_message,
            text="❌ An error occurred and your session was lost\\. Please start over\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    title = context.user_data.get("search_query_title", "this movie")
    escaped_title = escape_markdown(title, version=2)

    if not results:
        logger.warning(f"Preliminary search for '{title}' yielded no results.")
        await safe_edit_message(
            status_message,
            text=f"❌ No results found for `'{escaped_title}'`\\. Please check the title and try again\\.",
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
        await _prompt_for_year_selection(status_message, context, title, unique_years)

    elif len(unique_years) == 1:
        year = unique_years[0]
        logger.info(
            f"Found one unique year for '{title}': {year}. Proceeding to resolution selection."
        )
        full_title = f"{title} ({year})"
        await _prompt_for_resolution(status_message, context, full_title)

    else:
        logger.warning(
            f"Found results for '{title}', but could not determine any release years."
        )
        message_text = (
            f"ℹ️ Found results for `'{escaped_title}'`, but could not determine a release year\\.\n\n"
            f"Please try the search again and include the year manually \\(e\\.g\\., `{escaped_title} 2023`\\)\\."
        )
        await safe_edit_message(
            status_message, text=message_text, parse_mode=ParseMode.MARKDOWN_V2
        )
