import sys
from pathlib import Path
from bs4 import BeautifulSoup
from telegram_bot.services.scrapers.generic_web_scraper import (
    _strategy_find_direct_links,
    _strategy_contextual_search,
    _strategy_find_in_tables,
    _score_candidate_links,
)

sys.path.append(str(Path(__file__).resolve().parent.parent.parent.parent))


def test_strategy_find_direct_links_magnet():
    html = '<a href="magnet:?xt=urn:btih:123">Magnet</a>'
    soup = BeautifulSoup(html, "lxml")
    links = _strategy_find_direct_links(soup)
    assert links == {"magnet:?xt=urn:btih:123"}


def test_strategy_find_direct_links_torrent():
    html = '<a href="https://example.com/file.torrent">Download</a>'
    soup = BeautifulSoup(html, "lxml")
    links = _strategy_find_direct_links(soup)
    assert links == {"https://example.com/file.torrent"}


def test_strategy_find_direct_links_none():
    html = '<a href="/other">Link</a>'
    soup = BeautifulSoup(html, "lxml")
    links = _strategy_find_direct_links(soup)
    assert links == set()


def test_strategy_contextual_search_keyword():
    html = '<a href="/download/123">Download Torrent</a>'
    soup = BeautifulSoup(html, "lxml")
    links = _strategy_contextual_search(soup, "Query")
    assert "/download/123" in links


def test_strategy_contextual_search_query_match():
    html = '<a href="/details.php?id=456">My Show S01E01 1080p</a>'
    soup = BeautifulSoup(html, "lxml")
    links = _strategy_contextual_search(soup, "My Show")
    assert "/details.php?id=456" in links


def test_strategy_contextual_search_unrelated_keyword():
    html = '<a href="/about">About our download policy</a>'
    soup = BeautifulSoup(html, "lxml")
    links = _strategy_contextual_search(soup, "My Show")
    assert "/about" in links


def test_strategy_find_in_tables_single_match():
    html = '<table><tr><td>My Show</td><td><a href="/dl">Download</a></td></tr></table>'
    soup = BeautifulSoup(html, "lxml")
    results = _strategy_find_in_tables(soup, "My Show")
    assert "/dl" in results


def test_strategy_find_in_tables_multiple_matches():
    html = """
    <table>
      <tr><td>My Show S01E01</td><td><a href="/e1">DL</a></td></tr>
      <tr><td>My Show S01E02</td><td><a href="/e2">DL</a></td></tr>
    </table>
    """
    soup = BeautifulSoup(html, "lxml")
    results = _strategy_find_in_tables(soup, "My Show")
    assert {"/e1", "/e2"}.issubset(results.keys())


def test_strategy_find_in_tables_ignores_unrelated_tables():
    html = """
    <table><tr><td>Other</td><td><a href="/x">X</a></td></tr></table>
    <table><tr><td>My Show</td><td><a href="/dl">Download</a></td></tr></table>
    """
    soup = BeautifulSoup(html, "lxml")
    results = _strategy_find_in_tables(soup, "My Show")
    assert "/dl" in results and "/x" not in results


def test_score_candidate_links_prefers_magnet():
    html = (
        '<div><a href="magnet:?xt=urn:btih:1">Magnet</a></div>'
        '<div><a href="/context">Download Torrent</a></div>'
        '<table><tr><td>My Show</td><td><a href="/table">Link</a></td></tr></table>'
    )
    soup = BeautifulSoup(html, "lxml")
    links = {"magnet:?xt=urn:btih:1", "/context", "/table"}
    table_links = {"/table": 80.0}
    best = _score_candidate_links(links, "My Show", table_links, soup)
    assert best == "magnet:?xt=urn:btih:1"


def test_score_candidate_links_penalizes_ads():
    html = (
        '<div class="ad"><a href="/bad">My Show 1080p</a></div>'
        '<div><a href="/good">My Show 1080p</a></div>'
    )
    soup = BeautifulSoup(html, "lxml")
    links = {"/bad", "/good"}
    best = _score_candidate_links(links, "My Show", {}, soup)
    assert best == "/good"


def test_score_candidate_links_prefers_better_match():
    html = (
        '<div><a href="/high">My Show Episode</a></div>'
        '<div><a href="/low">Another Show</a></div>'
    )
    soup = BeautifulSoup(html, "lxml")
    links = {"/high", "/low"}
    best = _score_candidate_links(links, "My Show Episode", {}, soup)
    assert best == "/high"
