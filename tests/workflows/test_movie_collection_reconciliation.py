from pathlib import Path

import pytest
from unittest.mock import AsyncMock

from telegram_bot.workflows.search_workflow.movie_collection_flow import (
    _present_collection_download_confirmation,
    _prepare_collection_directory,
    _resolve_collection_paths,
    _render_collection_movie_picker,
    finalize_movie_collection,
)
from telegram_bot.workflows.search_session import SearchSession
from telegram_bot.workflows.search_workflow.collection_reconciliation import (
    locate_collection_movie_matches,
    reconcile_collection_movie,
    select_preferred_collection_match,
)


def test_locate_collection_movie_matches_finds_root_and_collection_entries(tmp_path: Path) -> None:
    movies_root = tmp_path / "movies"
    franchise_dir = movies_root / "Saga"
    nested_dir = franchise_dir / "Movie One (2020)"
    nested_dir.mkdir(parents=True)
    (movies_root / "Movie One (2020).mkv").write_bytes(b"root")
    (nested_dir / "Movie One (2020).mkv").write_bytes(b"nested")

    matches = locate_collection_movie_matches(
        str(movies_root), str(franchise_dir), "Movie One (2020)"
    )

    assert {match.location for match in matches} == {"movies_root", "collection"}


def test_locate_collection_movie_matches_finds_nested_library_entries_outside_collection(
    tmp_path: Path,
) -> None:
    movies_root = tmp_path / "movies"
    franchise_dir = movies_root / "Saga"
    nested_library_dir = movies_root / "Archive" / "Shelf"
    nested_library_dir.mkdir(parents=True)
    franchise_dir.mkdir(parents=True)
    (nested_library_dir / "Movie One (2020).mkv").write_bytes(b"root-nested")

    matches = locate_collection_movie_matches(
        str(movies_root), str(franchise_dir), "Movie One (2020)"
    )

    assert len(matches) == 1
    assert matches[0].location == "movies_root"
    assert matches[0].path == str(nested_library_dir / "Movie One (2020).mkv")


def test_locate_collection_movie_matches_relaxes_collection_filename_conventions(
    tmp_path: Path,
) -> None:
    movies_root = tmp_path / "movies"
    franchise_dir = movies_root / "Harry Potter"
    franchise_dir.mkdir(parents=True)
    (franchise_dir / "04 - Harry Potter and the Sorcerers Stone (2001).mp4").write_bytes(b"legacy")
    (
        franchise_dir / "12 - Harry Potter 20th Anniversary Return To Hogwarts (2022).mp4"
    ).write_bytes(b"doc")

    matches = locate_collection_movie_matches(
        str(movies_root),
        str(franchise_dir),
        "Harry Potter and the Philosopher's Stone (2001)",
    )

    assert len(matches) == 1
    assert matches[0].location == "collection"
    assert matches[0].path == str(
        franchise_dir / "04 - Harry Potter and the Sorcerers Stone (2001).mp4"
    )


def test_locate_collection_movie_matches_ignores_trash_directory_entries(tmp_path: Path) -> None:
    movies_root = tmp_path / "movies"
    franchise_dir = movies_root / "Jurassic"
    franchise_dir.mkdir(parents=True)
    collection_file = franchise_dir / "06 - Jurassic World Dominion (2022).mp4"
    collection_file.write_bytes(b"video")

    trash_info_dir = movies_root / ".Trash-1000" / "info"
    trash_info_dir.mkdir(parents=True)
    (trash_info_dir / "06 - Jurassic World Dominion (2022).mp4.trashinfo").write_text("trash entry")

    matches = locate_collection_movie_matches(
        str(movies_root),
        str(franchise_dir),
        "Jurassic World Dominion (2022)",
    )

    assert len(matches) == 1
    assert matches[0].location == "collection"
    assert matches[0].path == str(collection_file)


@pytest.mark.asyncio
async def test_reconcile_collection_movie_moves_root_file_into_collection(tmp_path: Path) -> None:
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    franchise_dir = movies_root / "Saga"
    source_file = movies_root / "Movie One (2020).mkv"
    source_file.write_bytes(b"data")

    result = await reconcile_collection_movie(
        str(movies_root), str(franchise_dir), "Movie One (2020)"
    )

    assert result.status == "moved_to_collection"
    assert result.destination_path == str(franchise_dir / "Movie One (2020).mkv")
    assert not source_file.exists()
    assert (franchise_dir / "Movie One (2020).mkv").exists()


@pytest.mark.asyncio
async def test_reconcile_collection_movie_flattens_root_folder_into_collection(
    tmp_path: Path,
) -> None:
    movies_root = tmp_path / "movies"
    source_dir = movies_root / "Movie Two (2021)"
    source_dir.mkdir(parents=True)
    source_file = source_dir / "Movie Two (2021).mkv"
    source_file.write_bytes(b"data")
    franchise_dir = movies_root / "Saga"

    result = await reconcile_collection_movie(
        str(movies_root), str(franchise_dir), "Movie Two (2021)"
    )

    assert result.status == "moved_to_collection"
    assert result.destination_path == str(franchise_dir / "Movie Two (2021).mkv")
    assert not source_dir.exists()
    assert (franchise_dir / "Movie Two (2021).mkv").exists()


@pytest.mark.asyncio
async def test_reconcile_collection_movie_reports_already_in_collection(tmp_path: Path) -> None:
    movies_root = tmp_path / "movies"
    franchise_dir = movies_root / "Saga"
    franchise_dir.mkdir(parents=True)
    source_file = franchise_dir / "Movie Three (2022).mkv"
    source_file.write_bytes(b"data")

    result = await reconcile_collection_movie(
        str(movies_root), str(franchise_dir), "Movie Three (2022)"
    )

    assert result.status == "already_in_collection"
    assert result.destination_path == str(source_file)


@pytest.mark.asyncio
async def test_reconcile_collection_movie_ignores_trash_directory_entries(
    tmp_path: Path,
) -> None:
    movies_root = tmp_path / "movies"
    franchise_dir = movies_root / "Jurassic"
    franchise_dir.mkdir(parents=True)
    source_file = franchise_dir / "06 - Jurassic World Dominion (2022).mp4"
    source_file.write_bytes(b"video")

    trash_info_dir = movies_root / ".Trash-1000" / "info"
    trash_info_dir.mkdir(parents=True)
    (trash_info_dir / "06 - Jurassic World Dominion (2022).mp4.trashinfo").write_text("trash entry")

    result = await reconcile_collection_movie(
        str(movies_root),
        str(franchise_dir),
        "Jurassic World Dominion (2022)",
    )

    assert result.status == "already_in_collection"
    assert result.destination_path == str(source_file)


@pytest.mark.asyncio
async def test_reconcile_collection_movie_prefers_existing_collection_match_over_duplicate_elsewhere(
    tmp_path: Path,
) -> None:
    movies_root = tmp_path / "movies"
    franchise_dir = movies_root / "Saga"
    franchise_dir.mkdir(parents=True)
    (movies_root / "Movie Four (2023).mkv").write_bytes(b"root")
    collection_file = franchise_dir / "Movie Four (2023).mkv"
    collection_file.write_bytes(b"nested")

    result = await reconcile_collection_movie(
        str(movies_root), str(franchise_dir), "Movie Four (2023)"
    )

    assert result.status == "already_in_collection"
    assert result.destination_path == str(collection_file)


def test_select_preferred_collection_match_returns_none_for_multiple_non_collection_matches(
    tmp_path: Path,
) -> None:
    movies_root = tmp_path / "movies"
    franchise_dir = movies_root / "Saga"
    archive_dir = movies_root / "Archive"
    archive_dir.mkdir(parents=True)
    franchise_dir.mkdir(parents=True)
    root_file = movies_root / "Movie Four (2023).mkv"
    root_file.write_bytes(b"root")
    nested_file = archive_dir / "Movie Four (2023).mkv"
    nested_file.write_bytes(b"nested")

    matches = locate_collection_movie_matches(
        str(movies_root), str(franchise_dir), "Movie Four (2023)"
    )

    assert len(matches) == 2
    assert select_preferred_collection_match(matches) is None


@pytest.mark.asyncio
async def test_prepare_collection_directory_prefers_collection_match_over_duplicate_elsewhere(
    tmp_path: Path, context
) -> None:
    movies_root = tmp_path / "movies"
    franchise_dir = movies_root / "Saga"
    franchise_dir.mkdir(parents=True)
    archive_dir = movies_root / "Archive" / "Favorites"
    archive_dir.mkdir(parents=True)
    (archive_dir / "Movie One (2020).mkv").write_bytes(b"owned")
    nested_dir = franchise_dir / "Movie Two (2021)"
    nested_dir.mkdir()
    (nested_dir / "Movie Two (2021).mkv").write_bytes(b"owned")
    (movies_root / "Movie Three (2022).mkv").write_bytes(b"root")
    duplicate_dir = franchise_dir / "Movie Three (2022)"
    duplicate_dir.mkdir()
    (duplicate_dir / "Movie Three (2022).mkv").write_bytes(b"nested")

    context.bot_data["SAVE_PATHS"] = {"movies": str(movies_root), "default": str(tmp_path)}
    session = SearchSession(
        collection_name="Saga",
        collection_fs_name="Saga",
        collection_movies=[
            {"title": "Movie One", "year": 2020},
            {"title": "Movie Two", "year": 2021},
            {"title": "Movie Three", "year": 2022},
            {"title": "Movie Four", "year": 2023},
        ],
    )

    owned_count = await _prepare_collection_directory(context, session)

    assert owned_count == 3
    assert session.collection_movies[0]["owned"] is True
    assert session.collection_movies[0]["existing_location"] == "movies_root"
    assert session.collection_movies[1]["already_in_collection"] is True
    assert session.collection_movies[2]["reconciliation_status"] == "already_in_collection"
    assert session.collection_movies[2]["already_in_collection"] is True
    assert session.collection_movies[3]["reconciliation_status"] == "missing"


@pytest.mark.asyncio
async def test_prepare_collection_directory_marks_multiple_non_collection_matches_ambiguous(
    tmp_path: Path, context
) -> None:
    movies_root = tmp_path / "movies"
    franchise_dir = movies_root / "Saga"
    archive_dir = movies_root / "Archive" / "Favorites"
    franchise_dir.mkdir(parents=True)
    archive_dir.mkdir(parents=True)
    (movies_root / "Movie One (2020).mkv").write_bytes(b"root")
    (archive_dir / "Movie One (2020).mkv").write_bytes(b"archive")

    context.bot_data["SAVE_PATHS"] = {"movies": str(movies_root), "default": str(tmp_path)}
    session = SearchSession(
        collection_name="Saga",
        collection_fs_name="Saga",
        collection_movies=[
            {"title": "Movie One", "year": 2020},
        ],
    )

    owned_count = await _prepare_collection_directory(context, session)

    assert owned_count == 0
    assert session.collection_movies[0]["owned"] is False
    assert session.collection_movies[0]["reconciliation_status"] == "ambiguous"


def test_resolve_collection_paths_uses_existing_alias_directory(tmp_path: Path, context) -> None:
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    legacy_dir = movies_root / "Harry Potter Collection"
    legacy_dir.mkdir()
    context.bot_data["SAVE_PATHS"] = {"movies": str(movies_root), "default": str(tmp_path)}

    resolved_root, resolved_dir = _resolve_collection_paths(
        context,
        "Harry Potter (film series)",
        "Harry Potter",
    )

    assert resolved_root == str(movies_root)
    assert resolved_dir == str(legacy_dir)


@pytest.mark.asyncio
async def test_prepare_collection_directory_uses_existing_alias_collection_folder(
    tmp_path: Path, context
) -> None:
    movies_root = tmp_path / "movies"
    legacy_dir = movies_root / "Harry Potter Collection"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "Harry Potter and the Philosopher's Stone (2001).mkv").write_bytes(b"owned")
    (legacy_dir / "Harry Potter and the Chamber of Secrets (2002).mkv").write_bytes(b"owned")
    (movies_root / "Harry Potter and the Prisoner of Azkaban (2004).mkv").write_bytes(b"owned")

    context.bot_data["SAVE_PATHS"] = {"movies": str(movies_root), "default": str(tmp_path)}
    session = SearchSession(
        collection_name="Harry Potter (film series)",
        collection_fs_name="Harry Potter",
        collection_movies=[
            {"title": "Harry Potter and the Philosopher's Stone", "year": 2001},
            {"title": "Harry Potter and the Chamber of Secrets", "year": 2002},
            {"title": "Harry Potter and the Prisoner of Azkaban", "year": 2004},
            {"title": "Harry Potter and the Goblet of Fire", "year": 2005},
        ],
    )

    owned_count = await _prepare_collection_directory(context, session)

    assert owned_count == 3
    assert session.collection_movies[0]["reconciliation_status"] == "already_in_collection"
    assert session.collection_movies[1]["reconciliation_status"] == "already_in_collection"
    assert session.collection_movies[2]["reconciliation_status"] == "available_outside_collection"
    assert session.collection_movies[3]["reconciliation_status"] == "missing"


@pytest.mark.asyncio
async def test_prepare_collection_directory_matches_legacy_prefixed_collection_files(
    tmp_path: Path, context
) -> None:
    movies_root = tmp_path / "movies"
    franchise_dir = movies_root / "Harry Potter"
    franchise_dir.mkdir(parents=True)
    (franchise_dir / "04 - Harry Potter and the Sorcerers Stone (2001).mp4").write_bytes(b"owned")
    (franchise_dir / "05 - Harry Potter and the Chamber of Secrets (2002).mp4").write_bytes(
        b"owned"
    )
    (
        franchise_dir / "12 - Harry Potter 20th Anniversary Return To Hogwarts (2022).mp4"
    ).write_bytes(b"doc")

    context.bot_data["SAVE_PATHS"] = {"movies": str(movies_root), "default": str(tmp_path)}
    session = SearchSession(
        collection_name="Harry Potter",
        collection_fs_name="Harry Potter",
        collection_movies=[
            {"title": "Harry Potter and the Philosopher's Stone", "year": 2001},
            {"title": "Harry Potter and the Chamber of Secrets", "year": 2002},
            {"title": "Harry Potter and the Prisoner of Azkaban", "year": 2004},
        ],
    )

    owned_count = await _prepare_collection_directory(context, session)

    assert owned_count == 2
    assert session.collection_movies[0]["reconciliation_status"] == "already_in_collection"
    assert session.collection_movies[1]["reconciliation_status"] == "already_in_collection"
    assert session.collection_movies[2]["reconciliation_status"] == "missing"


@pytest.mark.asyncio
async def test_finalize_movie_collection_moves_all_owned_movies_into_collection_folder(
    tmp_path: Path, context
) -> None:
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    (movies_root / "Movie One (2020).mkv").write_bytes(b"one")
    source_dir = movies_root / "Movie Two (2021)"
    source_dir.mkdir()
    (source_dir / "Movie Two (2021).mkv").write_bytes(b"two")
    context.bot_data["SAVE_PATHS"] = {"movies": str(movies_root), "default": str(tmp_path)}

    result = await finalize_movie_collection(
        context,
        {
            "name": "Saga",
            "fs_name": "Saga",
            "movies": [
                {"title": "Movie One", "year": 2020},
                {"title": "Movie Two", "year": 2021},
            ],
        },
    )

    franchise_dir = movies_root / "Saga"
    assert result["moved_count"] == 2
    assert result["already_in_collection_count"] == 0
    assert result["missing_count"] == 0
    assert (franchise_dir / "Movie One (2020).mkv").exists()
    assert (franchise_dir / "Movie Two (2021).mkv").exists()


@pytest.mark.asyncio
async def test_collection_confirmation_reports_ambiguous_matches(
    mocker, context, make_message
) -> None:
    edit_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_edit_message",
        new=AsyncMock(),
    )
    session = SearchSession(
        collection_name="Saga",
        collection_resolution="1080p",
        collection_codec="x264",
        collection_movies=[
            {
                "title": "Movie One",
                "year": 2020,
                "reconciliation_status": "ambiguous",
            }
        ],
    )

    await _present_collection_download_confirmation(
        make_message(),
        context,
        session,
        {"items": [], "franchise": {"name": "Saga"}, "owned_summaries": []},
        [],
    )

    assert "need review and were skipped" in edit_mock.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_collection_picker_all_owned_still_allows_continue(
    mocker, context, make_message
) -> None:
    edit_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_edit_message",
        new=AsyncMock(),
    )
    session = SearchSession(
        collection_name="Saga",
        collection_movies=[
            {
                "title": "Movie One",
                "year": 2020,
                "identifier": "movieone2020-0",
                "owned": True,
                "already_in_collection": False,
                "reconciliation_status": "available_outside_collection",
            }
        ],
    )

    await _render_collection_movie_picker(make_message(), context, session)

    labels = [
        button.text
        for row in edit_mock.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "✅ Continue" in labels


@pytest.mark.asyncio
async def test_collection_picker_separates_in_collection_vs_elsewhere_counts(
    mocker, context, make_message
) -> None:
    edit_mock = mocker.patch(
        "telegram_bot.workflows.search_workflow.movie_collection_flow.safe_edit_message",
        new=AsyncMock(),
    )
    session = SearchSession(
        collection_name="Saga",
        collection_movies=[
            {
                "title": "Movie One",
                "year": 2020,
                "identifier": "movie-0",
                "owned": True,
                "already_in_collection": True,
                "reconciliation_status": "already_in_collection",
            },
            {
                "title": "Movie Two",
                "year": 2021,
                "identifier": "movie-1",
                "owned": True,
                "already_in_collection": False,
                "reconciliation_status": "available_outside_collection",
            },
        ],
    )

    await _render_collection_movie_picker(make_message(), context, session)

    text = edit_mock.await_args.kwargs["text"]
    labels = [
        button.text
        for row in edit_mock.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "Already in collection folder: 1" in text
    assert "Found elsewhere in library: 1" in text
    assert "📁 Movie One (2020)" in labels
    assert "📦 Movie Two (2021)" in labels
