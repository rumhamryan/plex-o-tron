import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypedDict

import wikipedia
from bs4 import BeautifulSoup, Tag

from ....config import logger
from .cache import _WIKI_FRANCHISE_CACHE
from .dates import _extract_release_date_iso, _extract_year_from_text
from .fetch import _fetch_html_from_page
from .normalize import (
    _EPISODE_HEADER_TOKENS,
    _TITLE_HEADER_TOKENS,
    _YEAR_HEADER_TOKENS,
    _clean_movie_label,
    _header_contains_one,
    _normalize_for_comparison,
    _sanitize_wikipedia_title,
)

_FRANCHISE_KEYWORDS = (
    "film series",
    "film franchise",
    "franchise",
    "cinematic universe",
    "film universe",
    "films",
)

_SEASON_ORDINAL_TOKENS = (
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "seventh",
    "eighth",
    "ninth",
    "tenth",
    "eleventh",
    "twelfth",
    "final",
    "last",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
)
_SEASON_RELEASE_PATTERN = re.compile(
    r"^(?:the\s+)?(?:complete\s+|entire\s+|full\s+)?"
    r"(?:(?:" + "|".join(_SEASON_ORDINAL_TOKENS) + r")|\d+|[ivxlcdm]+)\s+"
    r"(?:season|series)\b",
    re.IGNORECASE,
)
_SEASON_SUFFIX_PATTERN = re.compile(
    r"[:\-]\s*(?:the\s+)?(?:complete\s+|entire\s+|full\s+)?"
    r"(?:(?:" + "|".join(_SEASON_ORDINAL_TOKENS) + r")|\d+|[ivxlcdm]+)\s+"
    r"(?:season|series)\b",
    re.IGNORECASE,
)
_COMPLETE_SERIES_PATTERN = re.compile(
    r"^(?:the\s+)?(?:complete|entire|full)\s+series\b",
    re.IGNORECASE,
)
_FILM_INFOBOX_LABEL_KEYS = {"film", "films"}
_FILM_SERIES_SECTION_PATTERN = re.compile(r"\bfilm\s+series\b", re.IGNORECASE)
_TRAILING_YEAR_QUALIFIER_PATTERN = re.compile(
    r"\s*\((?:18|19|20|21)\d{2}[^)]*\)\s*$",
    re.IGNORECASE,
)
_SINGLE_FILM_QUALIFIER_PATTERN = re.compile(
    r"\(\s*(?:(?:18|19|20|21)\d{2}\s+film|film)\s*\)\s*$",
    re.IGNORECASE,
)
_NEGATIVE_CANDIDATE_KEYWORDS = ("soundtrack", "album", "score", "song", "discography")
_POSITIVE_CANDIDATE_KEYWORDS = ("franchise", "film series")
_TITLE_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_MAX_FRANCHISE_CANDIDATES = 6


class _FranchiseScoreSignals(TypedDict):
    positive: list[str]
    negative: list[str]


_FranchiseSourceKind = Literal["infobox", "film_series_section", "navbox_films", "generic"]


class _FranchiseExtractionResult(TypedDict):
    movies: list[dict[str, Any]]
    source_kind: _FranchiseSourceKind


class _FranchiseCandidateResult(TypedDict):
    page_title: str
    resolved_title: str
    movies: list[dict[str, Any]]
    source_kind: _FranchiseSourceKind
    score: float
    signals: _FranchiseScoreSignals


class _RankedFranchiseSearchCandidate(TypedDict):
    title: str
    strong_match: bool
    has_keyword: bool
    title_score: int


_SOURCE_KIND_SCORE_BONUS: dict[_FranchiseSourceKind, float] = {
    "infobox": 12.0,
    "film_series_section": 8.0,
    "navbox_films": 10.0,
    "generic": 0.0,
}
_SOURCE_KIND_RANK: dict[_FranchiseSourceKind, int] = {
    "infobox": 4,
    "film_series_section": 3,
    "navbox_films": 2,
    "generic": 1,
}
_MIN_FRANCHISE_CANDIDATE_SCORE = 15.0


def _compose_movie_key(
    normalized_title: str, year: int | None, release_iso: str | None, fallback_idx: int
) -> str:
    base = normalized_title or f"title{fallback_idx}"
    if release_iso:
        return f"{base}-{release_iso}"
    if year is not None:
        return f"{base}-{year}"
    return f"{base}-{fallback_idx}"


def _title_tokens(value: str) -> tuple[str, ...]:
    return tuple(_TITLE_TOKEN_PATTERN.findall((value or "").casefold()))


def _exact_franchise_title_variants(search_title: str) -> set[str]:
    base = search_title.strip()
    if not base:
        return set()
    return {
        _normalize_for_comparison(variant)
        for variant in (
            base,
            f"{base} film series",
            f"{base} franchise",
            f"{base} cinematic universe",
            f"{base} film universe",
            f"List of {base} films",
        )
    }


def _rank_franchise_search_candidates(
    search_title: str, candidates: list[str]
) -> list[_RankedFranchiseSearchCandidate]:
    query_norm = _normalize_for_comparison(search_title)
    query_tokens = _title_tokens(search_title)
    exact_variants = _exact_franchise_title_variants(search_title)
    if not query_norm or not query_tokens:
        return []

    ranked: list[_RankedFranchiseSearchCandidate] = []
    seen_titles: set[str] = set()
    for raw_candidate in candidates:
        candidate_title = raw_candidate.strip()
        if not candidate_title or candidate_title in seen_titles:
            continue

        candidate_norm = _normalize_for_comparison(candidate_title)
        candidate_tokens = _title_tokens(candidate_title)
        if not candidate_norm or not candidate_tokens:
            continue

        token_set = set(candidate_tokens)
        if (
            not all(token in token_set for token in query_tokens)
            and candidate_norm not in exact_variants
        ):
            continue

        lowered = candidate_title.casefold()
        has_keyword = any(keyword in lowered for keyword in _FRANCHISE_KEYWORDS)
        strong_match = candidate_norm in exact_variants
        has_negative_keyword = any(keyword in lowered for keyword in _NEGATIVE_CANDIDATE_KEYWORDS)
        single_film_candidate = bool(_SINGLE_FILM_QUALIFIER_PATTERN.search(candidate_title))

        if (has_negative_keyword or single_film_candidate) and not strong_match:
            continue

        title_score = 0
        if strong_match:
            title_score += 120
        elif candidate_tokens[: len(query_tokens)] == query_tokens:
            title_score += 70
        elif candidate_norm.startswith(query_norm):
            title_score += 50
        else:
            title_score += 30

        if has_keyword:
            title_score += 25
        if has_negative_keyword:
            title_score -= 60
        if single_film_candidate:
            title_score -= 40

        extra_tokens = max(0, len(candidate_tokens) - len(query_tokens) - 2)
        title_score -= extra_tokens * 5
        if title_score <= 0:
            continue

        seen_titles.add(candidate_title)
        ranked.append(
            {
                "title": candidate_title,
                "strong_match": strong_match,
                "has_keyword": has_keyword,
                "title_score": title_score,
            }
        )

    return sorted(
        ranked,
        key=lambda item: (
            item["strong_match"],
            item["title_score"],
            item["has_keyword"],
            -len(_title_tokens(item["title"])),
        ),
        reverse=True,
    )


def _looks_like_season_release(label: str) -> bool:
    cleaned = _clean_movie_label(label)
    if not cleaned:
        return False
    return bool(
        _SEASON_RELEASE_PATTERN.search(cleaned)
        or _SEASON_SUFFIX_PATTERN.search(cleaned)
        or _COMPLETE_SERIES_PATTERN.search(cleaned)
    )


async def _resolve_franchise_candidate(
    candidate: str,
) -> wikipedia.WikipediaPage | None:
    try:
        return await asyncio.to_thread(wikipedia.page, candidate, auto_suggest=False, redirect=True)
    except wikipedia.exceptions.DisambiguationError as err:
        for option in err.options:
            if any(keyword in option.casefold() for keyword in _FRANCHISE_KEYWORDS):
                try:
                    return await asyncio.to_thread(
                        wikipedia.page, option, auto_suggest=False, redirect=True
                    )
                except wikipedia.exceptions.PageError:
                    continue
                except Exception:
                    continue
    except wikipedia.exceptions.PageError:
        return None
    except Exception:
        return None
    return None


def _build_movie_entry(
    raw_title: str,
    release_text: str,
    fallback_idx: int,
) -> dict[str, Any] | None:
    cleaned_title = _clean_movie_label(raw_title)
    if not cleaned_title or cleaned_title.casefold() in {"title", "film"}:
        return None
    normalized_key = _normalize_for_comparison(cleaned_title)
    if not normalized_key:
        return None
    year_value = _extract_year_from_text(release_text)
    if year_value is None:
        year_value = _extract_year_from_text(cleaned_title)
    release_iso = _extract_release_date_iso(release_text)
    return {
        "title": cleaned_title,
        "year": year_value,
        "identifier": _compose_movie_key(normalized_key, year_value, release_iso, fallback_idx),
        "release_text": release_text,
        "release_date": release_iso,
    }


def _infobox_label_keys(soup: BeautifulSoup) -> set[str]:
    infobox = soup.find("table", class_=re.compile(r"\binfobox\b"))
    if not isinstance(infobox, Tag):
        return set()

    labels: set[str] = set()
    for header in infobox.find_all("th"):
        if not isinstance(header, Tag):
            continue
        label_key = _normalize_for_comparison(header.get_text(" ", strip=True))
        if label_key:
            labels.add(label_key)
    return labels


def _score_keyword_matches(
    *,
    score: float,
    signals: _FranchiseScoreSignals,
    text: str,
    keywords: tuple[str, ...],
    prefix: str,
    weight: float,
    bucket: Literal["positive", "negative"],
    seen: set[str],
) -> float:
    lowered = text.casefold()
    for keyword in keywords:
        if keyword not in lowered:
            continue
        signal = f"{prefix}:{keyword}"
        if signal in seen:
            continue
        score += weight
        signals[bucket].append(signal)
        seen.add(signal)
    return score


def _score_franchise_candidate(
    *,
    candidate_title: str,
    resolved_title: str,
    soup: BeautifulSoup,
    movies: list[dict[str, Any]],
    source_kind: _FranchiseSourceKind,
) -> dict[str, Any]:
    score = 0.0
    signals: _FranchiseScoreSignals = {
        "positive": [],
        "negative": [],
    }
    seen_positive: set[str] = set()
    seen_negative: set[str] = set()

    title_text = " ".join(
        part.strip() for part in (candidate_title, resolved_title) if part
    ).casefold()
    heading_text = " ".join(
        heading.get_text(" ", strip=True) for heading in soup.find_all(["h1", "h2", "h3", "h4"])
    )
    lead_text = " ".join(
        paragraph.get_text(" ", strip=True) for paragraph in soup.find_all("p")[:3]
    )
    infobox_text = " ".join(sorted(_infobox_label_keys(soup)))
    key_content_text = " ".join(part for part in (heading_text, lead_text, infobox_text) if part)

    score = _score_keyword_matches(
        score=score,
        signals=signals,
        text=title_text,
        keywords=_NEGATIVE_CANDIDATE_KEYWORDS,
        prefix="title",
        weight=-40.0,
        bucket="negative",
        seen=seen_negative,
    )
    score = _score_keyword_matches(
        score=score,
        signals=signals,
        text=key_content_text,
        keywords=_NEGATIVE_CANDIDATE_KEYWORDS,
        prefix="content",
        weight=-10.0,
        bucket="negative",
        seen=seen_negative,
    )
    score = _score_keyword_matches(
        score=score,
        signals=signals,
        text=title_text,
        keywords=_POSITIVE_CANDIDATE_KEYWORDS,
        prefix="title",
        weight=18.0,
        bucket="positive",
        seen=seen_positive,
    )
    score = _score_keyword_matches(
        score=score,
        signals=signals,
        text=key_content_text,
        keywords=_POSITIVE_CANDIDATE_KEYWORDS,
        prefix="content",
        weight=8.0,
        bucket="positive",
        seen=seen_positive,
    )

    source_bonus = _SOURCE_KIND_SCORE_BONUS[source_kind]
    if source_bonus:
        score += source_bonus
        signals["positive"].append(f"source:{source_kind}")

    if _FILM_INFOBOX_LABEL_KEYS & _infobox_label_keys(soup):
        score += 20.0
        signals["positive"].append("infobox:films_field")

    movie_count = len(movies)
    if movie_count:
        score += min(movie_count * 2.0, 10.0)
        signals["positive"].append(f"movies:count={movie_count}")

    dated_count = sum(
        1 for movie in movies if movie.get("year") is not None or movie.get("release_date")
    )
    if dated_count:
        score += min(dated_count * 4.0, 16.0)
        signals["positive"].append(f"movies:dated={dated_count}")

    return {
        "score": score,
        "signals": signals,
    }


def _extract_movies_from_generic_structures(soup: BeautifulSoup) -> list[dict[str, Any]]:
    for table in soup.find_all("table", class_="wikitable"):
        movies = _extract_movies_from_table(table)
        if len(movies) >= 2:
            return movies
    movies = _extract_movies_from_lists(soup)
    if movies:
        return movies
    condensed: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        movies = _extract_movies_from_table(table)
        if movies:
            condensed.extend(movies)
            break
    return condensed if len(condensed) >= 2 else []


def _extract_movies_from_navbox_films(soup: BeautifulSoup) -> list[dict[str, Any]]:
    for navbox in soup.find_all("table", class_=re.compile(r"\bnavbox\b")):
        if not isinstance(navbox, Tag):
            continue
        for row in navbox.find_all("tr"):
            if not isinstance(row, Tag):
                continue
            header = row.find("th")
            value = row.find("td")
            if not isinstance(header, Tag) or not isinstance(value, Tag):
                continue
            label_key = _normalize_for_comparison(header.get_text(" ", strip=True))
            if label_key != "films":
                continue

            movies: list[dict[str, Any]] = []
            seen: set[str] = set()
            candidates = list(value.find_all("li", recursive=False))
            if not candidates:
                candidates = list(value.find_all("li"))
            if not candidates:
                candidates = [value]

            for idx, candidate in enumerate(candidates):
                if not isinstance(candidate, Tag):
                    continue
                raw_title = candidate.get_text(" ", strip=True)
                movie = _build_movie_entry(raw_title, raw_title, idx)
                if not movie or _looks_like_season_release(movie["title"]):
                    continue
                if movie["identifier"] in seen:
                    continue
                seen.add(movie["identifier"])
                movies.append(movie)

            return movies if len(movies) >= 2 else []

    return []


def _extract_franchise_candidate_result(html: str) -> _FranchiseExtractionResult | None:
    soup = BeautifulSoup(html, "html.parser")
    extractors: tuple[tuple[_FranchiseSourceKind, Any], ...] = (
        ("infobox", _extract_movies_from_infobox),
        ("film_series_section", _extract_movies_from_film_series_section),
        ("navbox_films", _extract_movies_from_navbox_films),
        ("generic", _extract_movies_from_generic_structures),
    )
    for source_kind, extractor in extractors:
        movies = extractor(soup)
        if movies:
            return {
                "movies": movies,
                "source_kind": source_kind,
            }
    return None


def _extract_movies_from_infobox(soup: BeautifulSoup) -> list[dict[str, Any]]:
    infobox = soup.find("table", class_=re.compile(r"\binfobox\b"))
    if not isinstance(infobox, Tag):
        return []

    for row in infobox.find_all("tr"):
        if not isinstance(row, Tag):
            continue
        header = row.find("th")
        value = row.find("td")
        if not isinstance(header, Tag) or not isinstance(value, Tag):
            continue

        label_key = _normalize_for_comparison(header.get_text(" ", strip=True))
        if label_key not in _FILM_INFOBOX_LABEL_KEYS:
            continue

        movies: list[dict[str, Any]] = []
        seen: set[str] = set()
        list_items = list(value.find_all("li"))
        candidates: list[Tag] = list_items or list(value.find_all(["i", "em"]))
        if not candidates:
            candidates = [value]

        for idx, candidate in enumerate(candidates):
            if not isinstance(candidate, Tag):
                continue
            label_source = candidate.find(["i", "em"])
            raw_title = (
                label_source.get_text(" ", strip=True)
                if isinstance(label_source, Tag)
                else candidate.get_text(" ", strip=True)
            )
            release_text = candidate.get_text(" ", strip=True)
            movie = _build_movie_entry(raw_title, release_text, idx)
            if not movie or _looks_like_season_release(movie["title"]):
                continue
            if movie["identifier"] in seen:
                continue
            seen.add(movie["identifier"])
            movies.append(movie)

        return movies if len(movies) >= 2 else []

    return []


def _extract_direct_heading(node: Tag) -> Tag | None:
    if node.name in {"h2", "h3", "h4"}:
        return node
    direct_heading = node.find(["h2", "h3", "h4"], recursive=False)
    return direct_heading if isinstance(direct_heading, Tag) else None


def _heading_container(heading: Tag) -> Tag:
    parent = heading.parent
    parent_classes = parent.get("class", []) if isinstance(parent, Tag) else []
    if isinstance(parent, Tag) and "mw-heading" in parent_classes:
        return parent
    return heading


def _extract_movies_from_film_series_section(soup: BeautifulSoup) -> list[dict[str, Any]]:
    for heading in soup.find_all(["h2", "h3", "h4"]):
        heading_text = heading.get_text(" ", strip=True)
        if not _FILM_SERIES_SECTION_PATTERN.search(heading_text):
            continue

        section_level = int(heading.name[1])
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        sibling = _heading_container(heading).find_next_sibling()
        while sibling:
            if isinstance(sibling, Tag):
                direct_heading = _extract_direct_heading(sibling)
                if isinstance(direct_heading, Tag):
                    level = int(direct_heading.name[1])
                    if level <= section_level:
                        break
                    raw_heading_text = direct_heading.get_text(" ", strip=True)
                    if _extract_year_from_text(raw_heading_text) is not None:
                        italic = direct_heading.find(["i", "em"])
                        raw_title = (
                            italic.get_text(" ", strip=True)
                            if isinstance(italic, Tag)
                            else _TRAILING_YEAR_QUALIFIER_PATTERN.sub("", raw_heading_text).strip()
                        )
                        movie = _build_movie_entry(raw_title, raw_heading_text, len(entries))
                        if movie and movie["identifier"] not in seen:
                            seen.add(movie["identifier"])
                            entries.append(movie)
            sibling = sibling.find_next_sibling()

        if len(entries) >= 2:
            return entries

    return []


def _extract_movies_from_table(table: Tag) -> list[dict[str, Any]]:
    headers = [header.get_text(" ", strip=True).casefold() for header in table.find_all("th")]
    if not headers:
        return []

    if any(token in header for header in headers for token in _EPISODE_HEADER_TOKENS):
        return []

    title_idx: int | None = None
    for idx, header in enumerate(headers):
        if _header_contains_one(header, _TITLE_HEADER_TOKENS):
            title_idx = idx
            break
    if title_idx is None:
        return []

    year_idx: int | None = None
    for idx, header in enumerate(headers):
        if _header_contains_one(header, _YEAR_HEADER_TOKENS):
            year_idx = idx
            break

    if year_idx is None:
        return []

    movies: list[dict[str, Any]] = []
    seen: set[str] = set()
    season_like_titles = 0
    for row_idx, row in enumerate(table.find_all("tr")):
        cells = row.find_all(["td", "th"])
        if len(cells) <= title_idx:
            continue
        raw_title = cells[title_idx].get_text(" ", strip=True)
        cleaned_title = _clean_movie_label(raw_title)
        if not cleaned_title or cleaned_title.casefold() in {"title", "film"}:
            continue
        if _looks_like_season_release(cleaned_title):
            season_like_titles += 1
        normalized_key = _normalize_for_comparison(cleaned_title)
        if not normalized_key:
            continue
        year_value = None
        release_text = ""
        if year_idx is not None and len(cells) > year_idx:
            raw_year = cells[year_idx].get_text(" ", strip=True)
            release_text = raw_year
            year_value = _extract_year_from_text(raw_year)
        if year_value is None:
            year_value = _extract_year_from_text(cleaned_title)
        release_iso = _extract_release_date_iso(release_text)
        dedup_key = _compose_movie_key(normalized_key, year_value, release_iso, row_idx)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        movies.append(
            {
                "title": cleaned_title,
                "year": year_value,
                "identifier": dedup_key,
                "release_text": release_text,
                "release_date": release_iso,
            }
        )
    if len(movies) < 2:
        return []
    if season_like_titles > len(movies) // 2:
        return []
    return movies


def _extract_movies_from_lists(soup: BeautifulSoup) -> list[dict[str, Any]]:
    for heading in soup.find_all(["h2", "h3", "h4"]):
        heading_text = heading.get_text(" ", strip=True).casefold()
        if not any(token in heading_text for token in _TITLE_HEADER_TOKENS):
            continue
        sibling = heading.find_next_sibling()
        while sibling:
            if isinstance(sibling, Tag) and sibling.name == "ul":
                entries: list[dict[str, Any]] = []
                seen: set[str] = set()
                for idx, li in enumerate(sibling.find_all("li", recursive=False)):
                    label = _clean_movie_label(li.get_text(" ", strip=True))
                    if not label:
                        continue
                    if "episode" in label.casefold():
                        entries = []
                        break
                    normalized_key = _normalize_for_comparison(label)
                    if not normalized_key:
                        continue
                    year_value = _extract_year_from_text(label)
                    if year_value is None and not any(
                        token in label.casefold() for token in _TITLE_HEADER_TOKENS
                    ):
                        continue
                    release_iso = _extract_release_date_iso(label)
                    dedup_key = _compose_movie_key(normalized_key, year_value, release_iso, idx)
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)
                    entries.append(
                        {
                            "title": label,
                            "year": year_value,
                            "identifier": dedup_key,
                            "release_text": label,
                            "release_date": release_iso,
                        }
                    )
                if len(entries) >= 2:
                    return entries
            if isinstance(sibling, Tag) and sibling.name in {"h2", "h3", "h4"}:
                break
            sibling = sibling.find_next_sibling()
    return []


def _extract_movies_from_franchise_html(html: str) -> list[dict[str, Any]]:
    extraction = _extract_franchise_candidate_result(html)
    return extraction["movies"] if extraction else []


def _dated_movie_count(movies: list[dict[str, Any]]) -> int:
    return sum(1 for movie in movies if movie.get("year") is not None or movie.get("release_date"))


def _candidate_sort_key(candidate: _FranchiseCandidateResult) -> tuple[float, int, int, int]:
    movies = candidate["movies"]
    return (
        candidate["score"],
        _SOURCE_KIND_RANK[candidate["source_kind"]],
        len(movies),
        _dated_movie_count(movies),
    )


def _select_best_franchise_candidate(
    candidates: list[_FranchiseCandidateResult],
) -> _FranchiseCandidateResult | None:
    viable_candidates = [
        candidate
        for candidate in candidates
        if candidate["score"] >= _MIN_FRANCHISE_CANDIDATE_SCORE
    ]
    if not viable_candidates:
        return None
    return max(viable_candidates, key=_candidate_sort_key)


async def fetch_movie_franchise_details_from_wikipedia(
    movie_title: str,
    *,
    progress_callback: Callable[[str, str | None], Awaitable[None]] | None = None,
) -> tuple[str, list[dict[str, Any]]] | None:
    """Attempts to find a franchise page and enumerate its films."""
    search_title = movie_title.strip()
    if not search_title:
        return None

    cache_key = search_title.casefold()
    if cache_key in _WIKI_FRANCHISE_CACHE:
        return _WIKI_FRANCHISE_CACHE[cache_key]

    search_variants = [
        f"{search_title} film series",
        f"{search_title} franchise",
        f"List of {search_title} films",
        search_title,
    ]
    raw_candidates: list[str] = []
    seen_candidates: set[str] = set()

    if progress_callback is not None:
        await progress_callback("review", None)

    for term in search_variants:
        try:
            search_results = await asyncio.to_thread(wikipedia.search, term, 10)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[WIKI] Franchise search failed for '{term}': {exc}")
            search_results = []
        for candidate in search_results or []:
            if candidate in seen_candidates:
                continue
            seen_candidates.add(candidate)
            raw_candidates.append(candidate)

    ranked_candidates = _rank_franchise_search_candidates(search_title, raw_candidates)
    strong_candidates = [candidate for candidate in ranked_candidates if candidate["strong_match"]]
    if strong_candidates:
        ranked_candidates = strong_candidates
    gathered_candidates = [
        candidate["title"] for candidate in ranked_candidates[:_MAX_FRANCHISE_CANDIDATES]
    ]

    if progress_callback is not None and gathered_candidates:
        await progress_callback("compare", None)

    evaluated_candidates: list[_FranchiseCandidateResult] = []
    opened_candidate_page = False
    for candidate in gathered_candidates:
        page = await _resolve_franchise_candidate(candidate)
        if not page:
            continue
        if progress_callback is not None and not opened_candidate_page:
            await progress_callback("inspect", None)
            opened_candidate_page = True
        html = await _fetch_html_from_page(page)
        if not html:
            continue
        extraction = _extract_franchise_candidate_result(html)
        if not extraction:
            continue
        if progress_callback is not None:
            resolved_name = _sanitize_wikipedia_title(page.title.strip())
            await progress_callback("score", resolved_name)

        soup = BeautifulSoup(html, "html.parser")
        resolved_name = _sanitize_wikipedia_title(page.title.strip())
        scoring = _score_franchise_candidate(
            candidate_title=candidate,
            resolved_title=resolved_name,
            soup=soup,
            movies=extraction["movies"],
            source_kind=extraction["source_kind"],
        )
        evaluated_candidates.append(
            {
                "page_title": candidate,
                "resolved_title": resolved_name,
                "movies": extraction["movies"],
                "source_kind": extraction["source_kind"],
                "score": scoring["score"],
                "signals": scoring["signals"],
            }
        )

    best_candidate = _select_best_franchise_candidate(evaluated_candidates)
    if best_candidate is None:
        return None

    payload = (best_candidate["resolved_title"], best_candidate["movies"])
    _WIKI_FRANCHISE_CACHE[cache_key] = payload
    return payload
