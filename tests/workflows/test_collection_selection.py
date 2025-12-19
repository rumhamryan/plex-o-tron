import pytest
from telegram_bot.workflows.search_workflow import _pick_collection_candidate


@pytest.fixture
def mock_results():
    return [
        {
            "title": "Movie 2160p x265",
            "codec": "x265",
            "score": 10,
            "seeders": 100,
            "size_gb": 10.0,
            "source": "tpb",
            "page_url": "magnet:1",
        },
        {
            "title": "Movie 2160p x264",
            "codec": "x264",
            "score": 10,
            "seeders": 100,
            "size_gb": 10.0,
            "source": "tpb",
            "page_url": "magnet:2",
        },
        {
            "title": "Movie 1080p x265",
            "codec": "x265",
            "score": 10,
            "seeders": 100,
            "size_gb": 2.0,
            "source": "tpb",
            "page_url": "magnet:3",
        },
        {
            "title": "Movie 1080p x264",
            "codec": "x264",
            "score": 10,
            "seeders": 100,
            "size_gb": 2.0,
            "source": "tpb",
            "page_url": "magnet:4",
        },
    ]


def test_pick_collection_candidate_strict_match(mock_results):
    # Tier 1: 2160p + x265
    candidate = _pick_collection_candidate(mock_results, "2160p", "x265", None, None)
    assert candidate["page_url"] == "magnet:1"


def test_pick_collection_candidate_fallback_codec(mock_results):
    # Tier 2: 2160p + x264 (requested x265, but only x264 exists in 2160p)
    results = [r for r in mock_results if r["page_url"] != "magnet:1"]
    candidate = _pick_collection_candidate(results, "2160p", "x265", None, None)
    assert candidate["page_url"] == "magnet:2"


def test_pick_collection_candidate_fallback_resolution(mock_results):
    # Tier 3: 1080p + x265 (requested 2160p + x265, but only 1080p exists)
    results = [r for r in mock_results if "2160p" not in r["title"]]
    candidate = _pick_collection_candidate(results, "2160p", "x265", None, None)
    assert candidate["page_url"] == "magnet:3"


def test_pick_collection_candidate_fallback_both(mock_results):
    # Tier 4: 1080p + x264 (requested 2160p + x265, but only 1080p x264 exists)
    results = [mock_results[3]]  # Only 1080p x264
    candidate = _pick_collection_candidate(results, "2160p", "x265", None, None)
    assert candidate["page_url"] == "magnet:4"


def test_pick_collection_candidate_uploader_consistency(mock_results):
    # Within a tier, uploader consistency should win
    results = [
        {
            "title": "Movie 2160p x265 A",
            "codec": "x265",
            "score": 10,
            "uploader": "ConsistentGroup",
            "page_url": "magnet:match",
        },
        {
            "title": "Movie 2160p x265 B",
            "codec": "x265",
            "score": 20,  # Higher base score
            "uploader": "Other",
            "page_url": "magnet:no-match",
        },
    ]
    candidate = _pick_collection_candidate(
        results, "2160p", "x265", None, "ConsistentGroup"
    )
    assert candidate["page_url"] == "magnet:match"


def test_pick_collection_candidate_ignores_rm4k_for_4k_request():
    # RM4K is 1080p, should not match a 2160p request
    results = [
        {
            "title": "Movie (1995) RM4K (1080p x265)",
            "codec": "x265",
            "score": 100,
            "page_url": "magnet:rm4k",
        },
        {
            "title": "Movie (1995) [2160p x264]",
            "codec": "x264",
            "score": 10,
            "page_url": "magnet:real4k",
        },
    ]
    # Requesting 2160p + x265.
    # Tier 1 (2160p + x265) -> Empty
    # Tier 2 (2160p + x264) -> Should match magnet:real4k
    candidate = _pick_collection_candidate(results, "2160p", "x265", None, None)
    assert candidate["page_url"] == "magnet:real4k"
