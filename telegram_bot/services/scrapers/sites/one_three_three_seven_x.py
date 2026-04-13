import urllib.parse
from pathlib import Path
from typing import Any

from telegram.ext import ContextTypes

from ....config import MAX_TORRENT_SIZE_GIB, logger
from ....utils import (
    compute_av_match_metadata,
    parse_codec,
    parse_torrent_name,
    score_torrent_result,
)
from ...generic_torrent_scraper import GenericTorrentScraper, load_site_config


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
        context.bot_data.get("SEARCH_CONFIG", {}).get("preferences", {}).get(prefs_key, {})
    )
    if not preferences:
        logger.warning(
            f"[SCRAPER] No preferences found for '{prefs_key}'. Cannot score 1337x results."
        )
        return []

    try:
        config_path = (
            Path(__file__).resolve().parent.parent.parent.parent
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

    max_size_gib = kwargs.get("max_size_gib", kwargs.get("max_size_gb", MAX_TORRENT_SIZE_GIB))

    results: list[dict[str, Any]] = []
    for item in raw_results:
        if item.seeders < 20:
            continue
        score = score_torrent_result(
            item.name,
            item.uploader or "",
            preferences,
            seeders=item.seeders,
            leechers=item.leechers,
        )
        if score < 6:
            continue
        av_metadata = compute_av_match_metadata(item.name, preferences)

        size_gib = item.size_bytes / (1024**3)
        if size_gib > max_size_gib:
            continue

        parsed_name = parse_torrent_name(item.name)

        info_url = None
        if item.details_link:
            info_url = urllib.parse.urljoin(scraper.base_url, item.details_link)

        results.append(
            {
                "title": item.name,
                "page_url": item.magnet_url,
                "info_url": info_url,
                "score": score,
                "source": item.source_site,
                "uploader": item.uploader or "Anonymous",
                "size_gib": size_gib,
                "codec": parse_codec(item.name),
                "seeders": item.seeders,
                "leechers": item.leechers,
                "year": parsed_name.get("year"),
                "matched_video_formats": av_metadata["matched_video_formats"],
                "matched_audio_formats": av_metadata["matched_audio_formats"],
                "matched_audio_channels": av_metadata["matched_audio_channels"],
                "is_gold_av": av_metadata["is_gold_av"],
                "is_silver_av": av_metadata["is_silver_av"],
                "has_video_match": av_metadata["has_video_match"],
                "has_audio_match": av_metadata["has_audio_match"],
            }
        )

    logger.info(
        f"[SCRAPER] 1337x: Found {len(results)} torrents for query '{query}' from {scraper.base_url}."
    )
    return results
