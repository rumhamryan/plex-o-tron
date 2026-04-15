import urllib.parse
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from telegram.ext import ContextTypes

from ....config import logger, require_scraper_max_torrent_size_gib
from ....utils import (
    compute_av_match_metadata,
    parse_codec,
    parse_torrent_name,
    score_torrent_result,
)
from ...generic_torrent_scraper import GenericTorrentScraper, load_site_config


def _find_site_config_by_name(site_name: str) -> Path | None:
    """Locate a YAML config whose 'site_name' matches the given name.

    This scans telegram_bot/scrapers/configs for .yaml files and reads only the
    'site_name' field to match quickly.
    """
    # Fix path from services/scrapers/sites/yaml.py -> scrapers/configs
    base = Path(__file__).resolve().parent.parent.parent.parent / "scrapers" / "configs"
    if not base.exists():
        return None
    for path in base.glob("*.yaml"):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict) and data.get("site_name") == site_name:
                return path
        except Exception:
            continue
    return None


async def scrape_yaml_site(
    query: str,
    media_type: str,
    _search_url_template: str,  # unused; present for call compatibility
    context: ContextTypes.DEFAULT_TYPE,
    *,
    site_name: str,
    limit: int = 15,
    base_query_for_filter: str | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Generic path that uses a YAML config (GenericTorrentScraper).

    It looks up a YAML file whose 'site_name' matches and delegates scraping to
    GenericTorrentScraper. Results are mapped to the unified result schema.
    """
    prefs_key = "movies" if "movie" in media_type else "tv"
    preferences = (
        context.bot_data.get("SEARCH_CONFIG", {}).get("preferences", {}).get(prefs_key, {})
    )
    if not preferences:
        return []

    config_path = _find_site_config_by_name(site_name)
    if not isinstance(config_path, Path):
        logger.warning("[SCRAPER] No YAML config found for site '%s' — skipping.", site_name)
        return []

    try:
        site_config = load_site_config(config_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("[SCRAPER] Failed to load YAML config for %s: %s", site_name, exc)
        return []

    scraper = GenericTorrentScraper(site_config)
    raw_results = await scraper.search(
        query, media_type, base_query_for_filter=base_query_for_filter, limit=limit
    )

    raw_max_size_gib = kwargs.get("max_size_gib", kwargs.get("max_size_gb"))
    if raw_max_size_gib is None:
        max_size_gib = require_scraper_max_torrent_size_gib(context.bot_data)
    else:
        try:
            max_size_gib = float(raw_max_size_gib)
        except (TypeError, ValueError):
            logger.error(
                "[SCRAPER] %s: Invalid max_size_gib override: %r", site_name, raw_max_size_gib
            )
            return []
        if max_size_gib <= 0:
            logger.error(
                "[SCRAPER] %s: max_size_gib must be greater than 0. Got %s",
                site_name,
                max_size_gib,
            )
            return []

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
                "is_bronze_av": av_metadata["is_bronze_av"],
                "has_video_match": av_metadata["has_video_match"],
                "has_audio_match": av_metadata["has_audio_match"],
            }
        )

    logger.info(
        "[SCRAPER] %s: Found %d torrents for query '%s' from %s.",
        site_name,
        len(results),
        query,
        scraper.base_url,
    )
    return results
