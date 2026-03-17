# telegram_bot/workflows/delete_workflow/plex.py

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Literal, TypedDict

from plexapi.exceptions import BadRequest, NotFound, Unauthorized
from plexapi.server import PlexServer

from ...config import logger

if TYPE_CHECKING:
    from plexapi.video import Episode, Movie, Season, Show

from .helpers import _has_name_twin


class PlexDeleteResult(TypedDict, total=False):
    status: Literal["success", "skip", "not_found", "manual_delete_required", "error"]
    detail: str
    plex_deleted: bool
    plex_items_deleted: int


def _get_part_files(plex_item) -> set[str]:
    """Collect normalized file paths for all Plex media parts on an item."""
    part_files: set[str] = set()
    for media in getattr(plex_item, "media", []) or []:
        for part in getattr(media, "parts", []) or []:
            file_path = getattr(part, "file", None)
            if not file_path:
                continue
            try:
                part_files.add(os.path.abspath(file_path))
            except Exception:
                part_files.add(str(file_path))
    return part_files


def _has_multiple_plex_parts(plex_item, target_path: str) -> bool:
    """Detect whether a Plex item has multiple distinct file parts."""
    try:
        target_abs = os.path.abspath(target_path)
    except Exception:
        target_abs = target_path

    part_files = _get_part_files(plex_item)

    if len(part_files) <= 1:
        return False

    if target_abs and target_abs in part_files:
        return True
    return False


def _has_plex_parts_outside_directory(plex_item, target_path: str) -> bool:
    """Return True when a Plex item also references files outside the selected directory."""
    try:
        target_abs = os.path.abspath(target_path)
    except Exception:
        target_abs = target_path

    if not target_abs:
        return False

    target_prefix = target_abs + os.sep
    for part_file in _get_part_files(plex_item):
        if part_file != target_abs and not part_file.startswith(target_prefix):
            return True
    return False


async def _delete_plex_collection(plex: PlexServer, collection_name: str) -> bool:
    """Delete a Plex collection by name, if it exists."""
    if not collection_name:
        return False

    normalized = collection_name.strip()
    if not normalized:
        return False

    try:
        for section in plex.library.sections():
            if getattr(section, "type", None) != "movie":
                continue
            try:
                collection = section.collection(normalized)
            except NotFound:
                continue

            await asyncio.to_thread(collection.delete)
            logger.info("Deleted Plex collection '%s'.", normalized)
            return True
    except Exception as exc:
        logger.error("Failed to delete Plex collection '%s': %s", normalized, exc, exc_info=True)
        raise

    logger.info(
        "No Plex collection named '%s' was found across the configured sections.",
        normalized,
    )
    return False


def _find_media_in_plex_by_path(
    plex: PlexServer, path_to_delete: str
) -> Movie | Episode | Show | Season | None:
    """
    Scans all Plex libraries to find the media item that corresponds to a given file or directory path.

    This is a blocking function and must be run in a separate thread.
    """
    # Normalize paths to be safe
    path_to_delete = os.path.abspath(path_to_delete)

    # Iterate through all video libraries
    for section in plex.library.sections():
        if section.type in ["movie", "show"]:
            logger.info(
                f"Searching for path '{path_to_delete}' in Plex library '{section.title}'..."
            )

            # Use the .search() method for efficiency where possible, but we need to check file paths,
            # which often requires iterating.
            for item in section.all():
                # For Shows, Seasons, or Movies (items with locations)
                if hasattr(item, "locations"):
                    for location in item.locations:
                        if os.path.abspath(location) == path_to_delete:
                            logger.info(f"Found match for directory: {item.title}")
                            return item  # Found a match for a whole Show or Movie directory

                # For individual episodes
                if hasattr(item, "media"):
                    for media in item.media:
                        for part in media.parts:
                            if os.path.abspath(part.file) == path_to_delete:
                                logger.info(f"Found match for file: {item.title}")
                                return item  # Found a match for an Episode file

    logger.warning(f"Could not find any item in Plex matching path: {path_to_delete}")
    return None


async def _delete_item_from_plex(
    path_to_delete: str, plex_config: dict
) -> tuple[PlexDeleteResult, PlexServer | None]:
    """
    Connects to Plex, finds a media item by its path, and deletes it via the API.
    Returns a structured result plus the Plex connection for downstream use.
    """
    plex: PlexServer | None = None
    try:
        plex = await asyncio.to_thread(PlexServer, plex_config["url"], plex_config["token"])
    except Unauthorized:
        return (
            {"status": "error", "detail": "Plex authentication failed."},
            None,
        )
    except Exception as exc:
        logger.error("Plex connection failed during deletion request: %s", exc, exc_info=True)
        return (
            {
                "status": "error",
                "detail": f"An error occurred while communicating with Plex: {exc}",
            },
            None,
        )

    if plex is None:
        logger.error("Plex connection returned None during deletion.")
        return (
            {
                "status": "error",
                "detail": "Plex connection failed unexpectedly.",
            },
            None,
        )

    plex_server: PlexServer = plex

    abs_path = os.path.abspath(path_to_delete)
    if os.path.isfile(abs_path):
        try:
            if _has_name_twin(abs_path):
                detail = "Skipped Plex deletion because another encode with the same name exists in this directory."
                logger.info("%s", detail)
                return (
                    {"status": "skip", "detail": detail, "plex_deleted": False},
                    plex_server,
                )
        except RuntimeError as exc:
            return ({"status": "error", "detail": str(exc)}, plex_server)

    try:
        if os.path.isdir(abs_path):
            logger.info(
                "Directory delete requested. Searching Plex movies under: %s",
                abs_path,
            )
            items_to_delete = []
            seen_keys: set[int] = set()

            for section in plex_server.library.sections():
                if getattr(section, "type", None) != "movie":
                    continue
                for item in section.all():
                    try:
                        for media in getattr(item, "media", []) or []:
                            for part in getattr(media, "parts", []) or []:
                                part_path = os.path.abspath(getattr(part, "file", ""))
                                if part_path.startswith(abs_path + os.sep):
                                    rk = getattr(item, "ratingKey", None)
                                    if rk is not None and rk not in seen_keys:
                                        seen_keys.add(rk)
                                        items_to_delete.append(item)
                                    break
                    except Exception:
                        continue

            if items_to_delete:
                base_name = os.path.basename(abs_path)
                logger.info(
                    "Found %d Plex movie(s) under collection folder '%s'. Deleting...",
                    len(items_to_delete),
                    base_name,
                )
                deleted_count = 0
                skipped_for_external_parts = 0
                rejected_titles: list[str] = []

                for it in items_to_delete:
                    title = getattr(it, "title", getattr(it, "ratingKey", "Unknown title"))
                    if _has_plex_parts_outside_directory(it, abs_path):
                        skipped_for_external_parts += 1
                        logger.info(
                            "Skipping Plex deletion for '%s' because other files exist outside '%s'.",
                            title,
                            abs_path,
                        )
                        continue

                    try:
                        await asyncio.to_thread(it.delete)
                        deleted_count += 1
                    except BadRequest as exc:
                        rejected_titles.append(str(title))
                        logger.warning(
                            "Plex rejected deletion for '%s' under collection folder '%s': %s",
                            title,
                            base_name,
                            exc,
                        )

                if skipped_for_external_parts or rejected_titles:
                    details: list[str] = []
                    if deleted_count:
                        details.append(f"Deleted {deleted_count} Plex movie(s).")
                    if skipped_for_external_parts:
                        details.append(
                            f"Left {skipped_for_external_parts} movie(s) in Plex because other encodes exist outside the selected folder."
                        )
                    if rejected_titles:
                        details.append(
                            "Plex rejected deletion for: " + ", ".join(rejected_titles) + "."
                        )
                    details.append(
                        "The selected folder can still be removed from disk, but Plex may need a library refresh or trash cleanup afterward."
                    )

                    return (
                        {
                            "status": "manual_delete_required",
                            "detail": " ".join(details),
                            "plex_deleted": deleted_count > 0,
                            "plex_items_deleted": deleted_count,
                        },
                        plex_server,
                    )

                return (
                    {
                        "status": "success",
                        "detail": f"Deleted {deleted_count} Plex movie(s) linked to '{base_name}'.",
                        "plex_deleted": True,
                        "plex_items_deleted": deleted_count,
                    },
                    plex_server,
                )

        plex_item = await asyncio.to_thread(_find_media_in_plex_by_path, plex_server, abs_path)

        if not plex_item:
            return (
                {
                    "status": "not_found",
                    "detail": "Could not find the item in Plex. It may have already been removed.",
                },
                plex_server,
            )

        if os.path.isfile(abs_path) and _has_multiple_plex_parts(plex_item, abs_path):
            detail = (
                "Skipped Plex deletion because other encodes exist for this item. "
                "Removing only the selected file from disk."
            )
            logger.info("%s", detail)
            return (
                {"status": "skip", "detail": detail, "plex_deleted": False},
                plex_server,
            )

        display_name = plex_item.title
        logger.info("Found Plex item '%s'. Attempting API deletion...", display_name)
        try:
            await asyncio.to_thread(plex_item.delete)
        except BadRequest as exc:
            detail = (
                f"Plex rejected deletion for '{display_name}'. "
                "The item can still be removed from disk, but Plex may need a library refresh or trash cleanup afterward."
            )
            logger.warning("%s Details: %s", detail, exc)
            return (
                {
                    "status": "manual_delete_required",
                    "detail": detail,
                    "plex_deleted": False,
                    "plex_items_deleted": 0,
                },
                plex_server,
            )
        logger.info("Successfully deleted '%s' via Plex API.", display_name)
        return (
            {
                "status": "success",
                "detail": f"Deleted Plex item '{display_name}'.",
                "plex_deleted": True,
                "plex_items_deleted": 1,
            },
            plex_server,
        )

    except Exception as exc:
        logger.error("An unexpected error occurred during Plex deletion: %s", exc, exc_info=True)
        return (
            {
                "status": "error",
                "detail": f"An error occurred while communicating with Plex: {exc}",
            },
            plex_server,
        )
