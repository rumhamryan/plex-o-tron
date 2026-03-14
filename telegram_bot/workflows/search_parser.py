from __future__ import annotations

import re
from dataclasses import dataclass

YEAR_PATTERN = re.compile(r"\b(19\d{2}|20\d{2})\b")
SXXEYY_PATTERN = re.compile(r"(?i)\bS(\d{1,2})E(\d{1,2})\b")
SEASON_WITH_OPTIONAL_EPISODE_PATTERN = re.compile(
    r"(?i)\bSeason\s+(\d{1,2})(?:\s*(?:Episode|Ep)\s*(\d{1,2}))?\b"
)
SEASON_ONLY_PATTERN = re.compile(r"(?i)\bS(\d{1,2})\b")
EPISODE_ONLY_PATTERN = re.compile(r"(?i)\b(?:Episode|Ep)\.?\s*(\d{1,2})\b")
RESOLUTION_PATTERN = re.compile(r"(?i)\b(2160p|1080p|720p|4k|uhd)\b")
CODEC_PATTERN = re.compile(r"(?i)\b(x265|h265|hevc|x264|h264|avc)\b")
SANITIZE_PATTERN = re.compile(r"[^\w\s-]")


@dataclass(frozen=True)
class ParsedSearchQuery:
    """Represents structured hints extracted from a raw user query."""

    title: str
    year: str | None = None
    season: int | None = None
    episode: int | None = None
    resolution: str | None = None
    codec: str | None = None

    @property
    def has_season(self) -> bool:
        return self.season is not None

    @property
    def has_episode(self) -> bool:
        return self.episode is not None

    @property
    def has_media_preferences(self) -> bool:
        return bool(self.resolution and self.codec)


def parse_search_query(query: str) -> ParsedSearchQuery:
    """
    Extracts structured hints (year, season, episode) from user input while preserving
    the cleaned base title for downstream search logic.
    """

    raw_text = (query or "").strip()
    if not raw_text:
        return ParsedSearchQuery(title="")

    removal_spans: list[tuple[int, int]] = []
    token_spans: list[tuple[int, int]] = []
    working_text = raw_text
    season: int | None = None
    episode: int | None = None
    year: str | None = None
    resolution: str | None = None
    codec: str | None = None

    def _consume_span(span: tuple[int, int], *, track_token: bool = True) -> None:
        nonlocal working_text
        start, end = span
        if start < 0 or end <= start:
            return
        removal_spans.append((start, end))
        if track_token:
            token_spans.append((start, end))
        working_text = f"{working_text[:start]}{' ' * (end - start)}{working_text[end:]}"

    match = SXXEYY_PATTERN.search(working_text)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
        _consume_span(match.span())
    else:
        match = SEASON_WITH_OPTIONAL_EPISODE_PATTERN.search(working_text)
        if match:
            season = int(match.group(1))
            if match.group(2):
                episode = int(match.group(2))
            _consume_span(match.span())

    if season is None:
        season_match = SEASON_ONLY_PATTERN.search(working_text)
        if season_match:
            season = int(season_match.group(1))
            _consume_span(season_match.span())

    if episode is None:
        episode_match = EPISODE_ONLY_PATTERN.search(working_text)
        if episode_match:
            episode = int(episode_match.group(1))
            _consume_span(episode_match.span())

    year_match = YEAR_PATTERN.search(raw_text)
    if year_match:
        year = year_match.group(1)
        removal_spans.append(year_match.span())

    resolution_match = RESOLUTION_PATTERN.search(working_text)
    if resolution_match:
        resolution = _normalize_resolution_hint(resolution_match.group(1))
        _consume_span(resolution_match.span())

    codec_match = CODEC_PATTERN.search(working_text)
    if codec_match:
        codec = _normalize_codec_hint(codec_match.group(1))
        _consume_span(codec_match.span())

    cleaned_title = _strip_spans(raw_text, removal_spans)

    prioritized_source: str | None = None
    if token_spans:
        first_start = min(span[0] for span in token_spans)
        last_end = max(span[1] for span in token_spans)
        prefix = raw_text[:first_start].strip()
        suffix = raw_text[last_end:].strip()
        prioritized_source = prefix or suffix or None

    target_title = prioritized_source or cleaned_title
    target_title = _strip_known_hints(target_title)
    target_title = SANITIZE_PATTERN.sub("", target_title)
    target_title = re.sub(r"\s+", " ", target_title).strip(" _.-")

    return ParsedSearchQuery(
        title=target_title,
        year=year,
        season=season,
        episode=episode,
        resolution=resolution,
        codec=codec,
    )


def _strip_spans(value: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return value

    parts: list[str] = []
    last_index = 0
    for start, end in sorted(spans, key=lambda s: s[0]):
        if start < last_index:
            continue
        parts.append(value[last_index:start])
        last_index = end
    parts.append(value[last_index:])
    return "".join(parts)


def _normalize_resolution_hint(value: str) -> str | None:
    lowered = value.strip().lower()
    if lowered in {"4k", "uhd", "2160p"}:
        return "2160p"
    if lowered in {"1080p", "720p"}:
        return lowered
    return None


def _normalize_codec_hint(value: str) -> str | None:
    lowered = value.strip().lower()
    if lowered in {"x265", "h265", "hevc"}:
        return "x265"
    if lowered in {"x264", "h264", "avc"}:
        return "x264"
    return None


def _strip_known_hints(value: str) -> str:
    cleaned = YEAR_PATTERN.sub(" ", value)
    cleaned = SXXEYY_PATTERN.sub(" ", cleaned)
    cleaned = SEASON_WITH_OPTIONAL_EPISODE_PATTERN.sub(" ", cleaned)
    cleaned = SEASON_ONLY_PATTERN.sub(" ", cleaned)
    cleaned = EPISODE_ONLY_PATTERN.sub(" ", cleaned)
    cleaned = RESOLUTION_PATTERN.sub(" ", cleaned)
    cleaned = CODEC_PATTERN.sub(" ", cleaned)
    return cleaned
