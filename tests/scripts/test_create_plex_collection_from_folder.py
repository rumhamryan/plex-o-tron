from __future__ import annotations

from pathlib import Path

import pytest

from scripts.create_plex_collection_from_folder import (
    build_collection_movies_from_folder,
    resolve_collection_name,
)


def test_resolve_collection_name_uses_sanitized_override_and_folder_default(tmp_path: Path) -> None:
    folder = tmp_path / "The Matrix (Franchise)"
    folder.mkdir()

    from_override = resolve_collection_name(str(folder), "Matrix Collection")
    from_folder = resolve_collection_name(str(folder), None)

    assert from_override == "Matrix"
    assert from_folder == "The Matrix"


def test_build_collection_movies_from_folder_non_recursive(tmp_path: Path) -> None:
    folder = tmp_path / "mission_impossible"
    folder.mkdir()

    (folder / "Mission Impossible (1996).mkv").write_text("x", encoding="utf-8")
    (folder / "Mission Impossible 2 (2000).mp4").write_text("x", encoding="utf-8")
    (folder / "Show.Name.S01E01.1080p.mkv").write_text("x", encoding="utf-8")
    (folder / ".hidden_movie (2025).mkv").write_text("x", encoding="utf-8")
    (folder / "notes.txt").write_text("x", encoding="utf-8")
    (folder / "03 - Mission Impossible III (2006)").mkdir()

    movies, warnings = build_collection_movies_from_folder(str(folder), recursive=False)

    titles = {movie["title"] for movie in movies}
    years = {(movie["title"], movie["year"]) for movie in movies}

    assert titles == {
        "Mission Impossible",
        "Mission Impossible 2",
        "Mission Impossible III",
    }
    assert years == {
        ("Mission Impossible", 1996),
        ("Mission Impossible 2", 2000),
        ("Mission Impossible III", 2006),
    }
    assert any("Show.Name.S01E01.1080p.mkv" in warning for warning in warnings)


def test_build_collection_movies_from_folder_recursive_dedupes_movies(tmp_path: Path) -> None:
    folder = tmp_path / "collection"
    folder.mkdir()
    nested = folder / "nested"
    nested.mkdir()

    (folder / "Movie One (2020).mkv").write_text("x", encoding="utf-8")
    (nested / "Movie One (2020).mp4").write_text("x", encoding="utf-8")
    (nested / "Movie Two (2021)").mkdir()

    movies, warnings = build_collection_movies_from_folder(str(folder), recursive=True)

    labels = {(movie["title"], movie["year"]) for movie in movies}
    assert labels == {("Movie One", 2020), ("Movie Two", 2021)}
    assert any("Skipped duplicate movie candidate" in warning for warning in warnings)


def test_build_collection_movies_from_folder_rejects_invalid_paths(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    file_path = tmp_path / "not_a_directory.txt"
    file_path.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="Folder does not exist"):
        build_collection_movies_from_folder(str(missing), recursive=False)

    with pytest.raises(ValueError, match="Path is not a directory"):
        build_collection_movies_from_folder(str(file_path), recursive=False)
