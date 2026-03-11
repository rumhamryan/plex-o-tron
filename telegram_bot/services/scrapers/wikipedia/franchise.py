import asyncio
from typing import Any

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


def _compose_movie_key(
    normalized_title: str, year: int | None, release_iso: str | None, fallback_idx: int
) -> str:
    base = normalized_title or f"title{fallback_idx}"
    if release_iso:
        return f"{base}-{release_iso}"
    if year is not None:
        return f"{base}-{year}"
    return f"{base}-{fallback_idx}"


def _candidate_match_flags(candidate_title: str, normalized_query: str) -> tuple[bool, bool]:
    candidate_norm = _normalize_for_comparison(candidate_title)
    matches_query = bool(normalized_query) and normalized_query in candidate_norm
    lowered = candidate_title.casefold()
    has_keyword = any(keyword in lowered for keyword in _FRANCHISE_KEYWORDS)
    return matches_query, has_keyword


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
    for row_idx, row in enumerate(table.find_all("tr")):
        cells = row.find_all(["td", "th"])
        if len(cells) <= title_idx:
            continue
        raw_title = cells[title_idx].get_text(" ", strip=True)
        cleaned_title = _clean_movie_label(raw_title)
        if not cleaned_title or cleaned_title.casefold() in {"title", "film"}:
            continue
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
    return movies if len(movies) >= 2 else []


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
    soup = BeautifulSoup(html, "lxml")
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


async def fetch_movie_franchise_details_from_wikipedia(
    movie_title: str,
) -> tuple[str, list[dict[str, Any]]] | None:
    """Attempts to find a franchise page and enumerate its films."""
    search_title = movie_title.strip()
    if not search_title:
        return None

    cache_key = search_title.casefold()
    if cache_key in _WIKI_FRANCHISE_CACHE:
        return _WIKI_FRANCHISE_CACHE[cache_key]

    normalized_query = _normalize_for_comparison(search_title)
    search_variants = [
        f"{search_title} film series",
        f"{search_title} franchise",
        f"List of {search_title} films",
        search_title,
    ]
    attempted: set[str] = set()

    for term in search_variants:
        try:
            search_results = await asyncio.to_thread(wikipedia.search, term, 10)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[WIKI] Franchise search failed for '{term}': {exc}")
            search_results = []
        filtered: list[tuple[str, bool]] = []
        for candidate in search_results or []:
            if candidate in attempted:
                continue
            matches_query, has_keyword = _candidate_match_flags(candidate, normalized_query)
            if not matches_query:
                continue
            filtered.append((candidate, has_keyword))

        if not filtered:
            continue

        prioritized = [item for item in filtered if item[1]]
        fallbacks = [item for item in filtered if not item[1]]
        ordered_candidates = prioritized if prioritized else filtered
        if prioritized and fallbacks:
            ordered_candidates = prioritized + fallbacks

        for candidate, _ in ordered_candidates:
            if candidate in attempted:
                continue
            attempted.add(candidate)
            page = await _resolve_franchise_candidate(candidate)
            if not page:
                continue
            html = await _fetch_html_from_page(page)
            if not html:
                continue
            movies = _extract_movies_from_franchise_html(html)
            if not movies:
                continue
            resolved_name = _sanitize_wikipedia_title(page.title.strip())
            payload = (resolved_name, movies)
            _WIKI_FRANCHISE_CACHE[cache_key] = payload
            return payload

    return None
