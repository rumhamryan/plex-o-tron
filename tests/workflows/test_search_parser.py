from telegram_bot.workflows.search_parser import parse_search_query


def test_parse_query_with_sxxeyy_token():
    result = parse_search_query("The Bear S02E05")
    assert result.title == "The Bear"
    assert result.season == 2
    assert result.episode == 5
    assert result.year is None


def test_parse_query_with_season_and_episode_words():
    result = parse_search_query("severance season 1 episode 3")
    assert result.title == "severance"
    assert result.season == 1
    assert result.episode == 3


def test_parse_query_with_season_only():
    result = parse_search_query("Fargo Season 5 trailer")
    assert result.title == "Fargo"
    assert result.season == 5
    assert result.episode is None


def test_parse_query_with_episode_only():
    result = parse_search_query("Episode 4 The Office")
    assert result.title == "The Office"
    assert result.season is None
    assert result.episode == 4


def test_parse_query_detects_trailing_year():
    result = parse_search_query("Dune 2021")
    assert result.title == "Dune"
    assert result.year == "2021"
