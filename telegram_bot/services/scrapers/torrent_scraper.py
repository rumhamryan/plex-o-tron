# telegram_bot/services/scrapers/torrent_scraper.py

import asyncio
from pathlib import Path
import re
import urllib.parse
import time
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag
from telegram.ext import ContextTypes
from thefuzz import fuzz, process

from ...config import logger, MAX_TORRENT_SIZE_GB
from .scoring import parse_codec, score_torrent_result
from ...utils import parse_torrent_name
from ..generic_torrent_scraper import GenericTorrentScraper, load_site_config


async def scrape_1337x(
    query: str,
    media_type: str,
    site_url: str | None = None,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Scrape 1337x using the generic scraper framework."""
    try:
        config_path = (
            Path(__file__).resolve().parent.parent.parent
            / "scrapers"
            / "configs"
            / "1337x.yaml"
        )
        site_config = load_site_config(config_path)
    except Exception as exc:
        logger.error(f"[SCRAPER] Failed to load 1337x config: {exc}")
        return []

    # Get preferences for scoring
    prefs_key = "movies" if "movie" in media_type else "tv"
    preferences = {}
    if context:
        preferences = (
            context.bot_data.get("SEARCH_CONFIG", {})
            .get("preferences", {})
            .get(prefs_key, {})
        )

    scraper = GenericTorrentScraper(site_config)
    raw_results = await scraper.search(query, media_type, **kwargs)

    results: list[dict[str, Any]] = []
    for item in raw_results:
        # GenericTorrentScraper returns dicts of TorrentData
        name = item.get("name", "")
        seeders = item.get("seeders", 0)
        uploader = item.get("uploader") or "Anonymous"

        score = score_torrent_result(name, uploader, preferences, seeders=seeders)
        if score <= 0:
            continue

        parsed_name = parse_torrent_name(name)
        results.append(
            {
                "title": name,
                "page_url": item.get("magnet_url"),
                "score": score,
                "source": item.get("source_site", "1337x"),
                "uploader": uploader,
                "size_gb": item.get("size_bytes", 0) / (1024**3),
                "codec": parse_codec(name),
                "seeders": seeders,
                "leechers": item.get("leechers", 0),
                "year": parsed_name.get("year"),
            }
        )
    return results


from .base_scraper import Scraper


async def scrape_yts(
    query: str,
    media_type: str,
    site_url: str | None = None,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Wrapper for YTS scraper to match the generic scraper function signature."""
    if context:
        kwargs["context"] = context

    scraper = YtsScraper()
    return await scraper.search(query, media_type, **kwargs)


class YtsScraper(Scraper):
    """
    Scraper for YTS.mx.
    """

    async def search(
        self,
        query: str,
        media_type: str,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Uses the YTS.mx API and website to find movie torrents."""
        year = kwargs.get("year")
        resolution = kwargs.get("resolution")
        context = kwargs.get("context")
        logger.info(
            f"[SCRAPER] YTS: Initiating API-based scrape for '{query}' (Year: {year}, Res: {resolution})."
        )

        if not context:
            logger.warning(
                "[SCRAPER] YTS: No context provided. Cannot get preferences."
            )
            return []

        preferences = (
            context.bot_data.get("SEARCH_CONFIG", {})
            .get("preferences", {})
            .get("movies", {})
        )
        if not preferences:
            return []

        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }

                # --- helpers for robust title matching ---
                STOPWORDS = {"the", "a", "an", "of", "and"}

                def _tokens(s: str) -> set[str]:
                    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if t}

                base_tokens = _tokens(query) - STOPWORDS

                def _passes_gate(title_text: str) -> bool:
                    # Require at least one non-stopword token from query to appear in candidate
                    cand_tokens = _tokens(title_text)
                    return (
                        any(t in cand_tokens for t in base_tokens)
                        if base_tokens
                        else True
                    )

                # Stage 1: Scrape search results to find the movie's page URL
                formatted_query = urllib.parse.quote_plus(query)

                async def _fetch_browse_page(url: str) -> BeautifulSoup | None:
                    try:
                        resp = await client.get(url, headers=headers)
                        resp.raise_for_status()
                        return BeautifulSoup(resp.text, "lxml")
                    except Exception:
                        return None

                def _add_page_param(url: str, page_num: int) -> str:
                    if page_num <= 1:
                        return url
                    joiner = "&" if "?" in url else "?"
                    return f"{url}{joiner}page={page_num}"

                def _collect_choices_from_soup(soup: BeautifulSoup) -> dict[str, str]:
                    out: dict[str, str] = {}
                    for movie_wrapper in soup.find_all(
                        "div", class_="browse-movie-wrap"
                    ):
                        if not isinstance(movie_wrapper, Tag):
                            continue
                        year_tag = movie_wrapper.find("div", class_="browse-movie-year")
                        scraped_year = (
                            year_tag.get_text(strip=True)
                            if isinstance(year_tag, Tag)
                            else None
                        )
                        if year and scraped_year and year != scraped_year:
                            continue
                        title_tag = movie_wrapper.find("a", class_="browse-movie-title")
                        if isinstance(title_tag, Tag):
                            href = title_tag.get("href")
                            title_text = title_tag.get_text(strip=True)
                            if isinstance(href, str) and title_text:
                                # Gate by tokens when a year is specified to avoid near-homonyms
                                if not year or _passes_gate(title_text):
                                    out[href] = title_text
                    return out

                # Try the first page. If nothing matches (common for older films due to sorting),
                # paginate up to a small max to discover the intended year.
                base_search_url = f"https://yts.mx/browse-movies/{formatted_query}"
                choices: dict[str, str] = {}
                first_soup = await _fetch_browse_page(base_search_url)
                if first_soup:
                    choices.update(_collect_choices_from_soup(first_soup))

                if not choices and year:
                    for page_num in range(2, 6):  # check a few pages for older titles
                        paged_url = _add_page_param(base_search_url, page_num)
                        soup = await _fetch_browse_page(paged_url)
                        if not soup:
                            continue
                        choices.update(_collect_choices_from_soup(soup))
                        if choices:
                            break

                async def _api_fallback() -> list[dict[str, Any]]:
                    """Query YTS list_movies API directly and build results when browse fails or is ambiguous."""

                    def _build_results_from_movies(
                        movies: list[dict[str, Any]],
                    ) -> list[dict[str, Any]]:
                        out: list[dict[str, Any]] = []
                        for mv in movies:
                            try:
                                mv_title = (
                                    mv.get("title_long") or mv.get("title") or query
                                )
                                mv_year = mv.get("year")
                                if year and mv_year and str(mv_year) != str(year):
                                    continue
                                if year and not _passes_gate(str(mv_title)):
                                    # Avoid near-homonyms like 'The Dunes'
                                    continue
                                for tor in mv.get("torrents", []) or []:
                                    quality = str(tor.get("quality", "")).lower()
                                    if (
                                        resolution
                                        and isinstance(resolution, str)
                                        and resolution.lower() not in quality
                                    ):
                                        continue
                                    size_gb = (tor.get("size_bytes", 0) or 0) / (
                                        1024**3
                                    )
                                    if size_gb > MAX_TORRENT_SIZE_GB:
                                        continue
                                    info_hash = tor.get("hash")
                                    if not info_hash:
                                        continue
                                    title_full = f"{mv_title} [{tor.get('quality')}.{tor.get('type')}] [YTS.MX]"
                                    trackers = "&tr=" + "&tr=".join(
                                        [
                                            "udp://open.demonii.com:1337/announce",
                                            "udp://tracker.openbittorrent.com:80",
                                        ]
                                    )
                                    magnet_link = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote_plus(str(mv_title))}{trackers}"
                                    seeders_count = int(tor.get("seeds", 0) or 0)
                                    parsed_codec = _parse_codec(title_full) or "x264"
                                    score = score_torrent_result(
                                        title_full,
                                        "YTS",
                                        preferences,
                                        seeders=seeders_count,
                                    )
                                    out.append(
                                        {
                                            "title": title_full,
                                            "page_url": magnet_link,
                                            "score": score,
                                            "source": "YTS.mx",
                                            "uploader": "YTS",
                                            "size_gb": size_gb,
                                            "codec": parsed_codec,
                                            "seeders": seeders_count,
                                            "year": mv_year,
                                        }
                                    )
                            except Exception:
                                continue
                        return out

                    async def _call_list_movies(
                        params: dict[str, Any],
                    ) -> list[dict[str, Any]]:
                        try:
                            resp = await client.get(
                                "https://yts.mx/api/v2/list_movies.json", params=params
                            )
                            resp.raise_for_status()
                            payload = resp.json()
                        except Exception as exc:  # noqa: BLE001
                            logger.debug(
                                "[SCRAPER] YTS API fallback request failed: %s", exc
                            )
                            return []
                        data = (
                            payload.get("data") if isinstance(payload, dict) else None
                        )
                        movies = data.get("movies") if isinstance(data, dict) else None
                        if not isinstance(movies, list):
                            return []
                        return _build_results_from_movies(movies)

                    # Attempt 1: honor both year and quality if provided
                    base_params: dict[str, Any] = {"query_term": query, "limit": 50}
                    if isinstance(resolution, str) and resolution.lower() in {
                        "720p",
                        "1080p",
                        "2160p",
                    }:
                        base_params["quality"] = resolution.lower()
                    if year and str(year).isdigit():
                        base_params["year"] = str(year)
                    results = await _call_list_movies(base_params)
                    if results:
                        logger.info(
                            "[SCRAPER] YTS API fallback finished. Found %d torrents.",
                            len(results),
                        )
                        return results

                    # Attempt 2: drop quality, keep year
                    params_no_quality = {
                        k: v for k, v in base_params.items() if k != "quality"
                    }
                    results = await _call_list_movies(params_no_quality)
                    if results:
                        logger.info(
                            "[SCRAPER] YTS API fallback (no quality) finished. Found %d torrents.",
                            len(results),
                        )
                        return results

                    # Attempt 3: drop year filter from request but filter in-code by year
                    params_no_year = {
                        k: v for k, v in base_params.items() if k != "year"
                    }
                    results_all_years = await _call_list_movies(params_no_year)
                    if results_all_years:
                        # _build_results_from_movies() already filters by 'year' via closure
                        logger.info(
                            "[SCRAPER] YTS API fallback (no year param) finished. Found %d torrents after filtering.",
                            len(results_all_years),
                        )
                        return results_all_years

                    logger.info(
                        "[SCRAPER] YTS API fallback finished. Found 0 torrents."
                    )
                    return []

                if not choices:
                    if year:
                        logger.warning(
                            f"[SCRAPER] YTS Stage 1: No movies found matching year '{year}'. Trying API fallback."
                        )
                        return await _api_fallback()
                    else:
                        logger.warning(
                            f"[SCRAPER] YTS Stage 1: No movies found for '{query}'."
                        )
                        return []

                # Use a more robust scorer and apply token gating if year is provided
                best_match = process.extractOne(
                    query, choices, scorer=fuzz.token_set_ratio
                )
                is_confident = bool(
                    best_match
                    and len(best_match) == 3
                    and best_match[1] >= (80 if year else 86)
                )
                gated_ok = True
                if year and best_match and len(best_match) == 3:
                    candidate_title = choices.get(best_match[2], "")
                    gated_ok = _passes_gate(candidate_title)

                if not (is_confident and gated_ok):
                    logger.warning(
                        f"[SCRAPER] YTS Stage 1: No confident gated match for '{query}'. Best was: {best_match}. Trying API fallback."
                    )
                    api_results = await _api_fallback()
                    if api_results:
                        return api_results
                    else:
                        return []

                # The URL is the third element (the key from the choices dict).
                best_page_url = best_match[2]
                if not isinstance(best_page_url, str):
                    logger.error(
                        f"[SCRAPER ERROR] YTS Stage 1: Matched item key was not a string URL. Got: {best_page_url}"
                    )
                    return []

                # Stage 2: Scrape the movie's page to get its API ID
                response = await client.get(best_page_url, headers=headers)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "lxml")

                movie_info_div = soup.select_one("#movie-info")
                if not (
                    isinstance(movie_info_div, Tag)
                    and (movie_id := movie_info_div.get("data-movie-id"))
                ):
                    logger.error(
                        f"[SCRAPER ERROR] YTS Stage 2: Could not find data-movie-id on page {best_page_url}"
                    )
                    # Try API fallback as a last resort
                    return await _api_fallback()

                # Stage 3: Call the YTS API with the movie ID and validate
                api_url = (
                    f"https://yts.mx/api/v2/movie_details.json?movie_id={movie_id}"
                )
                api_data: dict[str, Any] | None = None
                movie_data: dict[str, Any] | None = None
                torrents: list[dict[str, Any]] = []

                for attempt in range(1, 4):
                    delay = 2 ** (attempt - 1)
                    api_start = time.perf_counter()
                    try:
                        response = await client.get(api_url)
                        duration = time.perf_counter() - api_start
                        response.raise_for_status()
                        api_data = response.json()
                    except Exception as e:
                        logger.debug(
                            (
                                "[SCRAPER] YTS API attempt %s request error: %s. "
                                "Retrying in %ss."
                            ),
                            attempt,
                            e,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue

                    # Narrow JSON types with explicit isinstance checks for IDEs
                    _data_field = (
                        api_data.get("data") if isinstance(api_data, dict) else None
                    )
                    movie_data = (
                        _data_field.get("movie")
                        if isinstance(_data_field, dict)
                        else None
                    )
                    torrents = (
                        movie_data.get("torrents", [])
                        if isinstance(movie_data, dict)
                        else []
                    )
                    conditions = []
                    status = (
                        api_data.get("status") if isinstance(api_data, dict) else None
                    )
                    if status != "ok":
                        conditions.append(f"status != 'ok' (got {status!r})")
                    if not movie_data:
                        conditions.append("missing 'movie' object")
                    if movie_data and not torrents:
                        conditions.append("missing 'torrents' entries")

                    if conditions:
                        logger.debug(
                            (
                                "[SCRAPER] YTS API attempt %s failed validation: %s. "
                                "Found %s torrents, expected >=1. Retrying in %ss."
                            ),
                            attempt,
                            "; ".join(conditions),
                            len(torrents),
                            delay,
                        )
                        await asyncio.sleep(delay)
                        continue

                    logger.debug(
                        (
                            "[SCRAPER] YTS API attempt %s succeeded in %.2fs "
                            "with %d torrents."
                        ),
                        attempt,
                        duration,
                        len(torrents),
                    )
                    break
                else:
                    logger.error(
                        (
                            "[SCRAPER ERROR] YTS API validation failed after 3 attempts "
                            f"for movie id {movie_id}."
                        )
                    )
                    # Try API fallback if details endpoint keeps failing
                    return await _api_fallback()

                # Stage 4: Parse the API response
                results: list[dict[str, Any]] = []
                movie_title = (
                    movie_data.get("title_long", query)
                    if isinstance(movie_data, dict)
                    else query
                )

                for torrent in torrents:
                    quality = torrent.get("quality", "").lower()
                    if not resolution or (
                        resolution
                        and isinstance(resolution, str)
                        and resolution.lower() in quality
                    ):
                        size_gb = torrent.get("size_bytes", 0) / (1024**3)
                        if size_gb > MAX_TORRENT_SIZE_GB:
                            continue

                        full_title = f"{movie_title} [{torrent.get('quality')}.{torrent.get('type')}] [YTS.MX]"
                        if info_hash := torrent.get("hash"):
                            trackers = "&tr=" + "&tr=".join(
                                [
                                    "udp://open.demonii.com:1337/announce",
                                    "udp://tracker.openbittorrent.com:80",
                                ]
                            )
                            magnet_link = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote_plus(movie_title)}{trackers}"

                            seeders_count = torrent.get("seeds", 0)
                            parsed_codec = (
                                parse_codec(full_title) or "x264"  # Default YTS to x264
                            )
                            score = score_torrent_result(
                                full_title, "YTS", preferences, seeders=seeders_count
                            )

                            results.append(
                                {
                                    "title": full_title,
                                    "page_url": magnet_link,
                                    "score": score,
                                    "source": "YTS.mx",
                                    "uploader": "YTS",
                                    "size_gb": size_gb,
                                    "codec": parsed_codec,
                                    "seeders": seeders_count,
                                    "year": (
                                        movie_data.get("year")
                                        if isinstance(movie_data, dict)
                                        else None
                                    ),
                                }
                            )

                logger.info(
                    f"[SCRAPER] YTS API scrape finished. Found {len(results)} matching torrents."
                )
                return results

        except Exception as e:
            logger.error(f"[SCRAPER ERROR] YTS scrape failed: {e}", exc_info=True)
            return []
