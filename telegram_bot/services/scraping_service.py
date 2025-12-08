# telegram_bot/services/scraping_service.py

from .scrapers import (
    fetch_episode_title_from_wikipedia,
    fetch_movie_years_from_wikipedia,
    fetch_episode_titles_for_season,
    fetch_total_seasons_from_wikipedia,
    fetch_season_episode_count_from_wikipedia,
    scrape_yts,
    scrape_1337x,
    find_magnet_link_on_page,
    scrape_generic_page,
    scrape_yaml_site,
    _get_page_html,
    _strategy_find_direct_links,
    _strategy_contextual_search,
    _strategy_find_in_tables,
    _score_candidate_links,
    _WIKI_TITLES_CACHE,
    _WIKI_SOUP_CACHE,
    _WIKI_MOVIE_CACHE,
)

from .generic_torrent_scraper import GenericTorrentScraper, load_site_config

__all__ = [
    "fetch_episode_title_from_wikipedia",
    "fetch_movie_years_from_wikipedia",
    "fetch_episode_titles_for_season",
    "fetch_total_seasons_from_wikipedia",
    "fetch_season_episode_count_from_wikipedia",
    "scrape_yts",
    "scrape_1337x",
    "find_magnet_link_on_page",
    "scrape_generic_page",
    "scrape_yaml_site",
    "_get_page_html",
    "_strategy_find_direct_links",
    "_strategy_contextual_search",
    "_strategy_find_in_tables",
    "_score_candidate_links",
    "_WIKI_TITLES_CACHE",
    "_WIKI_SOUP_CACHE",
    "_WIKI_MOVIE_CACHE",
    "GenericTorrentScraper",
    "load_site_config",
]
