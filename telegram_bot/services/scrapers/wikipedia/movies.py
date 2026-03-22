import asyncio
import re

import wikipedia
from bs4 import BeautifulSoup, Tag

from ....config import logger
from .cache import _WIKI_MOVIE_CACHE
from .fetch import _fetch_html_from_page
from .normalize import _normalize_for_comparison


async def fetch_movie_years_from_wikipedia(
    movie_title: str, _last_resort: bool = False
) -> tuple[list[int], str | None]:
    title = movie_title.strip()
    if not title:
        return [], None

    cache_key = title.lower()
    cached = _WIKI_MOVIE_CACHE.get(cache_key)
    if cached:
        return cached

    normalized_title_key = _normalize_for_comparison(title)
    year_film_pat = re.compile(r"\((19\d{2}|20\d{2})\s+film\)", re.IGNORECASE)
    generic_film_pat = re.compile(r"\((?:feature\s+)?film\)", re.IGNORECASE)
    disamb_pat = re.compile(r"\(disambiguation\)\Z", re.IGNORECASE)

    def _extract_years_from_text(text: str) -> list[int]:
        yrs = []
        for m in re.finditer(r"\b(19\d{2}|20\d{2})\b", text):
            try:
                yrs.append(int(m.group(1)))
            except Exception:
                continue
        return yrs

    def _year_from_title(page_title: str) -> int | None:
        m = re.search(r"\((19\d{2}|20\d{2})\s+film\)", page_title, re.IGNORECASE)
        return int(m.group(1)) if m else None

    def _normalized_search_title(page_title: str) -> str:
        base = re.sub(r"\s*\([^)]*\)\s*$", "", page_title).strip()
        return base

    def _candidate_matches_title(candidate_title: str) -> bool:
        base = _normalized_search_title(candidate_title)
        return _normalize_for_comparison(base) == normalized_title_key

    def _pick_film_candidate(
        search_results: list[str],
    ) -> tuple[str | None, str | None]:
        if not search_results:
            return None, None

        best: str | None = None
        disamb: str | None = None
        direct_exact_match: str | None = None
        title_cmp_normalized = normalized_title_key
        for candidate in search_results:
            if disamb is None and disamb_pat.search(candidate):
                disamb = candidate

            if not _candidate_matches_title(candidate):
                continue

            normalized_candidate = _normalized_search_title(candidate).casefold()
            stripped_candidate = candidate.strip().casefold()
            if (
                direct_exact_match is None
                and normalized_candidate == stripped_candidate
                and _normalize_for_comparison(normalized_candidate) == title_cmp_normalized
            ):
                direct_exact_match = candidate

            if year_film_pat.search(candidate):
                return candidate, disamb

            if best is None and generic_film_pat.search(candidate):
                best = candidate
            elif best is None and "film" in candidate.lower():
                best = candidate
        if best:
            return best, disamb
        return direct_exact_match, disamb

    years: list[int] = []
    corrected_for_search: str | None = None

    try:
        logger.info("[WIKI] Resolving movie years via Wikipedia for '%s'", title)
        search_results = await asyncio.to_thread(wikipedia.search, title, 50)
    except Exception as e:  # noqa: BLE001
        logger.error("[WIKI] Wikipedia search failed for '%s': %s", title, e)
        search_results = []

    equal_precision_years: set[int] = set()
    for cand in search_results or []:
        if not _candidate_matches_title(cand):
            continue
        m = year_film_pat.search(cand)
        if m:
            try:
                equal_precision_years.add(int(m.group(1)))
            except Exception:
                pass

    best_title, disamb_title = _pick_film_candidate(search_results)

    if best_title:
        normalized_best_title = _normalized_search_title(best_title)
        if (
            _normalize_for_comparison(normalized_best_title) == normalized_title_key
            and not corrected_for_search
        ):
            # Keep the best matching canonical text from Wikipedia search results,
            # even if page loading fails later.
            corrected_for_search = normalized_best_title
        try:
            page = await asyncio.to_thread(
                wikipedia.page, best_title, auto_suggest=False, redirect=True
            )
            assert page is not None

            normalized = _normalized_search_title(page.title)
            if normalized.lower() != title.lower():
                corrected_for_search = normalized

            if (y := _year_from_title(page.title)) is not None:
                years.append(y)
            else:
                try:
                    summary = await asyncio.to_thread(
                        wikipedia.summary, page.title, sentences=2, auto_suggest=False
                    )
                except Exception:
                    summary = ""
                m = re.search(r"\b(19\d{2}|20\d{2})\b[^.]{0,60}\bfilm\b", summary, re.IGNORECASE)
                if m:
                    years.append(int(m.group(1)))
                else:
                    if page:
                        html = await _fetch_html_from_page(page)
                        if html:
                            soup = BeautifulSoup(html, "html.parser")
                            infobox = soup.find("table", class_=re.compile(r"\binfobox\b"))
                            if isinstance(infobox, Tag):
                                for row in infobox.find_all("tr"):
                                    if not isinstance(row, Tag):
                                        continue
                                    th = row.find("th")
                                    if th and "release" in th.get_text(strip=True).lower():
                                        td = row.find("td")
                                        if td:
                                            cand_years = _extract_years_from_text(
                                                td.get_text(" ", strip=True)
                                            )
                                            for y in cand_years:
                                                if y not in years:
                                                    years.append(y)
                            if not years:
                                lead_p = soup.find("p")
                                if isinstance(lead_p, Tag):
                                    m2 = re.search(
                                        r"\b(19\d{2}|20\d{2})\b[^.]{0,60}\bfilm\b",
                                        lead_p.get_text(" ", strip=True),
                                        re.IGNORECASE,
                                    )
                                    if m2:
                                        years.append(int(m2.group(1)))
        except wikipedia.exceptions.DisambiguationError as d_err:
            for opt in getattr(d_err, "options", []) or []:
                m = re.search(r"\((19\d{2}|20\d{2})\s+film\)", opt, re.IGNORECASE)
                if m:
                    y = int(m.group(1))
                    if y not in years:
                        years.append(y)
            disamb_title = disamb_title or f"{title} (disambiguation)"
        except Exception as e:  # noqa: BLE001
            logger.debug("[WIKI] Error resolving film page for '%s': %s", title, e)

    if disamb_title:
        try:
            disamb_page = await asyncio.to_thread(
                wikipedia.page, disamb_title, auto_suggest=False, redirect=True
            )
            html = await _fetch_html_from_page(disamb_page)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                for a in soup.find_all("a", href=True):
                    if not isinstance(a, Tag):
                        continue
                    text = a.get_text(strip=True)
                    if not text:
                        continue
                    if not _candidate_matches_title(text):
                        continue
                    m = year_film_pat.search(text)
                    if m:
                        try:
                            equal_precision_years.add(int(m.group(1)))
                        except Exception:
                            pass
        except Exception as e:  # noqa: BLE001
            logger.debug(
                "[WIKI] Error parsing disambiguation for '%s' via '%s': %s",
                title,
                disamb_title,
                e,
            )

    if len(equal_precision_years) < 2:
        fallback_disamb = f"{title} (disambiguation)"
        try:
            disamb_page = await asyncio.to_thread(
                wikipedia.page, fallback_disamb, auto_suggest=False, redirect=True
            )
            html = await _fetch_html_from_page(disamb_page)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                for a in soup.find_all("a", href=True):
                    if not isinstance(a, Tag):
                        continue
                    text = a.get_text(strip=True)
                    if not text:
                        continue
                    if not _candidate_matches_title(text):
                        continue
                    m = year_film_pat.search(text)
                    if m:
                        try:
                            equal_precision_years.add(int(m.group(1)))
                        except Exception:
                            pass
        except Exception:
            pass

    if not years and not _last_resort and "(film)" not in title.lower():
        qualified = f"{title} (film)"
        logger.info(
            "[WIKI] No film years found for '%s'. Retrying with qualifier: '%s'",
            title,
            qualified,
        )
        yr2, corr2 = await fetch_movie_years_from_wikipedia(qualified, _last_resort=True)
        if corr2:
            corrected_for_search = corr2
        years = yr2

    preferred_years: list[int]
    if equal_precision_years:
        preferred_years = sorted(equal_precision_years)
    else:
        seen: set[int] = set()
        preferred_years = []
        for y in years:
            if y not in seen:
                seen.add(y)
                preferred_years.append(y)

    logger.info(
        "[WIKI] Movie years for '%s': %s (corrected: %s)",
        title,
        preferred_years,
        corrected_for_search,
    )
    _WIKI_MOVIE_CACHE[cache_key] = (preferred_years, corrected_for_search)
    return preferred_years, corrected_for_search
