from __future__ import annotations

from typing import Any


def build_collection_reconciliation_lines(finalization: dict[str, Any]) -> list[str]:
    """Build concise MarkdownV2-safe count lines from collection reconciliation results."""
    lines: list[str] = []

    moved_count = int(finalization.get("moved_count") or 0)
    already_count = int(finalization.get("already_in_collection_count") or 0)
    missing_count = int(finalization.get("missing_count") or 0)
    conflict_count = int(finalization.get("conflict_count") or 0)
    ambiguous_count = int(finalization.get("ambiguous_count") or 0)
    error_count = int(finalization.get("error_count") or 0)

    if moved_count:
        lines.append(f"Moved into collection folder: *{moved_count}*")
    if already_count:
        lines.append(f"Already organized: *{already_count}*")
    if missing_count:
        lines.append(f"Missing from library: *{missing_count}*")
    if conflict_count:
        lines.append(f"Conflicts requiring manual review: *{conflict_count}*")
    if ambiguous_count:
        lines.append(f"Ambiguous matches requiring review: *{ambiguous_count}*")
    if error_count:
        lines.append(f"Filesystem errors: *{error_count}*")

    return lines


def get_collection_movies_for_plex(finalization: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only the movies confirmed to be organized in the collection folder."""
    movies = finalization.get("organized_movies") or []
    if not isinstance(movies, list):
        return []
    return [movie for movie in movies if isinstance(movie, dict)]
