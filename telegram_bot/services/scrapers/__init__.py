from .wikipedia import (
    fetch_episode_title_from_wikipedia,
    fetch_movie_years_from_wikipedia,
    fetch_episode_titles_for_season,
    fetch_total_seasons_from_wikipedia,
    fetch_season_episode_count_from_wikipedia,
    _WIKI_TITLES_CACHE,
    _WIKI_SOUP_CACHE,
    _WIKI_MOVIE_CACHE,
)
from .yts import scrape_yts
from .one_three_three_seven_x import scrape_1337x
from .generic import (
    find_magnet_link_on_page,
    scrape_generic_page,
    _strategy_find_direct_links,
    _strategy_contextual_search,
    _strategy_find_in_tables,
    _score_candidate_links,
)
from .yaml import scrape_yaml_site
from .tpb import scrape_tpb
from .utils import _get_page_html

__all__ = [
    "fetch_episode_title_from_wikipedia",
    "fetch_movie_years_from_wikipedia",
    "fetch_episode_titles_for_season",
    "fetch_total_seasons_from_wikipedia",
    "fetch_season_episode_count_from_wikipedia",
    "_WIKI_TITLES_CACHE",
    "_WIKI_SOUP_CACHE",
    "_WIKI_MOVIE_CACHE",
    "scrape_yts",
    "scrape_1337x",
    "find_magnet_link_on_page",
    "scrape_generic_page",
    "_strategy_find_direct_links",
    "_strategy_contextual_search",
    "_strategy_find_in_tables",
    "_score_candidate_links",
    "scrape_yaml_site",
    "scrape_tpb",
    "_get_page_html",
]
