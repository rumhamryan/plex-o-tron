import asyncio
import re
import warnings

import wikipedia
from bs4 import BeautifulSoup, Tag, GuessedAtParserWarning

from ...config import logger
from ...utils import extract_first_int
from .utils import _get_page_html

# --- Wikipedia caching (per-process) ---
# Caches per-season episode titles and corrected show title to avoid repeated
# network requests and HTML parsing during season workflows.
_WIKI_TITLES_CACHE: dict[tuple[str, int], tuple[dict[int, str], str | None]] = {}
_WIKI_SOUP_CACHE: dict[str, BeautifulSoup] = {}
_WIKI_MOVIE_CACHE: dict[str, tuple[list[int], str | None]] = {}


def _normalize_for_comparison(value: str) -> str:
    """Collapse punctuation/spacing so title comparisons tolerate stylistic variants."""
    return re.sub(r"[\W_]+", "", value).casefold()


_WIKIPEDIA_TRAILING_QUALIFIER_PATTERN = re.compile(
    r"\s*\((?:[^)]*\b(?:mini[-\s]?series|(?:tv|television)\s+series)[^)]*)\)\s*$",
    re.IGNORECASE,
)


def _sanitize_wikipedia_title(title: str) -> str:
    """Normalize Wikipedia titles by trimming trailing series qualifiers."""
    if not title:
        return title
    cleaned = title
    while True:
        new_cleaned = _WIKIPEDIA_TRAILING_QUALIFIER_PATTERN.sub("", cleaned).strip()
        if new_cleaned == cleaned:
            break
        cleaned = new_cleaned
    return cleaned or title


# Suppress noisy BeautifulSoup parser guess warnings originating from wikipedia lib only
warnings.filterwarnings(
    "ignore", category=GuessedAtParserWarning, module=r"^wikipedia\.wikipedia$"
)


# --- Wikipedia Scraping ---


async def fetch_episode_title_from_wikipedia(
    show_title: str, season: int, episode: int, _last_resort: bool = False
) -> tuple[str | None, str | None]:
    """
    Fetches an episode title from Wikipedia, trying a dedicated episode list page first,
    then falling back to the main show page.

    Returns:
        A tuple of (episode_title, corrected_show_title). The corrected title is
        returned if Wikipedia redirects the initial search.
    """
    corrected_show_title: str | None = None
    normalized_input = show_title.strip()
    cache_key = (normalized_input.lower(), season)

    # Fast path: return from cache if available
    cached = _WIKI_TITLES_CACHE.get(cache_key)
    if cached:
        titles_map, corrected = cached
        return titles_map.get(episode), corrected
    canonical_title = normalized_input
    main_page_url: str | None = None

    # --- Step 1: Find the main show page to get the canonical, corrected title ---
    try:
        logger.info(
            f"[WIKI] Step 1: Finding main page to correct title for '{show_title}'"
        )
        search_results = await asyncio.to_thread(wikipedia.search, show_title)
        if not search_results:
            logger.error(
                f"[WIKI] No Wikipedia page found for '{show_title}'. Aborting."
            )
            return None, None

        main_page_title = search_results[0]
        main_page = await asyncio.to_thread(
            wikipedia.page, main_page_title, auto_suggest=False, redirect=True
        )
        main_page_url = main_page.url

        resolved_title = main_page.title.strip()
        sanitized_title = _sanitize_wikipedia_title(resolved_title)
        canonical_title = sanitized_title

        if resolved_title != sanitized_title:
            logger.info(
                f"[WIKI] Normalized resolved title '{resolved_title}' -> '{sanitized_title}'"
            )

        if canonical_title.casefold() != normalized_input.casefold():
            corrected_show_title = canonical_title
            logger.info(
                f"[WIKI] Title was corrected: '{show_title}' -> '{canonical_title}'"
            )
        else:
            logger.info("[WIKI] Successfully found main show page with original title.")

    except wikipedia.exceptions.PageError:
        logger.error(
            f"[WIKI] Could not find any Wikipedia page for '{show_title}'. Aborting."
        )
        # Last-resort retry for TV titles: append qualifier once
        if not _last_resort:
            qualified = f"{show_title} (TV series)"
            logger.info(
                f"[WIKI] Retrying with TV qualifier as last resort: '{qualified}'"
            )
            return await fetch_episode_title_from_wikipedia(
                qualified, season, episode, _last_resort=True
            )
        return None, None
    except Exception as e:
        logger.error(
            f"[WIKI] An unexpected error occurred during main page search: {e}"
        )
        if not _last_resort:
            qualified = f"{show_title} (TV series)"
            logger.info(
                f"[WIKI] Retrying with TV qualifier as last resort: '{qualified}'"
            )
            return await fetch_episode_title_from_wikipedia(
                qualified, season, episode, _last_resort=True
            )
        return None, None

    # --- Step 2: Use the canonical title to find the dedicated episode page ---
    html_to_scrape: str | None = None
    try:
        direct_query = f"List of {canonical_title} episodes"
        logger.info(
            f"[WIKI] Step 2: Attempting to find dedicated episode page: '{direct_query}'"
        )
        list_page = await asyncio.to_thread(
            wikipedia.page, direct_query, auto_suggest=False, redirect=True
        )
        html_to_scrape = await _get_page_html(list_page.url)
        logger.info("[WIKI] Found and will use dedicated episode page.")

    except wikipedia.exceptions.PageError:
        logger.warning(
            "[WIKI] No dedicated episode page found. Falling back to main show page HTML."
        )
        if main_page_url:
            html_to_scrape = await _get_page_html(main_page_url)
    except Exception as e:
        logger.error(
            f"[WIKI] Unexpected error fetching list page, falling back to main page HTML: {e}"
        )
        if main_page_url:
            html_to_scrape = await _get_page_html(main_page_url)

    if not html_to_scrape:
        logger.error("[WIKI] All page search attempts failed.")
        # As a last resort, retry with a TV-series qualified title if not already tried
        if not _last_resort:
            qualified = f"{show_title} (TV series)"
            logger.info(
                f"[WIKI] Retrying with TV qualifier as last resort: '{qualified}'"
            )
            return await fetch_episode_title_from_wikipedia(
                qualified, season, episode, _last_resort=True
            )
        return None, None

    # --- Step 3: Parse the HTML to find the episode title ---
    soup = BeautifulSoup(html_to_scrape, "lxml")
    # Populate cache for the whole season while we’re here
    titles_map = await _extract_titles_for_season(soup, season)
    if titles_map:
        _WIKI_TITLES_CACHE[cache_key] = (titles_map, corrected_show_title)
    episode_title = titles_map.get(episode) if titles_map else None

    if not episode_title:
        logger.warning(
            f"[WIKI] All parsing strategies failed to find S{season:02d}E{episode:02d}."
        )
        # Final attempt with qualified TV title
        if not _last_resort:
            qualified = f"{show_title} (TV series)"
            logger.info(
                f"[WIKI] Retrying with TV qualifier as last resort: '{qualified}'"
            )
            return await fetch_episode_title_from_wikipedia(
                qualified, season, episode, _last_resort=True
            )

    return episode_title, corrected_show_title


async def fetch_movie_years_from_wikipedia(
    movie_title: str, _last_resort: bool = False
) -> tuple[list[int], str | None]:
    """
    Resolve a movie's likely release year(s) from Wikipedia and optionally return a
    corrected title suitable for searching.

    Strategy (adapted from TV show Wikipedia flow, but film-focused):
    1) Wikipedia search for the title and try to resolve a canonical film page
       ("Title (YEAR film)" preferred, then "Title (film)").
    2) If a film page is found, extract a release year from the page title,
       summary, or infobox.
    3) If resolution fails, fall back to the disambiguation page to collect film
       entries like "Title (YEAR film)".
    4) As a last resort, retry once by appending "(film)" to the query.

    Returns a tuple: (years, corrected_title_for_search).
    The corrected title is normalized for searching (i.e., parentheses are
    stripped so "Spider-Man" may be returned instead of "Spider-Man (film)"),
    and is only provided if it differs from the input after normalization.
    """

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
        # Remove any trailing parenthetical qualifier like "(film)" or "(1979 film)".
        base = re.sub(r"\s*\([^)]*\)\s*$", "", page_title).strip()
        return base

    def _candidate_matches_title(candidate_title: str) -> bool:
        base = _normalized_search_title(candidate_title)
        return _normalize_for_comparison(base) == normalized_title_key

    def _pick_film_candidate(
        search_results: list[str],
    ) -> tuple[str | None, str | None]:
        # Returns (best_title, disambiguation_title)
        if not search_results:
            return None, None

        best: str | None = None
        disamb: str | None = None
        for candidate in search_results:
            if disamb is None and disamb_pat.search(candidate):
                disamb = candidate

            if not _candidate_matches_title(candidate):
                continue

            if year_film_pat.search(candidate):
                return candidate, disamb

            if best is None and generic_film_pat.search(candidate):
                best = candidate
            elif best is None and "film" in candidate.lower():
                best = candidate
        return best, disamb

    years: list[int] = []
    corrected_for_search: str | None = None

    try:
        logger.info("[WIKI] Resolving movie years via Wikipedia for '%s'", title)
        # Use a higher result cap to capture multiple film variants (e.g., 1984, 2021)
        search_results = await asyncio.to_thread(wikipedia.search, title, 50)
    except Exception as e:  # noqa: BLE001
        logger.error("[WIKI] Wikipedia search failed for '%s': %s", title, e)
        search_results = []

    # Collect all equal-precision film years directly from search results first
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

    # Try to resolve the best film candidate page first
    page_url: str | None = None
    if best_title:
        try:
            page = await asyncio.to_thread(
                wikipedia.page, best_title, auto_suggest=False, redirect=True
            )
            page_url = getattr(page, "url", None)

            # Only expose a corrected base title if it materially differs for searching
            normalized = _normalized_search_title(page.title)
            if normalized.lower() != title.lower():
                corrected_for_search = normalized

            # Prefer year encoded in the title e.g., "Title (1979 film)"
            if (y := _year_from_title(page.title)) is not None:
                years.append(y)
            else:
                # Fallback 1: try summary heuristic "is a 1979 ... film"
                try:
                    summary = await asyncio.to_thread(
                        wikipedia.summary, page.title, sentences=2, auto_suggest=False
                    )
                except Exception:
                    summary = ""
                m = re.search(
                    r"\b(19\d{2}|20\d{2})\b[^.]{0,60}\bfilm\b", summary, re.IGNORECASE
                )
                if m:
                    years.append(int(m.group(1)))
                else:
                    # Fallback 2: fetch HTML and parse infobox release dates
                    if page_url:
                        html = await _get_page_html(page_url)
                        if html:
                            soup = BeautifulSoup(html, "lxml")
                            infobox = soup.find(
                                "table", class_=re.compile(r"\binfobox\b")
                            )
                            if isinstance(infobox, Tag):
                                for row in infobox.find_all("tr"):
                                    if not isinstance(row, Tag):
                                        continue
                                    th = row.find("th")
                                    if (
                                        th
                                        and "release" in th.get_text(strip=True).lower()
                                    ):
                                        td = row.find("td")
                                        if td:
                                            cand_years = _extract_years_from_text(
                                                td.get_text(" ", strip=True)
                                            )
                                            for y in cand_years:
                                                if y not in years:
                                                    years.append(y)
                            # As a last HTML heuristic, try the first paragraph
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
            # Collect film years directly from disambiguation options
            for opt in getattr(d_err, "options", []) or []:
                m = re.search(r"\((19\d{2}|20\d{2})\s+film\)", opt, re.IGNORECASE)
                if m:
                    y = int(m.group(1))
                    if y not in years:
                        years.append(y)
            # If no years, fall through to explicit disambiguation page parsing below
            disamb_title = disamb_title or f"{title} (disambiguation)"
        except Exception as e:  # noqa: BLE001
            logger.debug("[WIKI] Error resolving film page for '%s': %s", title, e)

    # If we still have no years OR we want to supplement equal-precision set, parse disambiguation
    if disamb_title:
        try:
            disamb_page = await asyncio.to_thread(
                wikipedia.page, disamb_title, auto_suggest=False, redirect=True
            )
            html = await _get_page_html(getattr(disamb_page, "url", ""))
            if html:
                soup = BeautifulSoup(html, "lxml")
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

    # If still underrepresented, try an explicit '(disambiguation)' page fallback
    if len(equal_precision_years) < 2:
        fallback_disamb = f"{title} (disambiguation)"
        try:
            disamb_page = await asyncio.to_thread(
                wikipedia.page, fallback_disamb, auto_suggest=False, redirect=True
            )
            html = await _get_page_html(getattr(disamb_page, "url", ""))
            if html:
                soup = BeautifulSoup(html, "lxml")
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
            # Silent fallback; not all titles have a dedicated disambiguation page
            pass

    # Last resort: try appending (film) once if nothing found
    if not years and not _last_resort and "(film)" not in title.lower():
        qualified = f"{title} (film)"
        logger.info(
            "[WIKI] No film years found for '%s'. Retrying with qualifier: '%s'",
            title,
            qualified,
        )
        yr2, corr2 = await fetch_movie_years_from_wikipedia(
            qualified, _last_resort=True
        )
        # Normalize corrected title for search if available; otherwise, use the qualified base
        if corr2:
            corrected_for_search = corr2
        elif corrected_for_search is None:
            # Strip qualifier when handing back a corrected base for searching
            corrected_for_search = _normalized_search_title(qualified)
        years = yr2

    # Prefer equal-precision years if any were found; otherwise, fall back to heuristics.
    preferred_years: list[int]
    if equal_precision_years:
        preferred_years = sorted(equal_precision_years)
    else:
        # De-duplicate heuristic-derived years while preserving order
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


async def fetch_episode_titles_for_season(
    show_title: str, season: int, _last_resort: bool = False
) -> tuple[dict[int, str], str | None]:
    """Fetch all episode titles for a given season in one pass.

    Returns a mapping of episode number to title and an optional corrected
    show title if Wikipedia redirects.
    Results are cached per (show_title, season).
    """
    cache_key = (show_title.strip().lower(), season)
    cached = _WIKI_TITLES_CACHE.get(cache_key)
    if cached:
        logger.info(
            f"[WIKI] Cache hit for episode titles: '{show_title}' S{season:02d}."
        )
        return cached[0], cached[1]

    corrected_show_title: str | None = None
    canonical_title = show_title
    main_page_url: str | None = None

    # Step 1: Resolve main show page to get canonical title
    try:
        logger.info(
            f"[WIKI] Resolving main show page for '{show_title}' to determine canonical title."
        )
        search_results = await asyncio.to_thread(wikipedia.search, show_title)
        if not search_results:
            logger.warning(f"[WIKI] No Wikipedia search results for '{show_title}'.")
            if not _last_resort:
                qualified = f"{show_title} (TV series)"
                logger.info(
                    f"[WIKI] Retrying with TV qualifier as last resort: '{qualified}'"
                )
                return await fetch_episode_titles_for_season(
                    qualified, season, _last_resort=True
                )
            return {}, None
        main_page_title = search_results[0]
        main_page = await asyncio.to_thread(
            wikipedia.page, main_page_title, auto_suggest=False, redirect=True
        )
        main_page_url = main_page.url
        if main_page.title != show_title:
            corrected_show_title = main_page.title
            canonical_title = main_page.title
            logger.info(
                f"[WIKI] Title corrected: '{show_title}' -> '{canonical_title}'."
            )
        logger.debug(f"[WIKI] Main page URL: {main_page_url}")
    except Exception:
        logger.error(
            f"[WIKI] Failed resolving main page for '{show_title}'. Continuing without correction."
        )
        if not _last_resort:
            qualified = f"{show_title} (TV series)"
            logger.info(
                f"[WIKI] Retrying with TV qualifier as last resort: '{qualified}'"
            )
            return await fetch_episode_titles_for_season(
                qualified, season, _last_resort=True
            )
        return {}, None

    # Step 2: Prefer dedicated list page; fallback to main page
    html_to_scrape: str | None = None
    try:
        # Try with the user-provided title first to avoid incorrect canonical corrections
        direct_query_user = f"List of {show_title} episodes"
        logger.info(
            f"[WIKI] Attempting dedicated list page lookup: '{direct_query_user}'."
        )
        list_page_user = await asyncio.to_thread(
            wikipedia.page, direct_query_user, auto_suggest=False, redirect=True
        )
        logger.debug(f"[WIKI] List page URL: {list_page_user.url}")
        html_to_scrape = await _get_page_html(list_page_user.url)
    except Exception:
        try:
            # If that failed, try with canonical title next
            direct_query_canon = f"List of {canonical_title} episodes"
            logger.info(
                f"[WIKI] Dedicated list page not found. Retrying with canonical: '{direct_query_canon}'."
            )
            list_page_canon = await asyncio.to_thread(
                wikipedia.page, direct_query_canon, auto_suggest=False, redirect=True
            )
            logger.debug(f"[WIKI] List page URL: {list_page_canon.url}")
            html_to_scrape = await _get_page_html(list_page_canon.url)
        except Exception:
            if main_page_url:
                logger.info(
                    f"[WIKI] Dedicated list page not found. Falling back to main page for '{canonical_title}'."
                )
                html_to_scrape = await _get_page_html(main_page_url)

    if not html_to_scrape:
        logger.warning(
            f"[WIKI] No HTML retrieved for '{canonical_title}'. Returning empty titles."
        )
        if not _last_resort:
            qualified = f"{show_title} (TV series)"
            logger.info(
                f"[WIKI] Retrying with TV qualifier as last resort: '{qualified}'"
            )
            return await fetch_episode_titles_for_season(
                qualified, season, _last_resort=True
            )
        return {}, corrected_show_title

    soup = BeautifulSoup(html_to_scrape, "lxml")
    titles_map = await _extract_titles_for_season(soup, season)
    if titles_map:
        logger.info(
            f"[WIKI] Parsed {len(titles_map)} episode titles for '{canonical_title}' S{season:02d}."
        )
        _WIKI_TITLES_CACHE[cache_key] = (titles_map, corrected_show_title)
    if not titles_map and not _last_resort:
        qualified = f"{show_title} (TV series)"
        logger.info(
            f"[WIKI] No titles parsed. Retrying with TV qualifier as last resort: '{qualified}'"
        )
        return await fetch_episode_titles_for_season(
            qualified, season, _last_resort=True
        )
    return titles_map, corrected_show_title


async def fetch_total_seasons_from_wikipedia(
    show_title: str, _last_resort: bool = False
) -> int | None:
    """Determine the total number of seasons for a TV show using Wikipedia.

    Strategy:
    1) Resolve the canonical show title (handles redirects and corrections).
    2) Prefer the dedicated "List of ... episodes" page; fallback to the main page.
    3) Parse for a "Series overview" table to enumerate seasons; otherwise, count
       distinct "Season N" headers.

    Returns the count of seasons if found, otherwise None.
    """
    canonical_title = show_title
    main_page_url: str | None = None

    # Step 1: Resolve main show page to get canonical title
    try:
        logger.info(
            f"[WIKI] Resolving main show page for '{show_title}' to determine total seasons."
        )
        search_results = await asyncio.to_thread(wikipedia.search, show_title)
        if not search_results:
            logger.warning(f"[WIKI] No Wikipedia search results for '{show_title}'.")
            if not _last_resort:
                qualified = f"{show_title} (TV series)"
                logger.info(
                    f"[WIKI] Retrying with TV qualifier as last resort: '{qualified}'"
                )
                return await fetch_total_seasons_from_wikipedia(
                    qualified, _last_resort=True
                )
            return None
        main_page_title = search_results[0]
        main_page = await asyncio.to_thread(
            wikipedia.page, main_page_title, auto_suggest=False, redirect=True
        )
        main_page_url = main_page.url
        if main_page.title != show_title:
            canonical_title = main_page.title
            logger.info(
                f"[WIKI] Title corrected: '{show_title}' -> '{canonical_title}'."
            )
        logger.debug(f"[WIKI] Main page URL: {main_page_url}")
    except Exception as e:
        logger.error(
            f"[WIKI] Failed resolving main page for '{show_title}': {e}. Continuing without correction."
        )
        if not _last_resort:
            qualified = f"{show_title} (TV series)"
            logger.info(
                f"[WIKI] Retrying with TV qualifier as last resort: '{qualified}'"
            )
            return await fetch_total_seasons_from_wikipedia(
                qualified, _last_resort=True
            )
        return None

    # Step 2: Prefer dedicated list page; fallback to main page
    html_to_scrape: str | None = None
    try:
        direct_query = f"List of {canonical_title} episodes"
        logger.info(f"[WIKI] Attempting dedicated list page lookup: '{direct_query}'.")
        list_page = await asyncio.to_thread(
            wikipedia.page, direct_query, auto_suggest=False, redirect=True
        )
        logger.debug(f"[WIKI] List page URL: {list_page.url}")
        html_to_scrape = await _get_page_html(list_page.url)
    except Exception:
        if main_page_url:
            logger.info(
                f"[WIKI] Dedicated list page not found. Falling back to main page for '{canonical_title}'."
            )
            html_to_scrape = await _get_page_html(main_page_url)

    if not html_to_scrape:
        logger.warning(
            f"[WIKI] No HTML retrieved for '{canonical_title}'. Unable to determine season count."
        )
        if not _last_resort:
            qualified = f"{show_title} (TV series)"
            logger.info(
                f"[WIKI] Retrying with TV qualifier as last resort: '{qualified}'"
            )
            return await fetch_total_seasons_from_wikipedia(
                qualified, _last_resort=True
            )
        return None

    soup = BeautifulSoup(html_to_scrape, "lxml")

    # Strategy A: Use "Series overview" table if present
    try:
        for table in soup.find_all("table", class_="wikitable"):
            if not isinstance(table, Tag):
                continue
            header_row = table.find("tr")
            if not isinstance(header_row, Tag):
                continue
            headers = [
                th.get_text(strip=True).lower() for th in header_row.find_all("th")
            ]
            if not headers:
                continue
            # Look for a table where first column is Season and where there is a column mentioning Episodes
            if ("season" in headers[0]) and any("episode" in h for h in headers):
                seasons: set[int] = set()
                for row in table.find_all("tr")[1:]:
                    if not isinstance(row, Tag):
                        continue
                    cells = row.find_all(["td", "th"])
                    if not cells:
                        continue
                    season_num = extract_first_int(cells[0].get_text(strip=True))
                    if season_num:
                        seasons.add(season_num)
                if seasons:
                    count = max(seasons)
                    logger.info(
                        f"[WIKI] Season count for '{canonical_title}' (from overview table): {count}"
                    )
                    return count
    except Exception as e:
        logger.debug(
            f"[WIKI] Failed parsing 'Series overview' table for '{canonical_title}': {e}"
        )

    # Strategy B: Count distinct Season headers (h2/h3) like "Season 1", "Season 2", ...
    try:
        season_pattern = re.compile(r"Season\s+(\d+)", re.IGNORECASE)
        seasons_found: set[int] = set()
        for tag in soup.find_all(["h2", "h3"]):
            text = tag.get_text(" ", strip=True)
            m = season_pattern.search(text)
            if m:
                try:
                    seasons_found.add(int(m.group(1)))
                except ValueError:
                    continue
        if seasons_found:
            count = max(seasons_found)
            logger.info(
                f"[WIKI] Season count for '{canonical_title}' (from headers): {count}"
            )
            return count
    except Exception as e:
        logger.debug(
            f"[WIKI] Failed parsing season headers for '{canonical_title}': {e}"
        )

    logger.warning(f"[WIKI] Unable to determine season count for '{canonical_title}'.")
    # As a last step, explicitly try with TV-series qualifier if we haven't already
    if not _last_resort:
        qualified = f"{show_title} (TV series)"
        logger.info(
            f"[WIKI] Retrying season-count lookup with TV qualifier as last resort: '{qualified}'"
        )
        return await fetch_total_seasons_from_wikipedia(qualified, _last_resort=True)
    return None


async def _parse_episode_tables(
    soup: BeautifulSoup, season: int, episode: int
) -> str | None:
    """
    Orchestrates different strategies to parse episode titles from tables.

    This function now intelligently calls different, specialized parsing functions
    based on the likely structure of the page (dedicated vs. embedded).
    """
    logger.info("[WIKI] Parsing HTML for episode tables.")

    # --- Strategy for DEDICATED pages: Look for explicit Season headers/links ---
    season_header_pattern = re.compile(rf"Season\s+{season}", re.IGNORECASE)
    header_tag = soup.find(
        lambda tag: tag.name in ["h2", "h3"]
        and bool(season_header_pattern.search(tag.get_text()))
    )
    if isinstance(header_tag, Tag):
        target_table = header_tag.find_next("table", class_="wikitable")
        if isinstance(target_table, Tag):
            logger.info("[WIKI] Found explicit season header. Using DEDICATED parser.")
            return await _extract_title_from_dedicated_table(
                target_table, season, episode
            )

    # --- Strategy for EMBEDDED lists: Look for a generic "Episodes" header ---
    episodes_header_pattern = re.compile(r"Episodes", re.IGNORECASE)
    episodes_header_tag = soup.find(
        lambda tag: tag.name in ["h2", "h3"]
        and bool(episodes_header_pattern.search(tag.get_text()))
    )
    if isinstance(episodes_header_tag, Tag):
        target_table = episodes_header_tag.find_next("table", class_="wikitable")
        if isinstance(target_table, Tag):
            logger.info(
                "[WIKI] Found generic 'Episodes' header. Using EMBEDDED parser."
            )
            return await _extract_title_from_embedded_table(
                target_table, season, episode
            )

    logger.warning(
        f"[WIKI] All parsing strategies failed to find S{season:02d}E{episode:02d}."
    )
    return None


async def _extract_titles_for_season(
    soup: BeautifulSoup, season: int
) -> dict[int, str]:
    """Extracts a mapping of episode number -> title for a given season.

    Uses the same heuristics as _parse_episode_tables, but returns all titles
    for the season in one pass, and is resilient to table layout variations
    and different quote styles on Wikipedia.
    """

    def _get_column_indices(
        table: Tag, *, default_ep: int, default_title: int
    ) -> tuple[int, int]:
        ep_idx, title_idx = default_ep, default_title
        header_row = table.find("tr")
        if isinstance(header_row, Tag):
            headers = [
                th.get_text(strip=True).lower() for th in header_row.find_all("th")
            ]
            # Prefer "No. in season"
            for i, h in enumerate(headers):
                if ("no" in h and "season" in h) or ("in season" in h):
                    ep_idx = i
                    break
            else:
                for i, h in enumerate(headers):
                    if "no" in h:
                        ep_idx = i
                        break
            for i, h in enumerate(headers):
                if "title" in h:
                    title_idx = i
                    break
        return ep_idx, title_idx

    def _extract_title_text(title_cell: Tag) -> str:
        italic = title_cell.find("i")
        if italic and italic.get_text(strip=True):
            return italic.get_text(strip=True)
        anchor = title_cell.find("a")
        if anchor and anchor.get_text(strip=True):
            return anchor.get_text(strip=True)
        text_full = title_cell.get_text(" ", strip=True)
        m = re.search("[\\\"“”'‘’]([^\\\"“”'‘’]+)[\\\"“”'‘’]", text_full)
        if m:
            return m.group(1).strip()
        return text_full.strip('"')

    results: dict[int, str] = {}

    # Strategy 1: dedicated season table under a specific Season header
    season_header_pattern = re.compile(rf"Season\s+{season}", re.IGNORECASE)
    header_tag = soup.find(
        lambda tag: tag.name in ["h2", "h3"]
        and bool(season_header_pattern.search(tag.get_text()))
    )
    if isinstance(header_tag, Tag):
        target_table = header_tag.find_next("table", class_="wikitable")
        if isinstance(target_table, Tag):
            ep_idx, title_idx = _get_column_indices(
                target_table, default_ep=1, default_title=2
            )
            for row in target_table.find_all("tr")[1:]:
                if not isinstance(row, Tag):
                    continue
                cells = row.find_all(["th", "td"])
                if len(cells) <= max(ep_idx, title_idx):
                    continue
                try:
                    ep_text = cells[ep_idx].get_text(" ", strip=True)
                    ep_num = extract_first_int(ep_text)
                    if not ep_num:
                        continue
                    title_cell = cells[title_idx]
                    if not isinstance(title_cell, Tag):
                        continue
                    results[ep_num] = _extract_title_text(title_cell)
                except Exception:
                    continue
            if results:
                return results

    # Strategy 2: simpler embedded episodes table under a generic header
    episodes_header_pattern = re.compile(r"Episodes", re.IGNORECASE)
    episodes_header_tag = soup.find(
        lambda tag: tag.name in ["h2", "h3"]
        and bool(episodes_header_pattern.search(tag.get_text()))
    )
    if isinstance(episodes_header_tag, Tag):
        target_table = episodes_header_tag.find_next("table", class_="wikitable")
        if isinstance(target_table, Tag):
            ep_idx, title_idx = _get_column_indices(
                target_table, default_ep=0, default_title=1
            )
            for row in target_table.find_all("tr")[1:]:
                if not isinstance(row, Tag):
                    continue
                cells = row.find_all(["td", "th"])
                if len(cells) <= max(ep_idx, title_idx):
                    continue
                try:
                    ep_num = extract_first_int(cells[ep_idx].get_text(" ", strip=True))
                    if not ep_num:
                        continue
                    title_cell = cells[title_idx]
                    if not isinstance(title_cell, Tag):
                        continue
                    results[ep_num] = _extract_title_text(title_cell)
                except Exception:
                    continue

    return results


async def _extract_title_from_dedicated_table(
    table: Tag, season: int, episode: int
) -> str | None:
    """
    (FOR DEDICATED PAGES) Extracts a title from a complex wikitable, typically
    found on "List of..." episode pages. It assumes a multi-column layout
    that may include overall episode numbers.
    """
    # This logic is restored from the previously working version for dedicated pages
    for row in table.find_all("tr")[1:]:  # Skip header row
        if not isinstance(row, Tag):
            continue

        cells = row.find_all(["th", "td"])
        if len(cells) < 3:
            continue

        try:
            # Column 1 is "No. in season" on these pages
            episode_cell_text = cells[1].get_text(strip=True)
            if extract_first_int(episode_cell_text) != episode:
                continue

            # Column 2 is the title
            title_cell = cells[2]
            if not isinstance(title_cell, Tag):
                continue

            # Find title (preferring text in quotes)
            found_text = title_cell.find(string=re.compile(r'"([^"]+)"'))
            if found_text:
                return str(found_text).strip().strip('"')
            italic_text = title_cell.find("i")
            if italic_text:
                return italic_text.get_text(strip=True)
            return title_cell.get_text(strip=True).strip('"')
        except (ValueError, IndexError):
            continue
    return None


async def _extract_title_from_embedded_table(
    table: Tag, season: int, episode: int
) -> str | None:
    """
    (FOR EMBEDDED PAGES) Extracts a title from a simpler table structure,
    typically found on a show's main page under an "Episodes" header.
    """
    for row in table.find_all("tr")[1:]:  # Skip header
        if not isinstance(row, Tag):
            continue

        cells = row.find_all(["td", "th"])
        if len(cells) < 2:  # Embedded tables often have just "No." and "Title"
            continue

        try:
            # Column 0 is "No." in this simpler format
            episode_cell_text = cells[0].get_text(strip=True)
            if extract_first_int(episode_cell_text) != episode:
                continue

            # Column 1 is the title
            title_cell = cells[1]
            if not isinstance(title_cell, Tag):
                continue

            # Same reliable title extraction logic
            found_text = title_cell.find(string=re.compile(r'"([^"]+)"'))
            if found_text:
                return str(found_text).strip().strip('"')
            italic_text = title_cell.find("i")
            if italic_text:
                return italic_text.get_text(strip=True)
            return title_cell.get_text(strip=True).strip('"')
        except (ValueError, IndexError):
            continue
    return None


async def fetch_season_episode_count_from_wikipedia(
    show_title: str, season: int, _last_resort: bool = False
) -> int | None:
    """Fetches the number of episodes for a given season from Wikipedia."""
    logger.info(
        f"[WIKI] Fetching episode count for '{show_title}' S{season:02d} from Wikipedia."
    )
    html_to_scrape = None
    try:
        # Attempt to get the dedicated "List of..." page first
        logger.debug(
            f"[WIKI] Trying dedicated list page for '{show_title}': 'List of {show_title} episodes'."
        )
        list_page = await asyncio.to_thread(
            wikipedia.page,
            f"List of {show_title} episodes",
            auto_suggest=False,
            redirect=True,
        )
        logger.debug(
            f"[WIKI] List page resolved -> title: '{getattr(list_page, 'title', '?')}', url: {getattr(list_page, 'url', '?')}"
        )
        html_to_scrape = await _get_page_html(list_page.url)
    except wikipedia.exceptions.PageError:
        # Fallback to the main show page if the list page doesn't exist
        try:
            logger.debug(
                f"[WIKI] Dedicated list page missing. Performing search-first fallback for '{show_title}'."
            )
            # Search-first approach to avoid autosuggest mis-corrections like 'allen earth'
            search_results = await asyncio.to_thread(wikipedia.search, show_title)
            logger.debug(
                f"[WIKI] Search results for '{show_title}': {search_results[:5] if search_results else '[]'}"
            )
            if not search_results:
                logger.error(
                    f"[WIKI] No search results for '{show_title}' during fallback."
                )
                return None

            # Prefer the first search result; disable autosuggest for determinism
            main_page = await asyncio.to_thread(
                wikipedia.page, search_results[0], auto_suggest=False, redirect=True
            )
            logger.debug(
                f"[WIKI] Fallback main page -> title: '{getattr(main_page, 'title', '?')}', url: {getattr(main_page, 'url', '?')}"
            )
            html_to_scrape = await _get_page_html(main_page.url)
        except wikipedia.exceptions.DisambiguationError as e:
            # Pick the first disambiguation option deterministically and log choices
            options_preview = e.options[:5] if hasattr(e, "options") else []
            logger.debug(
                f"[WIKI] Disambiguation for '{show_title}'. Options: {options_preview}"
            )
            try:
                choice = e.options[0]
                chosen_page = await asyncio.to_thread(
                    wikipedia.page, choice, auto_suggest=False, redirect=True
                )
                logger.debug(
                    f"[WIKI] Disambiguation choice -> title: '{getattr(chosen_page, 'title', '?')}', url: {getattr(chosen_page, 'url', '?')}"
                )
                html_to_scrape = await _get_page_html(chosen_page.url)
            except Exception as e2:
                logger.error(
                    f"[WIKI] Failed to resolve disambiguation for '{show_title}': {e2}"
                )
                return None
        except Exception as e:
            # Also attempt to log what autosuggest would have done for diagnostics
            try:
                auto_page = await asyncio.to_thread(
                    wikipedia.page, show_title, auto_suggest=True, redirect=True
                )
                logger.debug(
                    f"[WIKI] Autosuggest diagnostic -> title: '{getattr(auto_page, 'title', '?')}', url: {getattr(auto_page, 'url', '?')}"
                )
            except Exception as diag:
                logger.debug(
                    f"[WIKI] Autosuggest diagnostic raised: {type(diag).__name__}: {diag}"
                )
            logger.error(f"[WIKI] Failed to fetch page for '{show_title}': {e}")
            if not _last_resort:
                qualified = f"{show_title} (TV series)"
                logger.info(
                    f"[WIKI] Retrying with TV qualifier as last resort: '{qualified}'"
                )
                return await fetch_season_episode_count_from_wikipedia(
                    qualified, season, _last_resort=True
                )
            return None
    except Exception as e:
        logger.error(f"[WIKI] Unexpected error when fetching page: {e}")
        if not _last_resort:
            qualified = f"{show_title} (TV series)"
            logger.info(
                f"[WIKI] Retrying with TV qualifier as last resort: '{qualified}'"
            )
            return await fetch_season_episode_count_from_wikipedia(
                qualified, season, _last_resort=True
            )
        return None

    if not html_to_scrape:
        logger.warning(
            f"[WIKI] No HTML retrieved for '{show_title}' S{season:02d}. Unable to determine episode count."
        )
        if not _last_resort:
            qualified = f"{show_title} (TV series)"
            logger.info(
                f"[WIKI] Retrying with TV qualifier as last resort: '{qualified}'"
            )
            return await fetch_season_episode_count_from_wikipedia(
                qualified, season, _last_resort=True
            )
        return None

    soup = BeautifulSoup(html_to_scrape, "lxml")

    # First, attempt episode-title extraction (reflects enumerated episodes on the page)
    count_from_titles: int | None = None
    try:
        titles_map = await _extract_titles_for_season(soup, season)
        if titles_map:
            ep_numbers = sorted(titles_map.keys())
            # Use the max explicit episode number when available; otherwise fall back to the number of rows parsed
            count_from_titles = ep_numbers[-1] if ep_numbers else len(titles_map)
            logger.info(
                f"[WIKI] Episode count (from titles) for '{show_title}' S{season:02d}: {count_from_titles}"
            )
    except Exception as e:
        logger.debug(
            f"[WIKI] Title-based episode enumeration failed for '{show_title}' S{season:02d}: {e}"
        )

    overview_table = None

    # Find the "Series overview" table
    for table in soup.find_all("table", class_="wikitable"):
        if not isinstance(table, Tag):
            continue

        header_row = table.find("tr")
        if not isinstance(header_row, Tag):
            continue

        headers = [th.get_text(strip=True).lower() for th in header_row.find_all("th")]
        # Check if this looks like the right table
        if headers and "season" in headers[0] and any("episode" in h for h in headers):
            overview_table = table
            break

    if not isinstance(overview_table, Tag):
        logger.debug(f"[WIKI] 'Series overview' table not found for '{show_title}'.")
        # If we already have a titles-derived count, return it
        if isinstance(count_from_titles, int) and count_from_titles > 0:
            logger.info(
                f"[WIKI] Using titles-derived count for '{show_title}' S{season:02d}: {count_from_titles}"
            )
            return count_from_titles
        if not _last_resort:
            qualified = f"{show_title} (TV series)"
            logger.info(
                f"[WIKI] Retrying with TV qualifier as last resort: '{qualified}'"
            )
            return await fetch_season_episode_count_from_wikipedia(
                qualified, season, _last_resort=True
            )
        return None

    # Find the column index for "Episodes" and optionally "Last aired"/"Originally aired"
    header_row = overview_table.find("tr")
    if not isinstance(header_row, Tag):
        logger.debug(
            f"[WIKI] Header row not found in overview table for '{show_title}'."
        )
        return None

    header_cells = [th.get_text(strip=True).lower() for th in header_row.find_all("th")]
    episodes_col_index = -1
    last_aired_col_index = -1
    originally_aired_col_index = -1
    for idx, text in enumerate(header_cells):
        if "episode" in text:
            episodes_col_index = idx
            break
    # Try to capture columns indicating ongoing seasons
    for idx, text in enumerate(header_cells):
        if "last" in text and "air" in text:
            last_aired_col_index = idx
            break
    for idx, text in enumerate(header_cells):
        if ("originally" in text and "air" in text) or (
            "original" in text and "release" in text
        ):
            originally_aired_col_index = idx
            break

    if episodes_col_index == -1:
        logger.debug(
            f"[WIKI] Could not locate 'Episodes' column in overview table for '{show_title}'."
        )
        return None

    # Find the specific season in the table
    for row in overview_table.find_all("tr")[1:]:
        if not isinstance(row, Tag):
            continue

        cells = row.find_all(["td", "th"])
        if (
            len(cells) <= episodes_col_index
        ):  # Ensure the episode column exists in this row
            continue

        season_num = extract_first_int(cells[0].get_text(strip=True))
        if season_num == season:
            # If the season appears to be ongoing, avoid trusting the overview count.
            def _cell_text(i: int) -> str:
                try:
                    return cells[i].get_text(" ", strip=True)
                except Exception:
                    return ""

            last_text = (
                _cell_text(last_aired_col_index) if last_aired_col_index != -1 else ""
            )
            orig_text = (
                _cell_text(originally_aired_col_index)
                if originally_aired_col_index != -1
                else ""
            )
            combined_air_text = f"{last_text} {orig_text}".lower()
            if any(k in combined_air_text for k in ("present", "tba", "ongoing")):
                logger.info(
                    f"[WIKI] Overview indicates season is ongoing for '{show_title}' S{season:02d}; skipping overview count."
                )
                # If we have titles-based count, return it; otherwise unknown
                if isinstance(count_from_titles, int) and count_from_titles > 0:
                    return count_from_titles
                return None
            ep_text = cells[episodes_col_index].get_text(strip=True)
            ep_count = extract_first_int(ep_text)
            logger.info(
                f"[WIKI] Episode count (from overview) for '{show_title}' S{season:02d}: {ep_count}"
            )
            # Prefer the larger of titles-derived and overview when both exist
            if (
                isinstance(count_from_titles, int)
                and count_from_titles > 0
                and ep_count
            ):
                chosen = max(count_from_titles, ep_count)
                logger.info(
                    f"[WIKI] Using max(count_from_titles, overview) = {chosen} for '{show_title}' S{season:02d}."
                )
                return chosen
            return ep_count  # Return the extracted episode count

    # If we reach here, no overview match; fallback to titles-based count if available
    if isinstance(count_from_titles, int) and count_from_titles > 0:
        logger.info(
            f"[WIKI] Using titles-derived fallback for '{show_title}' S{season:02d}: {count_from_titles}"
        )
        return count_from_titles
    return None
