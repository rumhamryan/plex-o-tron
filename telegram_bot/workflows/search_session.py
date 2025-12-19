from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, MutableMapping, Literal, cast

CONTEXT_LOST_MESSAGE = "❓ Search context has expired\\. Please start over\\."


class SearchStep(str, Enum):
    """State machine steps for the search workflow."""

    MOVIE_SCOPE = "movie_scope"
    TITLE = "title"
    YEAR = "year"
    RESOLUTION = "resolution"
    TV_SEASON = "tv_season"
    TV_SCOPE = "tv_scope"
    TV_EPISODE = "tv_episode"
    CONFIRMATION = "confirmation"
    COMPLETE = "complete"


class SearchSessionError(Exception):
    """Raised when a handler requires state that is missing or expired."""

    def __init__(self, user_message: str = CONTEXT_LOST_MESSAGE):
        super().__init__(user_message)
        self.user_message = user_message


@dataclass
class SearchSession:
    """Serializable search session stored in PTB user_data."""

    CodecFilter = Literal["all", "x264", "x265"]

    step: SearchStep = SearchStep.TITLE
    media_type: Literal["movie", "tv"] | None = None
    movie_scope: Literal["single", "collection"] | None = None
    title: str | None = None
    resolved_title: str | None = None
    final_title: str | None = None
    collection_mode: bool = False
    collection_name: str | None = None
    collection_fs_name: str | None = None
    collection_movies: list[dict[str, Any]] = field(default_factory=list)
    collection_exclusions: list[str] = field(default_factory=list)
    collection_resolution: str | None = None
    collection_codec: str | None = None
    collection_seed_size_gb: float | None = None
    collection_seed_uploader: str | None = None
    collection_owned_count: int = 0
    season: int | None = None
    episode: int | None = None
    resolution: str | None = None
    tv_codec: str | None = None
    tv_scope: Literal["single", "season"] | None = None
    prompt_message_id: int | None = None
    season_episode_count: int | None = None
    existing_episodes: list[int] = field(default_factory=list)
    missing_episode_numbers: list[int] | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    results_query: str | None = None
    results_page: int = 0
    results_resolution_filter: str = "all"
    results_codec_filter: CodecFilter = "all"
    results_max_size_gb: float | None = None
    results_generated_at: float | None = None
    allow_detail_change: bool = False

    _SESSION_KEY = "search_session"

    @classmethod
    def from_user_data(
        cls, user_data: MutableMapping[str, Any] | None
    ) -> "SearchSession":
        if not isinstance(user_data, MutableMapping):
            return cls()

        payload = user_data.get(cls._SESSION_KEY)
        if not isinstance(payload, dict):
            return cls()

        step_value = payload.get("step", SearchStep.TITLE.value)
        try:
            step = SearchStep(step_value)
        except ValueError:
            step = SearchStep.TITLE

        raw_missing = payload.get("missing_episode_numbers")
        missing_episode_numbers = (
            list(raw_missing) if isinstance(raw_missing, list) else None
        )

        session = cls(
            step=step,
            media_type=payload.get("media_type"),
            movie_scope=payload.get("movie_scope"),
            title=payload.get("title"),
            resolved_title=payload.get("resolved_title"),
            final_title=payload.get("final_title"),
            collection_mode=bool(payload.get("collection_mode", False)),
            collection_name=payload.get("collection_name"),
            collection_fs_name=payload.get("collection_fs_name"),
            collection_movies=list(payload.get("collection_movies") or []),
            collection_exclusions=list(payload.get("collection_exclusions") or []),
            collection_resolution=payload.get("collection_resolution"),
            collection_codec=payload.get("collection_codec"),
            collection_seed_size_gb=payload.get("collection_seed_size_gb"),
            collection_seed_uploader=payload.get("collection_seed_uploader"),
            collection_owned_count=int(payload.get("collection_owned_count") or 0),
            season=payload.get("season"),
            episode=payload.get("episode"),
            resolution=payload.get("resolution"),
            tv_codec=payload.get("tv_codec"),
            tv_scope=payload.get("tv_scope"),
            prompt_message_id=payload.get("prompt_message_id"),
            season_episode_count=payload.get("season_episode_count"),
            existing_episodes=list(payload.get("existing_episodes") or []),
            missing_episode_numbers=missing_episode_numbers,
            results=list(payload.get("results") or []),
            results_query=payload.get("results_query"),
            results_page=int(payload.get("results_page") or 0),
            results_resolution_filter=payload.get("results_resolution_filter") or "all",
            results_codec_filter=cls.normalize_results_codec_filter(
                payload.get("results_codec_filter")
            ),
            results_max_size_gb=payload.get("results_max_size_gb"),
            results_generated_at=payload.get("results_generated_at"),
            allow_detail_change=bool(payload.get("allow_detail_change")),
        )
        return session

    @property
    def is_active(self) -> bool:
        return self.media_type is not None

    @property
    def effective_title(self) -> str | None:
        return self.resolved_title or self.title

    def advance(self, step: SearchStep) -> None:
        self.step = step

    def set_title(self, title: str, resolved_title: str | None = None) -> None:
        self.title = title
        if resolved_title:
            self.resolved_title = resolved_title

    def set_final_title(self, final_title: str) -> None:
        self.final_title = final_title

    def require_title(self) -> str:
        if not self.effective_title:
            raise SearchSessionError()
        return self.effective_title or ""

    def require_final_title(self) -> str:
        if not self.final_title:
            raise SearchSessionError()
        return self.final_title

    def require_media_type(self) -> str:
        if not self.media_type:
            raise SearchSessionError()
        return self.media_type

    def require_season(self) -> int:
        if self.season is None:
            raise SearchSessionError()
        return int(self.season)

    def require_tv_scope(self) -> str:
        if not self.tv_scope:
            raise SearchSessionError()
        return self.tv_scope

    def require_resolution(self) -> str:
        if not self.resolution:
            raise SearchSessionError()
        return self.resolution

    def consume_prompt_message_id(self) -> int | None:
        message_id = self.prompt_message_id
        self.prompt_message_id = None
        return message_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step.value,
            "media_type": self.media_type,
            "movie_scope": self.movie_scope,
            "title": self.title,
            "resolved_title": self.resolved_title,
            "final_title": self.final_title,
            "collection_mode": self.collection_mode,
            "collection_name": self.collection_name,
            "collection_fs_name": self.collection_fs_name,
            "collection_movies": list(self.collection_movies),
            "collection_exclusions": list(self.collection_exclusions),
            "collection_resolution": self.collection_resolution,
            "collection_codec": self.collection_codec,
            "collection_seed_size_gb": self.collection_seed_size_gb,
            "collection_seed_uploader": self.collection_seed_uploader,
            "collection_owned_count": int(self.collection_owned_count or 0),
            "season": self.season,
            "episode": self.episode,
            "resolution": self.resolution,
            "tv_codec": self.tv_codec,
            "tv_scope": self.tv_scope,
            "prompt_message_id": self.prompt_message_id,
            "season_episode_count": self.season_episode_count,
            "existing_episodes": list(self.existing_episodes),
            "missing_episode_numbers": (
                list(self.missing_episode_numbers)
                if isinstance(self.missing_episode_numbers, list)
                else None
            ),
            "results": list(self.results),
            "results_query": self.results_query,
            "results_page": int(self.results_page or 0),
            "results_resolution_filter": self.results_resolution_filter,
            "results_codec_filter": self.results_codec_filter,
            "results_max_size_gb": self.results_max_size_gb,
            "results_generated_at": self.results_generated_at,
            "allow_detail_change": self.allow_detail_change,
        }

    def save(self, user_data: MutableMapping[str, Any]) -> None:
        user_data[self._SESSION_KEY] = self.to_dict()

    @staticmethod
    def normalize_results_codec_filter(value: Any) -> "SearchSession.CodecFilter":
        """Normalizes persisted codec filter values."""
        if not isinstance(value, str):
            return "all"
        lowered = value.strip().lower()
        if lowered in {"all", "x264", "x265"}:
            return cast(SearchSession.CodecFilter, lowered)
        return "all"


def clear_search_session(user_data: MutableMapping[str, Any] | None) -> None:
    if isinstance(user_data, MutableMapping):
        user_data.pop(SearchSession._SESSION_KEY, None)
