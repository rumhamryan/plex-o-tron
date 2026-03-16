from __future__ import annotations

import asyncio
import difflib
import os
import re
import shutil
from dataclasses import dataclass
from typing import Literal, TypedDict, cast

from ...config import logger
from .helpers import _normalize_label

CollectionMovieMatchLocation = Literal["movies_root", "collection"]
CollectionMovieMatchKind = Literal["file", "directory"]
CollectionMovieStatus = Literal[
    "missing",
    "already_in_collection",
    "moved_to_collection",
    "ambiguous",
    "conflict",
    "error",
]


@dataclass(frozen=True)
class CollectionMovieMatch:
    label: str
    path: str
    location: CollectionMovieMatchLocation
    kind: CollectionMovieMatchKind


@dataclass(frozen=True)
class CollectionMovieResolution:
    label: str
    status: CollectionMovieStatus
    source_path: str | None = None
    destination_path: str | None = None
    detail: str | None = None
    match: CollectionMovieMatch | None = None


class CollectionMoveResult(TypedDict, total=False):
    status: Literal["already_in_collection", "moved_to_collection", "conflict"]
    destination_path: str
    detail: str


_LABEL_YEAR_PATTERN = re.compile(r"\((?P<year>(?:18|19|20|21)\d{2})\)\s*$")
_ENTRY_YEAR_PATTERN = re.compile(r"(?<!\d)(?P<year>(?:18|19|20|21)\d{2})(?!\d)")
_LEADING_ORDER_PREFIX_PATTERN = re.compile(r"^\s*\d+\s*[-._:]\s*")
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_TITLE_STOPWORDS = {"a", "an", "and", "the", "part"}


def locate_collection_movie_matches(
    movies_root: str, franchise_dir: str, label: str
) -> list[CollectionMovieMatch]:
    """Locate plausible filesystem matches for a collection movie label."""
    matches: list[CollectionMovieMatch] = []
    seen_paths: set[str] = set()

    def _add_match(path: str, location: CollectionMovieMatchLocation) -> None:
        normalized_path = os.path.abspath(path)
        if normalized_path in seen_paths:
            return
        seen_paths.add(normalized_path)
        match_kind: CollectionMovieMatchKind = "directory" if os.path.isdir(path) else "file"
        matches.append(
            CollectionMovieMatch(
                label=label,
                path=path,
                location=location,
                kind=match_kind,
            )
        )

    for path in _find_label_matches(
        movies_root,
        label,
        recursive=True,
        excluded_roots=(franchise_dir,),
    ):
        if os.path.abspath(path) == os.path.abspath(franchise_dir):
            continue
        _add_match(path, "movies_root")

    for path in _find_label_matches(franchise_dir, label, recursive=True):
        _add_match(path, "collection")

    return matches


def select_preferred_collection_match(
    matches: list[CollectionMovieMatch],
) -> CollectionMovieMatch | None:
    """
    Prefer a single collection-folder match over duplicates elsewhere.

    Ambiguous remains only when there is no clear winning collection match:
    - multiple non-collection matches
    - multiple collection matches
    - mixed matches without exactly one collection-folder candidate
    """
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    collection_matches = [match for match in matches if match.location == "collection"]
    if len(collection_matches) == 1:
        return collection_matches[0]

    return None


async def reconcile_collection_movie(
    movies_root: str, franchise_dir: str, label: str
) -> CollectionMovieResolution:
    """Ensure a movie lives inside the collection directory and report the outcome."""
    try:
        matches = await asyncio.to_thread(
            locate_collection_movie_matches, movies_root, franchise_dir, label
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[COLLECTION] Failed locating '%s': %s", label, exc)
        return CollectionMovieResolution(label=label, status="error", detail=str(exc))

    if not matches:
        return CollectionMovieResolution(label=label, status="missing")
    match = select_preferred_collection_match(matches)
    if match is None:
        detail = ", ".join(match.path for match in matches)
        return CollectionMovieResolution(label=label, status="ambiguous", detail=detail)
    try:
        move_result = await _move_match_into_collection(match, franchise_dir)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[COLLECTION] Failed reconciling '%s': %s", label, exc)
        return CollectionMovieResolution(
            label=label,
            status="error",
            source_path=match.path,
            detail=str(exc),
            match=match,
        )

    return CollectionMovieResolution(
        label=label,
        status=cast(CollectionMovieStatus, move_result["status"]),
        source_path=match.path,
        destination_path=move_result.get("destination_path"),
        detail=move_result.get("detail"),
        match=match,
    )


def _find_label_matches(
    root_path: str,
    label: str,
    *,
    recursive: bool,
    excluded_roots: tuple[str, ...] = (),
) -> list[str]:
    if not root_path or not os.path.isdir(root_path):
        return []

    normalized_label = _normalize_label(label)
    if not normalized_label:
        return []

    excluded_paths = {
        os.path.abspath(candidate)
        for candidate in excluded_roots
        if candidate and os.path.exists(candidate)
    }
    root_abs = os.path.abspath(root_path)
    if root_abs in excluded_paths:
        return []

    matches: list[str] = []
    if recursive:
        for current_root, dirs, files in os.walk(root_path):
            current_root_abs = os.path.abspath(current_root)
            if current_root_abs in excluded_paths:
                dirs[:] = []
                continue
            dirs[:] = [
                entry
                for entry in dirs
                if os.path.abspath(os.path.join(current_root, entry)) not in excluded_paths
            ]
            for entry in dirs + files:
                entry_path = os.path.join(current_root, entry)
                if _entry_matches_label(entry, label, normalized_label):
                    matches.append(entry_path)
    else:
        try:
            for entry in os.listdir(root_path):
                entry_path = os.path.join(root_path, entry)
                if _entry_matches_label(entry, label, normalized_label):
                    matches.append(entry_path)
        except FileNotFoundError:
            return []

    return _collapse_nested_matches(matches)


def _entry_matches_label(entry_name: str, label: str, normalized_label: str) -> bool:
    if _normalize_label(entry_name) == normalized_label:
        return True

    stem, _ = os.path.splitext(entry_name)
    if not stem:
        return False

    normalized_stem = _normalize_label(stem)
    if normalized_stem == normalized_label:
        return True

    expected_title, expected_year = _split_label_title_and_year(label)
    if expected_year is None:
        return False

    candidate_title, candidate_year = _extract_entry_title_and_year(stem)
    if candidate_year != expected_year or not candidate_title:
        return False

    expected_normalized = _normalize_label(expected_title)
    candidate_normalized = _normalize_label(candidate_title)
    if not expected_normalized or not candidate_normalized:
        return False
    if candidate_normalized == expected_normalized:
        return True

    token_overlap = _title_token_overlap_ratio(expected_title, candidate_title)
    title_similarity = difflib.SequenceMatcher(
        a=expected_normalized,
        b=candidate_normalized,
    ).ratio()
    return token_overlap >= 0.5 or title_similarity >= 0.72


def _split_label_title_and_year(label: str) -> tuple[str, int | None]:
    match = _LABEL_YEAR_PATTERN.search(label)
    if match is None:
        return label.strip(), None
    title = label[: match.start()].strip()
    return title, int(match.group("year"))


def _extract_entry_title_and_year(entry_stem: str) -> tuple[str, int | None]:
    year_match = None
    for match in _ENTRY_YEAR_PATTERN.finditer(entry_stem):
        year_match = match

    if year_match is None:
        cleaned_title = _LEADING_ORDER_PREFIX_PATTERN.sub("", entry_stem).strip()
        return cleaned_title, None

    raw_title = entry_stem[: year_match.start()].strip(" -_.()[]")
    cleaned_title = _LEADING_ORDER_PREFIX_PATTERN.sub("", raw_title).strip()
    return cleaned_title, int(year_match.group("year"))


def _title_token_overlap_ratio(left: str, right: str) -> float:
    left_tokens = _normalize_title_tokens(left)
    right_tokens = _normalize_title_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0

    overlap = len(left_tokens & right_tokens)
    return overlap / max(min(len(left_tokens), len(right_tokens)), 1)


def _normalize_title_tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for raw_token in _TOKEN_PATTERN.findall(value.casefold()):
        token = raw_token.strip()
        if not token:
            continue
        if token in _TITLE_STOPWORDS:
            continue
        token = token.removesuffix("'s")
        if len(token) > 4 and token.endswith("s"):
            token = token[:-1]
        if token:
            tokens.add(token)
    return tokens


async def _move_match_into_collection(
    match: CollectionMovieMatch, franchise_dir: str
) -> CollectionMoveResult:
    await asyncio.to_thread(os.makedirs, franchise_dir, exist_ok=True)
    if match.kind == "directory":
        return await _flatten_directory_into_collection(match.path, franchise_dir)
    return await _move_file_into_collection(match.path, franchise_dir)


async def _flatten_directory_into_collection(
    source_dir: str, franchise_dir: str
) -> CollectionMoveResult:
    if os.path.abspath(source_dir) == os.path.abspath(franchise_dir):
        return {"status": "already_in_collection", "destination_path": franchise_dir}

    destination_path: str | None = None
    entries = os.listdir(source_dir)
    for entry in entries:
        source_path = os.path.join(source_dir, entry)
        target_path = os.path.join(franchise_dir, entry)
        if os.path.abspath(source_path) == os.path.abspath(target_path):
            destination_path = target_path
            continue
        if os.path.exists(target_path):
            return {
                "status": "conflict",
                "destination_path": target_path,
                "detail": f"Target already exists: {target_path}",
            }
        await asyncio.to_thread(shutil.move, source_path, target_path)
        destination_path = target_path

    if entries:
        await asyncio.to_thread(shutil.rmtree, source_dir, ignore_errors=True)

    return {
        "status": "moved_to_collection",
        "destination_path": destination_path or franchise_dir,
    }


async def _move_file_into_collection(source_path: str, franchise_dir: str) -> CollectionMoveResult:
    parent_dir = os.path.dirname(source_path)
    if os.path.abspath(parent_dir) == os.path.abspath(franchise_dir):
        return {"status": "already_in_collection", "destination_path": source_path}

    destination_path = os.path.join(franchise_dir, os.path.basename(source_path))
    if os.path.exists(destination_path):
        return {
            "status": "conflict",
            "destination_path": destination_path,
            "detail": f"Target already exists: {destination_path}",
        }

    await asyncio.to_thread(shutil.move, source_path, destination_path)
    return {"status": "moved_to_collection", "destination_path": destination_path}


def _collapse_nested_matches(paths: list[str]) -> list[str]:
    collapsed: list[str] = []
    for path in sorted(paths, key=lambda candidate: (len(candidate), candidate.casefold())):
        path_abs = os.path.abspath(path)
        if any(_is_nested_under(path_abs, existing) for existing in collapsed):
            continue
        collapsed.append(path_abs)
    return collapsed


def _is_nested_under(path: str, candidate_parent: str) -> bool:
    try:
        common = os.path.commonpath([path, candidate_parent])
    except ValueError:
        return False
    return common == os.path.abspath(candidate_parent) and path != os.path.abspath(candidate_parent)
