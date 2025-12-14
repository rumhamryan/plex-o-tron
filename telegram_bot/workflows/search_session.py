from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, MutableMapping, Literal

CONTEXT_LOST_MESSAGE = "❓ Search context has expired\\. Please start over\\."


class SearchStep(str, Enum):
    """State machine steps for the search workflow."""

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

    step: SearchStep = SearchStep.TITLE
    media_type: Literal["movie", "tv"] | None = None
    title: str | None = None
    resolved_title: str | None = None
    final_title: str | None = None
    season: int | None = None
    episode: int | None = None
    resolution: str | None = None
    tv_scope: Literal["single", "season"] | None = None
    prompt_message_id: int | None = None
    season_episode_count: int | None = None
    existing_episodes: list[int] = field(default_factory=list)
    missing_episode_numbers: list[int] | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    results_query: str | None = None
    results_page: int = 0
    results_resolution_filter: str = "all"
    results_sort: str = "score"
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
            title=payload.get("title"),
            resolved_title=payload.get("resolved_title"),
            final_title=payload.get("final_title"),
            season=payload.get("season"),
            episode=payload.get("episode"),
            resolution=payload.get("resolution"),
            tv_scope=payload.get("tv_scope"),
            prompt_message_id=payload.get("prompt_message_id"),
            season_episode_count=payload.get("season_episode_count"),
            existing_episodes=list(payload.get("existing_episodes") or []),
            missing_episode_numbers=missing_episode_numbers,
            results=list(payload.get("results") or []),
            results_query=payload.get("results_query"),
            results_page=int(payload.get("results_page") or 0),
            results_resolution_filter=payload.get("results_resolution_filter") or "all",
            results_sort=payload.get("results_sort") or "score",
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
            "title": self.title,
            "resolved_title": self.resolved_title,
            "final_title": self.final_title,
            "season": self.season,
            "episode": self.episode,
            "resolution": self.resolution,
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
            "results_sort": self.results_sort,
            "results_max_size_gb": self.results_max_size_gb,
            "results_generated_at": self.results_generated_at,
            "allow_detail_change": self.allow_detail_change,
        }

    def save(self, user_data: MutableMapping[str, Any]) -> None:
        user_data[self._SESSION_KEY] = self.to_dict()


def clear_search_session(user_data: MutableMapping[str, Any] | None) -> None:
    if isinstance(user_data, MutableMapping):
        user_data.pop(SearchSession._SESSION_KEY, None)
