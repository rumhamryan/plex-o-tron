from pathlib import Path
from typing import Any
import urllib.parse
import yaml  # type: ignore[import-untyped]
from telegram.ext import ContextTypes

from ...config import logger, MAX_TORRENT_SIZE_GB
from ...utils import parse_codec, score_torrent_result, parse_torrent_name
from ..generic_torrent_scraper import GenericTorrentScraper, load_site_config


def _find_site_config_by_name(site_name: str) -> Path | None:
    """Locate a YAML config whose 'site_name' matches the given name.

    This scans telegram_bot/scrapers/configs for .yaml files and reads only the
    'site_name' field to match quickly.
    """
    # Fix path from services/scrapers/yaml.py -> scrapers/configs
    base = Path(__file__).resolve().parent.parent.parent / "scrapers" / "configs"
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
        context.bot_data.get("SEARCH_CONFIG", {})
        .get("preferences", {})
        .get(prefs_key, {})
    )
    if not preferences:
        return []

    config_path = _find_site_config_by_name(site_name)
    if not isinstance(config_path, Path):
        logger.warning(
            "[SCRAPER] No YAML config found for site '%s' — skipping.", site_name
        )
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

    max_size_gb = kwargs.get("max_size_gb", MAX_TORRENT_SIZE_GB)

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

        size_gb = item.size_bytes / (1024**3)
        if size_gb > max_size_gb:
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
                "size_gb": item.size_bytes / (1024**3),
                "codec": parse_codec(item.name),
                "seeders": item.seeders,
                "leechers": item.leechers,
                "year": parsed_name.get("year"),
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
