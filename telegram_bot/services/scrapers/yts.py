import asyncio
import re
import urllib.parse
import time
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag
from telegram.ext import ContextTypes
from thefuzz import fuzz, process

from ...config import logger, MAX_TORRENT_SIZE_GB
from ...utils import parse_codec, score_torrent_result


async def scrape_yts(
    query: str,
    media_type: str,
    search_url_template: str,
    context: ContextTypes.DEFAULT_TYPE,
    **kwargs,
) -> list[dict[str, Any]]:
    """Uses the YTS.lt API and website to find movie torrents."""
    year = kwargs.get("year")
    resolution = kwargs.get("resolution")
    year_str = str(year).strip() if year is not None else None
    if year_str == "":
        year_str = None

    max_size_gb = kwargs.get("max_size_gb", MAX_TORRENT_SIZE_GB)

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

            # --- helpers for robust title/year matching ---
            STOPWORDS = {"the", "a", "an", "of", "and"}
            YEAR_PATTERN = re.compile(r"\d{4}")

            def _tokens(s: str) -> set[str]:
                return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if t}

            base_tokens = _tokens(query) - STOPWORDS

            def _passes_gate(title_text: str) -> bool:
                # Require at least one non-stopword token from query to appear in candidate
                cand_tokens = _tokens(title_text)
                return (
                    any(t in cand_tokens for t in base_tokens) if base_tokens else True
                )

            def _normalize_year_text(value: str | None) -> str | None:
                if not value:
                    return None
                match = YEAR_PATTERN.search(value)
                if match:
                    return match.group(0)
                stripped = value.strip()
                return stripped or None

            # Stage 1: Scrape search results to find the movie's page URL
            formatted_query = urllib.parse.quote_plus(query)
            quality_slug = (
                str(resolution).strip().lower()
                if isinstance(resolution, str) and resolution.strip()
                else "all"
            )
            year_slug = year_str or "all"

            def _substitute_template(url_template: str) -> str:
                replacements = {
                    "{query}": formatted_query,
                    "{QUERY}": formatted_query,
                    "{quality}": urllib.parse.quote_plus(quality_slug),
                    "{QUALITY}": urllib.parse.quote_plus(quality_slug),
                    "{resolution}": urllib.parse.quote_plus(quality_slug),
                    "{RESOLUTION}": urllib.parse.quote_plus(quality_slug),
                    "{year}": urllib.parse.quote_plus(year_slug),
                    "{YEAR}": urllib.parse.quote_plus(year_slug),
                    "{media_type}": urllib.parse.quote_plus(media_type),
                    "{MEDIA_TYPE}": urllib.parse.quote_plus(media_type),
                }
                for placeholder, value in replacements.items():
                    url_template = url_template.replace(placeholder, value)
                return url_template

            base_search_url = _substitute_template(search_url_template)
            logger.info(
                f"[SCRAPER] YTS: Initiating API-based scrape for '{query}' (Year: {year}, Res: {resolution}) at {base_search_url}."
            )

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
                for movie_wrapper in soup.find_all("div", class_="browse-movie-wrap"):
                    if not isinstance(movie_wrapper, Tag):
                        continue
                    year_tag = movie_wrapper.find("div", class_="browse-movie-year")
                    scraped_year_raw = (
                        year_tag.get_text(strip=True)
                        if isinstance(year_tag, Tag)
                        else None
                    )
                    normalized_year = _normalize_year_text(scraped_year_raw)
                    if year_str and normalized_year and year_str != normalized_year:
                        continue
                    title_tag = movie_wrapper.find("a", class_="browse-movie-title")
                    if isinstance(title_tag, Tag):
                        href = title_tag.get("href")
                        title_text = title_tag.get_text(strip=True)
                        if isinstance(href, str) and title_text:
                            # Gate by tokens when a year is specified to avoid near-homonyms
                            if not year_str or _passes_gate(title_text):
                                out[href] = title_text
                return out

            # Try the first page. If nothing matches (common for older films due to sorting),
            # paginate up to a small max to discover the intended year.
            base_search_url = _substitute_template(search_url_template)
            choices: dict[str, str] = {}
            first_soup = await _fetch_browse_page(base_search_url)
            if first_soup:
                choices.update(_collect_choices_from_soup(first_soup))

            if not choices and year_str:
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
                            mv_title = mv.get("title_long") or mv.get("title") or query
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
                                size_gb = (tor.get("size_bytes", 0) or 0) / (1024**3)
                                if size_gb > max_size_gb:
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
                                if seeders_count < 20:
                                    continue
                                peers_count = int(tor.get("peers", 0) or 0)
                                leechers_count = max(peers_count - seeders_count, 0)

                                if "peers" not in tor:
                                    logger.warning(
                                        "[SCRAPER] YTS API result missing 'peers' field; defaulting leechers to 0"
                                    )

                                parsed_codec = parse_codec(title_full) or "x264"
                                score = score_torrent_result(
                                    title_full,
                                    "YTS",
                                    preferences,
                                    seeders=seeders_count,
                                    leechers=leechers_count,
                                )
                                out.append(
                                    {
                                        "title": title_full,
                                        "page_url": magnet_link,
                                        "info_url": mv.get("url"),
                                        "score": score,
                                        "source": "yts.lt",
                                        "uploader": "YTS",
                                        "size_gb": size_gb,
                                        "codec": parsed_codec,
                                        "seeders": seeders_count,
                                        "leechers": leechers_count,
                                        "year": mv_year,
                                    }
                                )
                        except Exception:
                            continue
                    return out

                async def _call_list_movies(
                    params: dict[str, Any],
                ) -> list[dict[str, Any]]:
                    url = "https://yts.lt/api/v2/list_movies.json"
                    try:
                        logger.debug(
                            f"[SCRAPER] YTS API call: {url} with params {params}"
                        )
                        resp = await client.get(url, params=params)
                        resp.raise_for_status()
                        payload = resp.json()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "[SCRAPER] YTS API fallback request failed: %s", exc
                        )
                        return []

                    data = payload.get("data") if isinstance(payload, dict) else None
                    movies_raw = data.get("movies") if isinstance(data, dict) else None

                    if not isinstance(movies_raw, list):
                        logger.debug(
                            "[SCRAPER] YTS API response missing 'movies' list."
                        )
                        return []

                    logger.debug(
                        f"[SCRAPER] YTS API returned {len(movies_raw)} movies before filtering."
                    )
                    if len(movies_raw) > 0 and len(movies_raw) <= 5:
                        sample_titles = [
                            (m.get("title"), m.get("year")) for m in movies_raw
                        ]
                        logger.debug(f"[SCRAPER] YTS API sample: {sample_titles}")

                    return _build_results_from_movies(movies_raw)

                # Attempt 1: honor both year and quality if provided
                logger.debug("[SCRAPER] YTS API Fallback: Attempt 1 (year and quality)")
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
                logger.debug("[SCRAPER] YTS API Fallback: Attempt 2 (year, no quality)")
                params_no_quality = {
                    k: v for k, v in base_params.items() if k != "quality"
                }
                if params_no_quality != base_params:
                    results = await _call_list_movies(params_no_quality)
                    if results:
                        logger.info(
                            "[SCRAPER] YTS API fallback (no quality) finished. Found %d torrents.",
                            len(results),
                        )
                        return results

                # Attempt 3: drop year filter from request but filter in-code by year
                logger.debug("[SCRAPER] YTS API Fallback: Attempt 3 (no year param)")
                params_no_year = {k: v for k, v in base_params.items() if k != "year"}
                if params_no_year != base_params:
                    results_all_years = await _call_list_movies(params_no_year)
                    if results_all_years:
                        logger.info(
                            "[SCRAPER] YTS API fallback (no year param) finished. Found %d torrents after filtering.",
                            len(results_all_years),
                        )
                        return results_all_years

                logger.info("[SCRAPER] YTS API fallback finished. Found 0 torrents.")
                return []

            if not choices:
                if year_str:
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
            best_match = process.extractOne(query, choices, scorer=fuzz.token_set_ratio)
            is_confident = bool(
                best_match
                and len(best_match) == 3
                and best_match[1] >= (80 if year_str else 86)
            )
            gated_ok = True
            if year_str and best_match and len(best_match) == 3:
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
            best_page_url_raw = best_match[2]
            if not isinstance(best_page_url_raw, str):
                logger.error(
                    f"[SCRAPER ERROR] YTS Stage 1: Matched item key was not a string URL. Got: {best_page_url_raw}"
                )
                return []

            best_page_url = urllib.parse.urljoin(base_search_url, best_page_url_raw)

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
            api_url = f"https://yts.lt/api/v2/movie_details.json?movie_id={movie_id}"
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
                    _data_field.get("movie") if isinstance(_data_field, dict) else None
                )
                torrents = (
                    movie_data.get("torrents", [])
                    if isinstance(movie_data, dict)
                    else []
                )
                conditions = []
                status = api_data.get("status") if isinstance(api_data, dict) else None
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
                    if size_gb > max_size_gb:
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
                        if seeders_count < 20:
                            continue
                        peers_count = torrent.get("peers", 0)
                        leechers_count = max(peers_count - seeders_count, 0)

                        if "peers" not in torrent:
                            logger.warning(
                                "[SCRAPER] YTS API (Stage 4) missing 'peers' field; defaulting leechers to 0"
                            )

                        parsed_codec = (
                            parse_codec(full_title) or "x264"  # Default YTS to x264
                        )
                        score = score_torrent_result(
                            full_title,
                            "YTS",
                            preferences,
                            seeders=seeders_count,
                            leechers=leechers_count,
                        )

                        results.append(
                            {
                                "title": full_title,
                                "page_url": magnet_link,
                                "info_url": best_page_url,
                                "score": score,
                                "source": "yts.lt",
                                "uploader": "YTS",
                                "size_gb": size_gb,
                                "codec": parsed_codec,
                                "seeders": seeders_count,
                                "leechers": leechers_count,
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
