import re


def _normalize_for_comparison(value: str) -> str:
    return re.sub(r"[\W_]+", "", value).casefold()


_WIKIPEDIA_TRAILING_QUALIFIER_PATTERN = re.compile(
    r"\s*\((?:[^)]*\b(?:mini[-\s]?series|(?:tv|television)\s+series)[^)]*)\)\s*$",
    re.IGNORECASE,
)


def _sanitize_wikipedia_title(title: str) -> str:
    if not title:
        return title
    cleaned = title
    while True:
        new_cleaned = _WIKIPEDIA_TRAILING_QUALIFIER_PATTERN.sub("", cleaned).strip()
        if new_cleaned == cleaned:
            break
        cleaned = new_cleaned
    return cleaned or title


_TITLE_HEADER_TOKENS = ("title", "film", "movie", "name")
_YEAR_HEADER_TOKENS = ("year", "release", "released", "date", "premiere", "debut")
_EPISODE_HEADER_TOKENS = (
    "episode",
    "episodes",
    "no. in season",
    "no.",
    "aired",
    "season",
)


def _clean_movie_label(value: str) -> str:
    cleaned = re.sub(r"\[\d+\]", "", value or "")
    cleaned = cleaned.replace("\u2013", "-").replace("\u2014", "-")
    cleaned = cleaned.replace("\u2019", "'")
    cleaned = cleaned.replace("\u201c", '"').replace("\u201d", '"')
    cleaned = cleaned.strip().strip('"')
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _normalized_header_text(value: str) -> str:
    lowered = (value or "").casefold()
    return re.sub(r"[^a-z0-9\s/]", " ", lowered)


def _header_contains_one(header: str, tokens: tuple[str, ...]) -> bool:
    normalized = _normalized_header_text(header)
    return any(token in normalized for token in tokens)
