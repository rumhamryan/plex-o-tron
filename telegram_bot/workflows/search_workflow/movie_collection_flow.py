# telegram_bot/workflows/search_workflow/movie_collection_flow.py

import asyncio
import os
import re
from datetime import date
from typing import Any

import wikipedia
from bs4 import BeautifulSoup, Tag
from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from ...config import LOG_SCRAPER_STATS, MAX_TORRENT_SIZE_GIB, logger
from ...services import scraping_service, search_logic
from ...services.media_manager import _get_path_size_bytes
from ...services.scrapers.wikipedia.dates import _extract_release_date_iso
from ...services.scrapers.wikipedia.fetch import _fetch_html_from_page
from ...ui.messages import format_media_summary
from ...utils import (
    format_bytes,
    parse_torrent_name,
    safe_edit_message,
    safe_send_message,
    sanitize_collection_name,
)
from ..search_session import (
    SearchSession,
    SearchStep,
    clear_search_session,
)
from .helpers import (
    _coerce_float,
    _coerce_int,
    _ensure_identifier,
    _format_collection_movie_label,
    _normalize_collection_movie_title,
    _normalize_label,
    _normalize_release_field,
    _parse_release_iso,
)
from .collection_reconciliation import (
    CollectionMovieResolution,
    locate_collection_movie_matches,
    reconcile_collection_movie,
)
from .results import (
    FOUR_K_SIZE_MULTIPLIER,
    _filter_results_by_resolution,
    _log_aggregated_results,
    _normalize_resolution_filter,
)
from .state import _get_callback_data, _get_user_data_store, _save_session

COLLECTION_CODEC_CHOICES: tuple[str, ...] = ("x264", "x265")
COLLECTION_RESOLUTION_CHOICES: tuple[str, ...] = ("1080p", "2160p")
COLLECTION_DEFAULT_RESOLUTION = "1080p"
COLLECTION_DEFAULT_CODEC = "x265"
_COLLECTION_RESOLUTION_ALIASES = {
    "1080p": "1080p",
    "2160p": "2160p",
    "4k": "2160p",
}
_COLLECTION_CODEC_ALIASES = {
    "x264": "x264",
    "h264": "x264",
    "x265": "x265",
    "hevc": "x265",
}


def _resolve_collection_search_template(
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[str, str]:
    """Resolves the collection template from configured movie search preferences."""
    search_config = (context.bot_data or {}).get("SEARCH_CONFIG", {})
    preferences = search_config.get("preferences", {}) if isinstance(search_config, dict) else {}
    movie_preferences = preferences.get("movies", {}) if isinstance(preferences, dict) else {}

    resolution = _resolve_collection_preference_value(
        movie_preferences.get("resolutions"),
        aliases=_COLLECTION_RESOLUTION_ALIASES,
        preference_order=COLLECTION_RESOLUTION_CHOICES,
        fallback=COLLECTION_DEFAULT_RESOLUTION,
    )
    codec = _resolve_collection_preference_value(
        movie_preferences.get("codecs"),
        aliases=_COLLECTION_CODEC_ALIASES,
        preference_order=COLLECTION_CODEC_CHOICES,
        fallback=COLLECTION_DEFAULT_CODEC,
    )
    return resolution, codec


def _resolve_collection_preference_value(
    raw_preferences: Any,
    *,
    aliases: dict[str, str],
    preference_order: tuple[str, ...],
    fallback: str,
) -> str:
    """Selects the highest-ranked supported preference from config values."""
    if not isinstance(raw_preferences, dict):
        return fallback

    order_index = {value: index for index, value in enumerate(preference_order)}
    best_scores: dict[str, float] = {}
    for raw_key, raw_value in raw_preferences.items():
        if not isinstance(raw_key, str):
            continue
        normalized = aliases.get(raw_key.strip().lower())
        if normalized not in order_index:
            continue
        score = _coerce_float(raw_value)
        if score is None:
            continue
        previous = best_scores.get(normalized)
        if previous is None or score > previous:
            best_scores[normalized] = score

    if not best_scores:
        return fallback

    return min(
        best_scores,
        key=lambda value: (-best_scores[value], order_index[value]),
    )


def _format_collection_lookup_phase(title: str, phase: str, detail: str | None = None) -> str:
    escaped_title = escape_markdown(title, version=2)
    if phase == "find":
        return f"🧩 Wikipedia lookup: searching for *{escaped_title}* franchise…"
    if phase == "review":
        return "🧩 Wikipedia lookup: reviewing Wikipedia franchise candidates…"
    if phase == "compare":
        return "🧩 Wikipedia lookup: comparing likely franchise pages…"
    if phase == "inspect":
        return "🧩 Wikipedia lookup: opening likely franchise pages…"
    if phase == "score":
        if detail:
            escaped_detail = escape_markdown(detail, version=2)
            return (
                "🧩 Wikipedia lookup: extracting films from "
                f"*{escaped_detail}* and scoring the franchise match…"
            )
        return "🧩 Wikipedia lookup: extracting film lists and scoring franchise candidates…"
    return "🧩 Wikipedia lookup: validating collection entries and release dates…"


def _classify_collection_release(
    raw_movie: dict[str, Any], today: date
) -> tuple[str, int | None, date | None]:
    """Classifies a franchise entry using release_date first, then year as fallback."""
    parsed_year = _coerce_int(raw_movie.get("year"))
    release_date = _parse_release_iso(raw_movie.get("release_date"))
    if release_date is not None:
        if release_date > today:
            return "unreleased", parsed_year, release_date
        return "released", parsed_year, release_date
    if parsed_year is None:
        return "unknown", None, None
    if parsed_year > today.year:
        return "unreleased", parsed_year, None
    return "released", parsed_year, None


def _extract_release_date_from_movie_html(html: str) -> date | None:
    soup = BeautifulSoup(html, "html.parser")
    infobox = soup.find("table", class_=re.compile(r"\binfobox\b"))
    if not isinstance(infobox, Tag):
        return None

    for row in infobox.find_all("tr"):
        if not isinstance(row, Tag):
            continue
        header = row.find("th")
        if not isinstance(header, Tag):
            continue
        if "release" not in header.get_text(" ", strip=True).casefold():
            continue
        value = row.find("td")
        if not isinstance(value, Tag):
            continue
        release_iso = _extract_release_date_iso(value.get_text(" ", strip=True))
        if release_iso:
            return _parse_release_iso(release_iso)
    return None


async def _resolve_current_year_release_date(title: str, year: int) -> date | None:
    """Best-effort lookup for ambiguous current-year entries."""
    lookup_title = _normalize_collection_movie_title(title, year)
    corrected_title: str | None = None
    try:
        _, corrected_title = await scraping_service.fetch_movie_years_from_wikipedia(lookup_title)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[COLLECTION] Could not refine current-year movie title '%s' (%s): %s",
            title,
            year,
            exc,
        )

    candidate_queries: list[str] = []
    for base_title in (corrected_title, lookup_title, title):
        if not base_title:
            continue
        normalized_title = _normalize_collection_movie_title(base_title, year)
        if not normalized_title:
            continue
        candidate_queries.append(normalized_title)
        candidate_queries.append(f"{normalized_title} (film)")
        candidate_queries.append(f"{normalized_title} ({year} film)")

    seen_queries: set[str] = set()
    for query in candidate_queries:
        cleaned_query = query.strip()
        if not cleaned_query or cleaned_query in seen_queries:
            continue
        seen_queries.add(cleaned_query)
        try:
            page = await asyncio.to_thread(
                wikipedia.page, cleaned_query, auto_suggest=False, redirect=True
            )
        except Exception:  # noqa: BLE001
            continue
        html = await _fetch_html_from_page(page)
        if not html:
            continue
        if release_date := _extract_release_date_from_movie_html(html):
            return release_date

    return None


async def _resolve_collection_release(
    raw_movie: dict[str, Any], today: date
) -> tuple[str, int | None, date | None]:
    release_state, parsed_year, release_date = _classify_collection_release(raw_movie, today)
    if release_date is not None or parsed_year != today.year:
        return release_state, parsed_year, release_date

    raw_title = raw_movie.get("title") or raw_movie.get("name")
    title = str(raw_title).strip() if raw_title is not None else ""
    if not title:
        return release_state, parsed_year, release_date

    try:
        resolved_release_date = await _resolve_current_year_release_date(title, parsed_year)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[COLLECTION] Current-year release lookup failed for '%s' (%s): %s",
            title,
            parsed_year,
            exc,
        )
        return release_state, parsed_year, release_date

    if resolved_release_date is None:
        return release_state, parsed_year, release_date
    if resolved_release_date > today:
        logger.info(
            "[COLLECTION] Current-year release resolved for '%s' (%s): %s -> unreleased",
            title,
            parsed_year,
            resolved_release_date.isoformat(),
        )
        return "unreleased", parsed_year, resolved_release_date
    logger.info(
        "[COLLECTION] Current-year release resolved for '%s' (%s): %s -> released",
        title,
        parsed_year,
        resolved_release_date.isoformat(),
    )
    return "released", parsed_year, resolved_release_date


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
        _format_collection_lookup_phase(display_title, "find"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    async def _update_lookup_phase(phase: str, detail: str | None = None) -> None:
        await safe_edit_message(
            status_message,
            text=_format_collection_lookup_phase(display_title, phase, detail),
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    try:
        result = await scraping_service.fetch_movie_franchise_details(
            display_title,
            progress_callback=_update_lookup_phase,
        )
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
    await safe_edit_message(
        status_message,
        text=_format_collection_lookup_phase(display_title, "validate"),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    normalized_movies: list[dict[str, Any]] = []
    today = date.today()
    unreleased_count = 0
    unknown_count = 0
    for idx, raw_movie in enumerate(movies or []):
        raw_title = raw_movie.get("title") or raw_movie.get("name") or display_title
        title_str = str(raw_title).strip() or display_title
        release_state, parsed_year, release_date = await _resolve_collection_release(
            raw_movie, today
        )
        if release_state == "unreleased":
            unreleased_count += 1
            continue
        if release_state != "released":
            unknown_count += 1
            continue
        normalized_title = _normalize_collection_movie_title(
            title_str,
            parsed_year,
            release_date.isoformat() if release_date is not None else raw_movie.get("release_date"),
        )
        entry = {
            "title": normalized_title,
            "year": parsed_year,
            "identifier": _ensure_identifier({"title": normalized_title, "year": parsed_year}, idx),
            "owned": False,
            "queued": False,
            "release_date": release_date.isoformat() if release_date is not None else None,
        }
        normalized_movies.append(entry)

    if not normalized_movies:
        if unreleased_count and not unknown_count:
            logger.info(
                "[COLLECTION] Franchise '%s' matched for '%s' has no released titles yet (%d unreleased entries).",
                franchise_name,
                display_title,
                unreleased_count,
            )
            message_text = (
                f"⚠️ The detected franchise for "
                f"*{escape_markdown(display_title, version=2)}* has no released titles available yet\\.\n"
                "Please try again once those movies premiere or pick another franchise\\."
            )
        else:
            if unreleased_count or unknown_count:
                logger.info(
                    "[COLLECTION] Franchise '%s' matched for '%s' has no queueable titles (%d unreleased, %d missing release metadata).",
                    franchise_name,
                    display_title,
                    unreleased_count,
                    unknown_count,
                )
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
    session.collection_fs_name = sanitize_collection_name(franchise_name)
    session.collection_movies = normalized_movies
    session.collection_exclusions = []
    session.collection_resolution = None
    session.collection_codec = None
    session.collection_seed_size_gib = None
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
    logger.info(
        "[COLLECTION] Prompting use/cancel confirmation for '%s' with titles: %s",
        franchise,
        ", ".join(_format_collection_movie_label(movie) for movie in movies) or "(none)",
    )
    preview_lines: list[str] = []
    for movie in movies:
        label = escape_markdown(_format_collection_movie_label(movie), version=2)
        preview_lines.append(f"• {label}")

    summary = "\n".join(preview_lines)
    text = (
        f"🎬 *{escape_markdown(franchise, version=2)}* contains "
        f"*{len(movies)}* film{'s' if len(movies) != 1 else ''}\\.\n"
        f"{summary}\n\n"
        "Use this collection?"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Use Collection", callback_data="search_collection_accept")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")],
        ]
    )
    await safe_edit_message(
        message,
        text=text,
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def _handle_collection_accept(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    """Prepares filesystem state and applies the configured collection template."""
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

    session.collection_resolution, session.collection_codec = _resolve_collection_search_template(
        context
    )
    session.collection_owned_count = owned_count
    session.prompt_message_id = query.message.message_id
    session.advance(SearchStep.CONFIRMATION)
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
        and m.get("reconciliation_status") != "ambiguous"
        and not m.get("queued")
        and m["identifier"] not in session.collection_exclusions
    ]
    downloadable = [
        m
        for m in movies
        if not m.get("owned")
        and m.get("reconciliation_status") != "ambiguous"
        and not m.get("queued")
    ]
    owned_count = sum(1 for m in movies if m.get("owned"))
    queued_count = sum(1 for m in movies if m.get("queued"))
    ambiguous_count = sum(1 for m in movies if m.get("reconciliation_status") == "ambiguous")
    franchise = session.collection_name or "Franchise"
    template_resolution = escape_markdown(
        session.collection_resolution or COLLECTION_DEFAULT_RESOLUTION,
        version=2,
    )
    template_codec = escape_markdown(
        (session.collection_codec or COLLECTION_DEFAULT_CODEC).upper(),
        version=2,
    )

    text_lines = [
        f"🎬 Preparing *{escape_markdown(franchise, version=2)}* collection\\.",
        f"Template from config: *{template_resolution}* / *{template_codec}*\\.",
        "Tap a title to remove it from this run\\.",
        f"Ready to download: *{len(available)}* / {len(downloadable)} remaining movies\\.",
    ]
    if owned_count:
        text_lines.append(f"📁 Owned: {owned_count}")
    if queued_count:
        text_lines.append(f"⏳ Already queued: {queued_count}")
    if ambiguous_count:
        text_lines.append(f"⚠️ Needs manual review: {ambiguous_count}")
    if not downloadable:
        if ambiguous_count:
            text_lines.append(
                "No additional downloads are ready\\. Remaining titles already exist, are queued, or need review\\."
            )
        else:
            text_lines.append("Everything in this franchise already exists or is queued\\.")
    text = "\n".join(text_lines)

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for movie in movies:
        label = _format_collection_movie_label(movie)
        identifier = movie["identifier"]
        prefix = ""
        if movie.get("owned"):
            prefix = "📁 "
        elif movie.get("reconciliation_status") == "ambiguous":
            prefix = "⚠️ "
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

    can_continue = bool(downloadable or owned_count)
    if can_continue:
        confirm_label = "✅ Confirm Selection" if downloadable else "✅ Continue"
        keyboard_rows.append(
            [InlineKeyboardButton(confirm_label, callback_data="search_collection_confirm")]
        )
    keyboard_rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")])

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
    if (
        not movie
        or movie.get("owned")
        or movie.get("queued")
        or movie.get("reconciliation_status") == "ambiguous"
    ):
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
        and m.get("reconciliation_status") != "ambiguous"
        and not m.get("queued")
        and m["identifier"] not in (session.collection_exclusions or [])
    ]
    owned_summaries = await _collect_owned_collection_summaries(context, session)
    if not movies and not owned_summaries:
        await query.answer(text="Select at least one movie to continue.", show_alert=True)
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
    if context.user_data is not None:
        context.user_data.pop("active_workflow", None)


async def _prepare_collection_directory(
    context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> int:
    """Checks for existing movies and stores structured ownership metadata."""
    movies_root, franchise_dir = _resolve_collection_paths(
        context, session.collection_name, session.collection_fs_name
    )

    owned = 0
    for movie in session.collection_movies or []:
        label = _format_collection_movie_label(movie)
        matches = await asyncio.to_thread(
            locate_collection_movie_matches,
            movies_root,
            franchise_dir,
            label,
        )
        if len(matches) == 1:
            match = matches[0]
            movie["owned"] = True
            movie["existing_path"] = match.path
            movie["existing_location"] = match.location
            movie["already_in_collection"] = match.location == "collection"
            movie["reconciliation_status"] = (
                "already_in_collection"
                if movie["already_in_collection"]
                else "available_outside_collection"
            )
            owned += 1
        elif len(matches) > 1:
            movie["owned"] = False
            movie["existing_path"] = None
            movie["existing_location"] = None
            movie["already_in_collection"] = False
            movie["reconciliation_status"] = "ambiguous"
        else:
            movie["owned"] = False
            movie["existing_path"] = None
            movie["existing_location"] = None
            movie["already_in_collection"] = False
            movie["reconciliation_status"] = "missing"
    _save_session(context, session)
    return owned


async def _ensure_existing_movie_in_collection(
    root_path: str, franchise_dir: str, label: str
) -> bool:
    """Moves an existing movie file/folder into the collection directory if present."""
    resolution = await reconcile_collection_movie(root_path, franchise_dir, label)
    return resolution.status in {"already_in_collection", "moved_to_collection"}


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
    safe_name = sanitize_collection_name(final_name)
    franchise_dir = os.path.join(movies_root, safe_name)
    return movies_root, franchise_dir


async def finalize_movie_collection(
    context: ContextTypes.DEFAULT_TYPE, collection_meta: dict[str, Any]
) -> dict[str, Any]:
    """
    Finalizes a movie collection run by creating the franchise directory
    and moving all associated movies (new and existing) into it.
    """
    summary: dict[str, Any] = {
        "collection_dir": None,
        "results": [],
        "moved_count": 0,
        "already_in_collection_count": 0,
        "missing_count": 0,
        "conflict_count": 0,
        "ambiguous_count": 0,
        "error_count": 0,
        "organized_movies": [],
        "missing_movies": [],
        "conflict_movies": [],
        "ambiguous_movies": [],
        "error_movies": [],
    }
    try:
        collection_name = collection_meta.get("name")
        collection_fs_name = collection_meta.get("fs_name")
        movies = collection_meta.get("movies") or []

        movies_root, franchise_dir = _resolve_collection_paths(
            context, collection_name, collection_fs_name
        )
        summary["collection_dir"] = franchise_dir

        await asyncio.to_thread(os.makedirs, franchise_dir, exist_ok=True)

        results: list[CollectionMovieResolution] = []
        for movie in movies:
            label = _format_collection_movie_label(movie)
            resolution = await reconcile_collection_movie(movies_root, franchise_dir, label)
            results.append(resolution)
            if resolution.status == "moved_to_collection":
                summary["moved_count"] += 1
                summary["organized_movies"].append(dict(movie))
            elif resolution.status == "already_in_collection":
                summary["already_in_collection_count"] += 1
                summary["organized_movies"].append(dict(movie))
            elif resolution.status == "missing":
                summary["missing_count"] += 1
                summary["missing_movies"].append(dict(movie))
            elif resolution.status == "conflict":
                summary["conflict_count"] += 1
                summary["conflict_movies"].append(dict(movie))
            elif resolution.status == "ambiguous":
                summary["ambiguous_count"] += 1
                summary["ambiguous_movies"].append(dict(movie))
            elif resolution.status == "error":
                summary["error_count"] += 1
                summary["error_movies"].append(dict(movie))
        summary["results"] = results

        logger.info(
            (
                "[COLLECTION] Finalized reorganization for '%s' into %s "
                "(moved=%d, already=%d, missing=%d, conflicts=%d, ambiguous=%d, errors=%d)"
            ),
            collection_name,
            franchise_dir,
            summary["moved_count"],
            summary["already_in_collection_count"],
            summary["missing_count"],
            summary["conflict_count"],
            summary["ambiguous_count"],
            summary["error_count"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("[COLLECTION] Reorganization failed: %s", exc)
        summary["error_count"] += 1
    return summary


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
    for movie in owned_movies:
        label = _format_collection_movie_label(movie)
        entry_path = movie.get("existing_path")
        if not entry_path:
            matches = await asyncio.to_thread(
                locate_collection_movie_matches,
                movies_root,
                franchise_dir,
                label,
            )
            if len(matches) == 1:
                entry_path = matches[0].path

        size_label: str | None = None
        if isinstance(entry_path, str) and entry_path:
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
            text=(f"🔍 Searching for *{escape_markdown(label, version=2)}* \\({idx}/{total}\\)…"),
            parse_mode=ParseMode.MARKDOWN_V2,
        )

        year_value = movie.get("year")
        year_kw = str(year_value) if isinstance(year_value, int) else None

        # Allow size override for 4K
        max_size: float = float(MAX_TORRENT_SIZE_GIB)
        if session.collection_resolution == "2160p":
            max_size *= FOUR_K_SIZE_MULTIPLIER

        results = await search_logic.orchestrate_searches(
            label, "movie", context, year=year_kw, max_size_gib=max_size
        )

        if LOG_SCRAPER_STATS:
            _log_aggregated_results(label, results)

        candidate = _pick_collection_candidate(
            results,
            session.collection_resolution,
            session.collection_codec,
            session.collection_seed_size_gib,
            session.collection_seed_uploader,
        )
        if not candidate:
            missing.append(label)
            continue
        link = candidate.get("page_url")
        if not link:
            missing.append(label)
            continue

        size_value = _coerce_float(candidate.get("size_gib", candidate.get("size_gb")))
        if session.collection_seed_size_gib is None and size_value is not None:
            session.collection_seed_size_gib = size_value
        uploader_value = _normalize_release_field(candidate.get("uploader"), "Anonymous")
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
                "size_gib": candidate.get("size_gib", candidate.get("size_gb")),
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
        size_val = _coerce_float(item.get("size_gib", item.get("size_gb")))
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
        _filter_results_by_resolution(results, target_res) if target_res != "all" else list(results)
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
    t5 = _filter_results_by_resolution(results, target_res) if target_res != "all" else []
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
    ambiguous_movies = [
        movie
        for movie in session.collection_movies or []
        if movie.get("reconciliation_status") == "ambiguous"
    ]
    if ambiguous_movies:
        labels = "\n".join(
            f"• {escape_markdown(_format_collection_movie_label(movie), version=2)}"
            for movie in ambiguous_movies
        )
        text += f"\n\n⚠️ Existing library matches need review and were skipped:\n{labels}"
    if missing:
        skipped = "\n".join(f"• {escape_markdown(label, version=2)}" for label in missing)
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
