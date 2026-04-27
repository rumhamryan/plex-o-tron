from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

MediaType = Literal["movie", "tv"]

DEFAULT_TORZNAB_CATEGORIES: dict[str, str] = {
    "movie": "2000",
    "tv": "5000",
}


def _validate_non_empty_string(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")


@dataclass(slots=True)
class DiscoveryRequest:
    """Search intent passed to discovery providers."""

    query: str
    media_type: MediaType
    year: str | int | None = None
    season: int | None = None
    episode: int | None = None
    resolution: str | None = None
    max_size_gib: float | None = None
    min_seeders: int = 0
    base_query_for_filter: str | None = None

    def __post_init__(self) -> None:
        _validate_non_empty_string(self.query, field_name="query")
        if self.media_type not in {"movie", "tv"}:
            raise ValueError("media_type must be either 'movie' or 'tv'.")
        if self.season is not None and self.season <= 0:
            raise ValueError("season must be greater than 0 when provided.")
        if self.episode is not None and self.episode <= 0:
            raise ValueError("episode must be greater than 0 when provided.")
        if self.max_size_gib is not None and self.max_size_gib <= 0:
            raise ValueError("max_size_gib must be greater than 0 when provided.")
        if self.min_seeders < 0:
            raise ValueError("min_seeders must be non-negative.")


@dataclass(slots=True)
class DiscoveryResult:
    """Canonical torrent candidate returned by discovery providers."""

    title: str
    download_url: str
    source: str
    size_bytes: int
    seeders: int
    leechers: int
    info_url: str | None = None
    magnet_url: str | None = None
    info_hash: str | None = None
    uploader: str | None = None
    year: int | None = None
    codec: str | None = None
    resolution: str | None = None
    raw_data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_non_empty_string(self.title, field_name="title")
        _validate_non_empty_string(self.download_url, field_name="download_url")
        _validate_non_empty_string(self.source, field_name="source")
        if self.size_bytes <= 0:
            raise ValueError("size_bytes must be greater than 0.")
        if self.seeders < 0:
            raise ValueError("seeders must be non-negative.")
        if self.leechers < 0:
            raise ValueError("leechers must be non-negative.")


@dataclass(slots=True)
class ProviderConfig:
    """Runtime configuration for one discovery provider."""

    name: str
    type: str
    search_url: str
    enabled: bool = True
    timeout_seconds: float = 8.0
    categories: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TORZNAB_CATEGORIES))

    def __post_init__(self) -> None:
        _validate_non_empty_string(self.name, field_name="name")
        _validate_non_empty_string(self.type, field_name="type")
        _validate_non_empty_string(self.search_url, field_name="search_url")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0.")
