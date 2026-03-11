from typing import Any

from bs4 import BeautifulSoup

_WIKI_TITLES_CACHE: dict[tuple[str, int], tuple[dict[int, dict[str, Any]], str | None]] = {}
_WIKI_SOUP_CACHE: dict[str, BeautifulSoup] = {}
_WIKI_MOVIE_CACHE: dict[str, tuple[list[int], str | None]] = {}
_WIKI_FRANCHISE_CACHE: dict[str, tuple[str, list[dict[str, Any]]]] = {}
