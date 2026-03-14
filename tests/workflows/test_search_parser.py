from telegram_bot.workflows.search_parser import parse_search_query


def test_parse_movie_query_extracts_year_resolution_and_codec():
    parsed = parse_search_query("Inception 2010 1080p HEVC")

    assert parsed.title == "Inception"
    assert parsed.year == "2010"
    assert parsed.resolution == "1080p"
    assert parsed.codec == "x265"


def test_parse_tv_query_extracts_episode_resolution_and_codec():
    parsed = parse_search_query("The Last of Us S01E02 720p h264")

    assert parsed.title == "The Last of Us"
    assert parsed.season == 1
    assert parsed.episode == 2
    assert parsed.resolution == "720p"
    assert parsed.codec == "x264"
