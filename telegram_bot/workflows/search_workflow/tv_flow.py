# telegram_bot/workflows/search_workflow/tv_flow.py

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from ...config import LOG_SCRAPER_STATS, logger
from ...services import plex_service, scraping_service, search_logic
from ...utils import (
    parse_torrent_name,
    safe_edit_message,
    safe_send_message,
)
from ..search_parser import parse_search_query
from ..search_session import (
    CONTEXT_LOST_MESSAGE,
    SearchSession,
    SearchSessionError,
    SearchStep,
    clear_search_session,
)
from .helpers import (
    _coerce_float,
    _coerce_int,
    _infer_resolution_from_title,
    _normalize_release_field,
)
from .movie_collection_flow import COLLECTION_CODEC_CHOICES
from .preferences import _render_search_preferences_prompt
from .results import _filter_results_by_resolution, _log_aggregated_results, _present_search_results
from .state import (
    _get_callback_data,
    _get_session,
    _get_user_data_store,
    _save_session,
    _send_prompt,
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
TV_SEASON_RESOLUTION_CHOICES: tuple[str, ...] = ("720p", "1080p")
TV_SEASON_RESOLUTION_OPTIONS = (("720p", "720p"), ("1080p", "1080p"))
TV_SEASON_CODEC_OPTIONS = (("x264", "x264 / AVC"), ("x265", "x265 / HEVC"))


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
    different_uploader_episodes: list[int] = field(default_factory=list)
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
    await _prompt_for_tv_season_selection(chat_id, context, session, display_title=sanitized_title)


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

    if isinstance(seasons_count, int) and seasons_count > 0 and seasons_count <= MAX_SEASON_BUTTONS:
        buttons = [
            InlineKeyboardButton(str(i), callback_data=f"search_select_season_{i}")
            for i in range(1, seasons_count + 1)
        ]
        keyboard = [buttons[i : i + SEASON_COLUMNS] for i in range(0, len(buttons), SEASON_COLUMNS)]
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")])
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


def _build_tv_scope_keyboard(*, include_change_button: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("Single Episode", callback_data="search_tv_scope_single"),
            InlineKeyboardButton("Entire Season", callback_data="search_tv_scope_season"),
        ]
    ]
    if include_change_button:
        rows.append([InlineKeyboardButton("Change", callback_data="search_tv_change_details")])
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
        titles_map, _ = await scraping_service.fetch_episode_titles_for_season(title, season)
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
                episode_count = await scraping_service.fetch_season_episode_count_from_wikipedia(
                    str(title), int(season)
                )
            except Exception:
                episode_count = None

        if isinstance(episode_count, int) and 0 < episode_count <= MAX_EPISODE_BUTTONS:
            buttons = [
                InlineKeyboardButton(str(i), callback_data=f"search_select_episode_{i}")
                for i in range(1, episode_count + 1)
            ]
            keyboard = [
                buttons[i : i + EPISODE_COLUMNS] for i in range(0, len(buttons), EPISODE_COLUMNS)
            ]
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation")])
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
        episode_count = await scraping_service.fetch_season_episode_count_from_wikipedia(
            title, season
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
        missing_list = [i for i in range(1, int(episode_count) + 1) if i not in existing_eps]

        session.season_episode_count = int(episode_count)
        session.existing_episodes = sorted(existing_eps)
        session.missing_episode_numbers = missing_list
        session.tv_scope = "season"
        session.media_type = "tv"
        session.resolution = None
        session.tv_codec = None
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
        await _prompt_tv_season_preferences(query.message, context, session)


async def _prompt_tv_season_preferences(
    message: Message, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    text = (
        "Choose the resolution and codec for this season search\\.\n"
        "These preferences guide selection toward consistent releases\\."
    )
    await _render_search_preferences_prompt(
        message,
        text=text,
        selected_resolution=session.resolution,
        resolution_options=TV_SEASON_RESOLUTION_OPTIONS,
        resolution_callback_prefix="search_tv_season_resolution_",
        selected_codec=session.tv_codec,
        codec_options=TV_SEASON_CODEC_OPTIONS,
        codec_callback_prefix="search_tv_season_codec_",
        continue_callback_data="search_tv_season_preferences_continue",
        continue_label="Search",
    )


async def _handle_tv_season_resolution_button(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    if not isinstance(query.message, Message):
        return

    choice = _get_callback_data(query).split("_")[-1]
    if choice not in TV_SEASON_RESOLUTION_CHOICES:
        try:
            await query.answer(text="Please choose 720p or 1080p.", show_alert=False)
        except RuntimeError:
            pass
        await _prompt_tv_season_preferences(query.message, context, session)
        return
    session.resolution = choice
    _save_session(context, session)
    await _prompt_tv_season_preferences(query.message, context, session)


async def _handle_tv_season_codec_button(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    if not isinstance(query.message, Message):
        return

    choice = _get_callback_data(query).split("_")[-1].lower()
    if choice not in COLLECTION_CODEC_CHOICES:
        try:
            await query.answer(text="Please choose x264 or x265.", show_alert=False)
        except RuntimeError:
            pass
        await _prompt_tv_season_preferences(query.message, context, session)
        return
    session.tv_codec = choice
    _save_session(context, session)
    await _prompt_tv_season_preferences(query.message, context, session)


async def _handle_tv_season_preferences_continue(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, session: SearchSession
) -> None:
    if not isinstance(query.message, Message):
        return

    if (
        session.resolution not in TV_SEASON_RESOLUTION_CHOICES
        or session.tv_codec not in COLLECTION_CODEC_CHOICES
    ):
        try:
            await query.answer(
                text="Choose both a resolution and codec before searching.",
                show_alert=False,
            )
        except RuntimeError:
            pass
        await _prompt_tv_season_preferences(query.message, context, session)
        return

    title = session.require_title()
    season = session.require_season()

    await _perform_tv_season_search(
        query.message,
        context,
        str(title),
        int(season),
        session=session,
    )


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
        summary_text = escape_markdown(f"Found a season pack for Season {season}.", version=2)
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
                InlineKeyboardButton("✅ Confirm", callback_data="confirm_season_download"),
                InlineKeyboardButton("⛔ Reject", callback_data="reject_season_pack"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_operation"),
            ]
        ]
    else:
        keyboard = [
            [
                InlineKeyboardButton("✅ Confirm", callback_data="confirm_season_download"),
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


def _target_size_for_resolution(resolution: str | None) -> float:
    if not resolution:
        return DEFAULT_SIZE_TARGET_GB
    return RESOLUTION_SIZE_TARGETS.get(resolution, DEFAULT_SIZE_TARGET_GB)


def _select_consistent_episode_set(
    candidates_by_episode: dict[int, list[EpisodeCandidate]],
    target_res: str = "all",
    target_codec: str = "all",
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
        primary_resolution = resolution_counter.most_common(1)[0][0] if resolution_counter else None
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
        logger.info("[SEARCH] No consistent release found across episodes. Using top results.")
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
    different_uploader_eps: list[int] = []
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

            # Check if it's a true fallback (codec/res mismatch) or just different uploader
            res_mismatch = (
                target_res != "all" and (selected.resolution or "").lower() != target_res.lower()
            )
            codec_mismatch = (
                target_codec != "all" and (selected.codec or "").lower() != target_codec.lower()
            )

            if res_mismatch or codec_mismatch:
                fallback_eps.append(ep)
            else:
                different_uploader_eps.append(ep)

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
        different_uploader_episodes=different_uploader_eps,
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
        details.append("\nResolution: " + escape_markdown(summary.resolution.lower(), version=2))
    if summary.avg_size_gb is not None:
        details.append(escape_markdown(f"\nAverage size: {summary.avg_size_gb:.2f} GB", version=2))
        total_size = escape_markdown(
            str(round(float(summary.avg_size_gb) * float(summary.matched_count), 2)),
            version=2,
        )
        details.append(escape_markdown(f"\nTotal Size: {total_size} GB"))
    base_line = f"Consistency: {label} \\({escape_markdown(coverage, version=2)}\\)"
    if details:
        base_line += " ".join(details)

    lines = [base_line]
    if summary.different_uploader_episodes:
        diff_str = ", ".join(f"E{num:02d}" for num in summary.different_uploader_episodes)
        lines.append(f"ℹ️ Different uploader: {escape_markdown(diff_str, version=2)}")
    if summary.fallback_episodes:
        fallback_str = ", ".join(f"E{num:02d}" for num in summary.fallback_episodes)
        lines.append(f"⚠️ Fallback episodes: {escape_markdown(fallback_str, version=2)}")
    return "\n".join(lines)


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
    session.results_codec_filter = SearchSession.normalize_results_codec_filter(target_codec)
    _save_session(context, session)

    await safe_edit_message(
        message,
        text=(
            f"🔍 Searching for Season {escape_markdown(str(season), version=2)} "
            f"of *{escape_markdown(title, version=2)}* in *{escape_markdown(target_res, version=2)}*\\.\\.\\."
        ),
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    season_queries = [f"{title} S{season:02d}", f"{title} Season {season}"]
    found_results: list[dict[str, Any]] = []

    # Add resolution hints to query if specific
    if target_res in ("720p", "1080p"):
        season_queries = [f"{q} {target_res}" for q in season_queries] + season_queries

    for q in season_queries:
        res = await search_logic.orchestrate_searches(q, "tv", context, base_query_for_filter=title)
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
                p for p in filtered_packs if (p.get("codec") or "").lower() == target_codec.lower()
            ]

        # Fallback to unfiltered if strict filtering yields nothing
        candidates_to_use = filtered_packs if filtered_packs else pack_candidates

        season_pack_torrent = (
            max(candidates_to_use, key=lambda x: x.get("score", 0)) if candidates_to_use else None
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
        if target_res in ("720p", "1080p"):
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
                r for r in filtered_eps if (r.get("codec") or "").lower() == target_codec.lower()
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
            episode_candidates,
            target_res=target_res,
            target_codec=target_codec,
        )

    for candidate in selected_candidates:
        fallback_tag = ""
        if consistency_summary and candidate.episode in consistency_summary.fallback_episodes:
            # Check if it's a true fallback (codec/res mismatch) or just different uploader
            res_mismatch = (
                target_res != "all" and (candidate.resolution or "").lower() != target_res.lower()
            )
            codec_mismatch = (
                target_codec != "all" and (candidate.codec or "").lower() != target_codec.lower()
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


async def handle_reject_season_pack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
