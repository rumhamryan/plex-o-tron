import re
from datetime import datetime

_YEAR_PATTERN = re.compile(r"(18|19|20|21)\d{2}")
_MONTH_PATTERN = (
    "January|February|March|April|May|June|July|August|September|October|November|December|"
    "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
_RELEASE_DATE_PATTERN = re.compile(
    rf"((?:{_MONTH_PATTERN})\s+\d{{1,2}},\s+\d{{4}}|\d{{1,2}}\s+(?:{_MONTH_PATTERN})\s+\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}})",
    re.IGNORECASE,
)
_RELEASE_DATE_FORMATS = [
    "%B %d, %Y",
    "%b %d, %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%Y-%m-%d",
]


def _extract_year_from_text(text: str) -> int | None:
    match = _YEAR_PATTERN.search(text or "")
    if not match:
        return None
    try:
        return int(match.group(0))
    except (ValueError, TypeError):
        return None


def _extract_release_date_iso(text: str) -> str | None:
    cleaned = re.sub(r"\[[^\]]+\]", "", text or "")
    cleaned = cleaned.replace("\xa0", " ").replace("\u2013", "-").replace("\u2014", "-")
    match = _RELEASE_DATE_PATTERN.search(cleaned)
    if not match:
        return None
    candidate = match.group(0)
    candidate = re.sub(r"\([^)]*\)", "", candidate).strip()
    candidate = re.sub(r"\s+", " ", candidate).strip(",; ")
    if not candidate:
        return None
    normalized = candidate.replace("Sept ", "Sep ").replace("Sept.", "Sep.")
    for fmt in _RELEASE_DATE_FORMATS:
        try:
            parsed = datetime.strptime(normalized, fmt)
            return parsed.date().isoformat()
        except ValueError:
            continue
    return None
