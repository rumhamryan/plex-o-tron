from pathlib import Path
from typing import Any
from telegram.ext import ContextTypes

from ...config import logger
from ..search_logic import _parse_codec, score_torrent_result
from ...utils import parse_torrent_name
from ..generic_torrent_scraper import GenericTorrentScraper, load_site_config


async def scrape_1337x(
    query: str,
    media_type: str,
    search_url_template: str,
    context: ContextTypes.DEFAULT_TYPE,
    limit: int = 15,
    **kwargs,
) -> list[dict[str, Any]]:
    """Scrape 1337x using the generic scraper framework."""

    prefs_key = "movies" if "movie" in media_type else "tv"
    preferences = (
        context.bot_data.get("SEARCH_CONFIG", {})
        .get("preferences", {})
        .get(prefs_key, {})
    )
    if not preferences:
        logger.warning(
            f"[SCRAPER] No preferences found for '{prefs_key}'. Cannot score 1337x results."
        )
        return []

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

    scraper = GenericTorrentScraper(site_config)
    base_filter = kwargs.get("base_query_for_filter")
    raw_results = await scraper.search(
        query, media_type, base_query_for_filter=base_filter, limit=limit
    )

    results: list[dict[str, Any]] = []
    for item in raw_results:
        score = score_torrent_result(
            item.name, item.uploader or "", preferences, seeders=item.seeders
        )
        if score <= 0:
            continue
        parsed_name = parse_torrent_name(item.name)
        results.append(
            {
                "title": item.name,
                "page_url": item.magnet_url,
                "score": score,
                "source": item.source_site,
                "uploader": item.uploader or "Anonymous",
                "size_gb": item.size_bytes / (1024**3),
                "codec": _parse_codec(item.name),
                "seeders": item.seeders,
                "leechers": item.leechers,
                "year": parsed_name.get("year"),
            }
        )

    logger.info(f"[SCRAPER] 1337x: Found {len(results)} torrents for query '{query}'.")
    return results
