# telegram_bot/services/scraping_service.py

from .scrapers.torrent_scraper import (
    scrape_1337x,
    scrape_yts,
    YtsScraper,
)
from .scrapers.wikipedia_scraper import (
    fetch_episode_title_from_wikipedia,
    fetch_movie_years_from_wikipedia,
    fetch_episode_titles_for_season,
    fetch_total_seasons_from_wikipedia,
    fetch_season_episode_count_from_wikipedia,
    _get_page_html,
)
from .scrapers.generic_web_scraper import (
    find_magnet_link_on_page,
    scrape_generic_page,
    scrape_yaml_site,
)
from .generic_torrent_scraper import GenericTorrentScraper

__all__ = [
    "fetch_episode_title_from_wikipedia",
    "fetch_movie_years_from_wikipedia",
    "fetch_episode_titles_for_season",
    "fetch_total_seasons_from_wikipedia",
    "fetch_season_episode_count_from_wikipedia",
    "scrape_1337x",
    "scrape_yts",
    "YtsScraper",
    "find_magnet_link_on_page",
    "scrape_generic_page",
    "scrape_yaml_site",
    "GenericTorrentScraper",
    "_get_page_html",
]
