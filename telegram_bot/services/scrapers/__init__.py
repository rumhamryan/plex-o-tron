# telegram_bot/services/scrapers/__init__.py

from .generic_web_scraper import (
    find_magnet_link_on_page,
    scrape_generic_page,
    scrape_yaml_site,
)
from .torrent_scraper import (
    scrape_1337x,
    YtsScraper,
)
from .wikipedia_scraper import (
    fetch_episode_title_from_wikipedia,
    fetch_movie_years_from_wikipedia,
    fetch_episode_titles_for_season,
    fetch_total_seasons_from_wikipedia,
    fetch_season_episode_count_from_wikipedia,
)

__all__ = [
    "find_magnet_link_on_page",
    "scrape_generic_page",
    "scrape_yaml_site",
    "scrape_1337x",
    "YtsScraper",
    "fetch_episode_title_from_wikipedia",
    "fetch_movie_years_from_wikipedia",
    "fetch_episode_titles_for_season",
    "fetch_total_seasons_from_wikipedia",
    "fetch_season_episode_count_from_wikipedia",
]