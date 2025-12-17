import pytest

from telegram_bot.services import download_manager


class DummyApp:
    def __init__(self):
        self.bot_data = {
            "DOWNLOAD_BATCHES": {},
            "PLEX_CONFIG": None,
        }


@pytest.mark.asyncio
async def test_update_batch_triggers_single_scan(monkeypatch):
    app = DummyApp()
    batch_id = "season-123-456"
    app.bot_data["DOWNLOAD_BATCHES"][batch_id] = {
        "total": 2,
        "done": 1,
        "media_type": "tv",
        "scanned": False,
        "summaries": [],
    }

    # Monkeypatch scan to observe invocation
    calls = {"count": 0}

    async def fake_scan(media_type, cfg):
        calls["count"] += 1
        return "\n\nPlex scan started"

    monkeypatch.setattr(download_manager, "_trigger_plex_scan", fake_scan)

    source_dict = {"batch_id": batch_id}
    parsed_info = {"title": "Rick and Morty", "season": 1}

    msg = await download_manager._update_batch_and_maybe_scan(
        app, source_dict, "Done", parsed_info
    )

    # Scan called exactly once, scanned flag set
    assert calls["count"] == 1
    assert app.bot_data["DOWNLOAD_BATCHES"][batch_id]["scanned"] is True
    # Message contains batch-complete information and scan line
    assert "Batch Complete" in msg
    assert "Plex scan" in msg


@pytest.mark.asyncio
async def test_update_batch_no_scan_before_completion(monkeypatch):
    app = DummyApp()
    batch_id = "season-234-567"
    app.bot_data["DOWNLOAD_BATCHES"][batch_id] = {
        "total": 3,
        "done": 1,
        "media_type": "tv",
        "scanned": False,
        "summaries": [],
    }

    async def fake_scan(media_type, cfg):  # Should not be called
        raise AssertionError("Scan should not be triggered before completion")

    monkeypatch.setattr(download_manager, "_trigger_plex_scan", fake_scan)

    source_dict = {"batch_id": batch_id}
    parsed_info = {"title": "Some Show", "season": 2}

    msg = await download_manager._update_batch_and_maybe_scan(
        app, source_dict, "Done", parsed_info
    )

    assert app.bot_data["DOWNLOAD_BATCHES"][batch_id]["done"] == 2
    assert app.bot_data["DOWNLOAD_BATCHES"][batch_id]["scanned"] is False
    assert msg == "Done"


@pytest.mark.asyncio
async def test_update_batch_skip_duplicate_scan(monkeypatch):
    app = DummyApp()
    batch_id = "season-345-678"
    app.bot_data["DOWNLOAD_BATCHES"][batch_id] = {
        "total": 1,
        "done": 1,
        "media_type": "tv",
        "scanned": True,
        "summaries": [],
    }


@pytest.mark.asyncio
async def test_update_batch_collection_includes_all_summaries(monkeypatch):
    app = DummyApp()
    batch_id = "collection-999"
    app.bot_data["DOWNLOAD_BATCHES"][batch_id] = {
        "total": 2,
        "done": 0,
        "media_type": "movie",
        "scanned": False,
        "collection": {"name": "Saga", "movies": []},
        "summaries": [],
    }

    async def fake_scan(media_type, cfg):
        return ""

    async def fake_ensure(plex_config, name, movies):
        return []

    monkeypatch.setattr(download_manager, "_trigger_plex_scan", fake_scan)
    monkeypatch.setattr(
        download_manager, "ensure_collection_contains_movies", fake_ensure
    )

    source_dict = {"batch_id": batch_id}
    parsed_info = {"title": "Movie One"}

    interim = await download_manager._update_batch_and_maybe_scan(
        app, source_dict, "First summary", parsed_info
    )
    assert interim == "First summary"

    final = await download_manager._update_batch_and_maybe_scan(
        app, source_dict, "Second summary", parsed_info
    )
    assert "First summary" in final
    assert "Second summary" in final
    assert "Collection Complete" in final

    async def fake_scan(media_type, cfg):  # Should not be called
        raise AssertionError("Duplicate scan should be skipped")

    monkeypatch.setattr(download_manager, "_trigger_plex_scan", fake_scan)

    source_dict = {"batch_id": batch_id}
    parsed_info = {"title": "Show", "season": 1}

    msg = await download_manager._update_batch_and_maybe_scan(
        app, source_dict, "Done", parsed_info
    )

    assert msg == "Done"
    assert app.bot_data["DOWNLOAD_BATCHES"][batch_id]["scanned"] is True
