# telegram_bot/workflows/search_workflow/results.py

import re
import time
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from ...config import LOG_SCRAPER_STATS, logger
from ...utils import (
    safe_edit_message,
)
from ..search_session import (
    SearchSession,
    SearchSessionError,
    SearchStep,
)
from .state import _end_search_workflow, _get_session, _save_session

RESULTS_PAGE_SIZE = 5
RESULTS_SESSION_TTL_SECONDS = 15 * 60
FOUR_K_SIZE_MULTIPLIER = 2.0
RESOLUTION_FILTERS: tuple[str, ...] = ("all", "720p", "1080p", "2160p")
RESULTS_EXPIRED_MESSAGE = "? These search results have expired\\. Please start a new search\\."


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
        "size_gib",
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
    max_size_gib: float | None = None,
    initial_resolution: str | None = None,
    initial_codec: str | None = None,
):
    """Persists result metadata on the session and renders the first page."""
    if session is None:
        session = _get_session(context)

    # Sort results by score (descending) to ensure aggregation hasn't broken ordering
    results.sort(key=lambda x: x.get("score", 0), reverse=True)

    escaped_query = escape_markdown(query_str, version=2)

    if not results:
        await _end_search_workflow(
            context,
            message.chat_id,
            f"❌ No results found for '`{escaped_query}`' across all configured sites\\.",
            source_message=message,
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
    session.results_codec_filter = SearchSession.normalize_results_codec_filter(initial_codec)
    session.results_max_size_gib = (
        float(max_size_gib) if isinstance(max_size_gib, (int, float)) else None
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


def _result_size_gib(result: dict[str, Any]) -> float | None:
    return _safe_float(result.get("size_gib", result.get("size_gb")))


def _determine_size_cap(
    session: SearchSession, resolution_filter: str | None = None
) -> float | None:
    cap = session.results_max_size_gib
    if cap is None:
        return None
    active = _normalize_resolution_filter(resolution_filter or session.results_resolution_filter)
    if active == "2160p":
        return cap * FOUR_K_SIZE_MULTIPLIER
    return cap


def _compute_filtered_results(session: SearchSession) -> list[dict[str, Any]]:
    working = list(session.results or [])
    if not working:
        return []

    codec_filter = SearchSession.normalize_results_codec_filter(session.results_codec_filter)
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
            size_value = _result_size_gib(item)
            if size_value is None or size_value <= size_cap:
                limited.append(item)
        working = limited

    return working


def _format_result_button_label(result: dict[str, Any]) -> str:
    codec = result.get("codec") or "N/A"
    seeders = _safe_int(result.get("seeders"))
    size_value = _result_size_gib(result)
    size_text = f"{size_value:.2f} GiB" if size_value is not None else "? GiB"
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
    active_codec = SearchSession.normalize_results_codec_filter(session.results_codec_filter)
    for value, label in (("all", "All"), ("x264", "x264"), ("x265", "x265")):
        prefix = "🟢" if active_codec == value else ""
        codec_row.append(
            InlineKeyboardButton(
                f"{prefix}{label}",
                callback_data=f"search_results_filter_codec_{value}",
            )
        )
    keyboard.append(codec_row)

    if session.allow_detail_change and session.media_type == "tv" and session.tv_scope == "single":
        keyboard.append(
            [InlineKeyboardButton("🔄️ Change", callback_data="search_tv_change_details")]
        )

    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")])
    return keyboard


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
        session.results_page = min(max(session.results_page, 0), max(total_pages - 1, 0))

    _save_session(context, session)

    raw_query = session.results_query or "this title"
    trim = slice(-6)
    processed_query = raw_query[trim]
    escaped_query = escape_markdown(processed_query, version=2)
    active_codec = SearchSession.normalize_results_codec_filter(session.results_codec_filter)
    codec_label = "  All  " if active_codec == "all" else active_codec
    resolution_label = (
        "   All    "
        if session.results_resolution_filter == "all"
        else session.results_resolution_filter.lower()
    )
    filters_text = f"Resolution: *{resolution_label}* \\| Codec filter: *{codec_label}*"

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
