# telegram_bot/services/media_manager/naming.py

from typing import Any


def generate_plex_filename(parsed_info: dict[str, Any], original_extension: str) -> str:
    """Generates a clean, Plex-friendly filename from the parsed info."""
    title = parsed_info.get("title", "Unknown Title")
    invalid_chars = r'<>:"/\\|?*'
    safe_title = "".join(c for c in title if c not in invalid_chars)

    if parsed_info.get("type") == "movie":
        year = parsed_info.get("year", "Unknown Year")
        return f"{safe_title} ({year}){original_extension}"

    if parsed_info.get("type") == "tv":
        season = parsed_info.get("season", 0)
        episode = parsed_info.get("episode", 0)
        episode_title = parsed_info.get("episode_title")
        safe_episode_title = ""
        if episode_title:
            safe_episode_title = " - " + "".join(c for c in episode_title if c not in invalid_chars)
        return f"s{season:02d}e{episode:02d}{safe_episode_title}{original_extension}"

    # Fallback for unknown media types.
    return f"{safe_title}{original_extension}"


def parse_resolution_from_name(name: str) -> str:
    """Parses a torrent name to find the video resolution."""
    name_lower = name.lower()
    if any(res in name_lower for res in ["2160p", "4k", "uhd"]):
        return "4K"
    if "1080p" in name_lower:
        return "1080p"
    if "720p" in name_lower:
        return "720p"
    if any(res in name_lower for res in ["480p", "sd", "dvdrip"]):
        return "SD"
    return "N/A"


def _build_media_display_name(parsed_info: dict[str, Any]) -> str:
    """Return a human-readable label for success toasts."""
    title = str(parsed_info.get("title") or "Download").strip() or "Download"
    media_type = parsed_info.get("type")
    is_season_pack = parsed_info.get("is_season_pack")

    if media_type == "movie":
        year = parsed_info.get("year")
        if year:
            return f"{title} ({year})"
        return title

    if media_type == "tv":
        season = parsed_info.get("season")
        episode = parsed_info.get("episode")
        if is_season_pack:
            if isinstance(season, int):
                return f"{title} - Season {season:02d}"
            return f"{title} - Season"
        if isinstance(season, int) and isinstance(episode, int):
            return f"{title} - S{season:02d}E{episode:02d}"
        if isinstance(season, int):
            return f"{title} - Season {season:02d}"
        return title

    return title
