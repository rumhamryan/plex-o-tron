from telegram_bot.services.tracking.selection import (
    resolve_top_resolution_tiers,
    select_best_auto_download_candidate,
)


def test_resolve_top_resolution_tiers_for_movie_and_tv():
    search_config = {
        "preferences": {
            "movies": {"resolutions": {"2160p": 5, "1080p": 3}},
            "tv": {"resolutions": {"1080p": 4, "720p": 2}},
        }
    }
    assert resolve_top_resolution_tiers(search_config, media_type="movie") == {"2160p"}
    assert resolve_top_resolution_tiers(search_config, media_type="tv") == {"1080p"}


def test_select_best_auto_download_candidate_uses_media_type_specific_tiers():
    results = [
        {"title": "Show S01E02 720p WEB", "score": 200, "page_url": "magnet:?xt=urn:btih:720"},
        {"title": "Show S01E02 1080p WEB", "score": 150, "page_url": "magnet:?xt=urn:btih:1080"},
    ]
    search_config = {"preferences": {"tv": {"resolutions": {"1080p": 5, "720p": 1}}}}

    selected = select_best_auto_download_candidate(
        results,
        search_config=search_config,
        media_type="tv",
    )

    assert selected is not None
    assert selected["page_url"] == "magnet:?xt=urn:btih:1080"
