import os
import json
import asyncio
import shutil
import libtorrent as lt
from types import SimpleNamespace

from telegram_bot.config import get_configuration, logger
from telegram_bot.workflows.search_parser import parse_search_query
from telegram_bot.services.search_logic.orchestrator import orchestrate_searches
from telegram_bot.services.media_manager.naming import generate_plex_filename
from telegram_bot.utils import format_bytes

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# --- User Constants ---
# Default library path (used if config.ini doesn't specify one)
DEFAULT_LIBRARY_PATH = r"/tank/movies"
# Default temporary download path (used if config.ini doesn't specify one)
DEFAULT_TEMP_PATH = r"~/Downloads"
# Minimum size for a movie to be considered "high quality"
MIN_UPGRADE_SIZE_GB = 15.0
# Maximum size to allow in searches (upper bound)
MAX_SEARCH_SIZE_GB = 70.0
# File to track which movies already meet the requirement
TRACKING_FILE = os.path.join(project_root, "upgrade_tracking.json")


async def download_torrent(magnet_link: str, temp_path: str):
    """Downloads a torrent using libtorrent and waits for completion."""
    ses = lt.session()
    ses.listen_on(6881, 6891)

    params = lt.parse_magnet_uri(magnet_link)
    params.save_path = temp_path
    handle = ses.add_torrent(params)

    logger.info(f"Downloading: {handle.name()}")

    while not handle.status().is_seeding:
        s = handle.status()
        state_str = [
            "queued",
            "checking",
            "downloading metadata",
            "downloading",
            "finished",
            "seeding",
            "allocating",
            "checking fastresume",
        ]

        progress = s.progress * 100
        logger.info(
            f"Progress: {progress:.2f}% | "
            f"Down: {format_bytes(s.download_rate)}/s | "
            f"State: {state_str[s.state]} | "
            f"Seeds: {s.num_seeds}"
        )

        if progress < 100 or s.state != 5:  # 5 is seeding
            await asyncio.sleep(5)
        else:
            break

    logger.info(f"Download completed: {handle.name()}")
    return handle.torrent_file(), handle.save_path()


def find_largest_file(torrent_info, base_path):
    """Finds the largest file in the torrent (assumed to be the movie)."""
    files = torrent_info.files()
    largest_size = 0
    largest_path = None

    for i in range(files.num_files()):
        file_path = files.file_path(i)
        full_path = os.path.join(base_path, file_path)
        if os.path.exists(full_path):
            size = os.path.getsize(full_path)
            if size > largest_size:
                largest_size = size
                largest_path = full_path

    return largest_path


async def upgrade_movies():
    """
    Scans the local movie library and searches for higher quality (larger) torrents.
    """
    # 1. Load configuration
    try:
        os.chdir(project_root)
        _, paths, _, _, search_config, _, runtime_limits = get_configuration()
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        return

    scan_path = paths.get("movies", DEFAULT_LIBRARY_PATH)
    temp_path = paths.get("default", DEFAULT_TEMP_PATH)

    if not os.path.exists(scan_path):
        logger.warning(f"Path '{scan_path}' not found. Falling back to '{DEFAULT_LIBRARY_PATH}'")
        scan_path = DEFAULT_LIBRARY_PATH

    if not os.path.exists(scan_path):
        logger.error(f"Library path not found: {scan_path}")
        return

    if not temp_path:
        temp_path = DEFAULT_TEMP_PATH

    if not os.path.exists(temp_path):
        logger.info(f"Temp path '{temp_path}' not found. Creating it.")
        os.makedirs(temp_path, exist_ok=True)

    # 2. Mock bot context for the orchestrator
    context = SimpleNamespace()
    context.bot_data = {
        "SEARCH_CONFIG": search_config,
        "SCRAPER_MAX_TORRENT_SIZE_GIB": runtime_limits.get("scraper_max_torrent_size_gib", 22.0),
    }

    # 3. Load tracking state
    tracking_state = {"met_requirement": []}
    if os.path.exists(TRACKING_FILE):
        try:
            with open(TRACKING_FILE, "r", encoding="utf-8") as f:
                tracking_state = json.load(f)
        except Exception:
            pass

    met_requirement = set(tracking_state.get("met_requirement", []))

    # 4. Scan library
    logger.info(f"Scanning library: {scan_path}")
    movies_to_check = []
    extensions = (".mkv", ".mp4", ".avi", ".mov", ".m4v")

    for root, dirs, files in os.walk(scan_path):
        # Skip hidden directories (like .Trash-1000, .local, etc.)
        dirs[:] = [d for d in dirs if not d.startswith(".")]

        for file in files:
            # Skip hidden files
            if file.startswith("."):
                continue

            lower_file = file.lower()
            if any(lower_file.endswith(ext) for ext in extensions):
                if file in met_requirement:
                    continue

                file_path = os.path.join(root, file)
                try:
                    size_gb = os.path.getsize(file_path) / (1024**3)
                except OSError:
                    continue

                if size_gb >= MIN_UPGRADE_SIZE_GB:
                    logger.info(
                        f"[PASS] '{file}' meets {MIN_UPGRADE_SIZE_GB}GB threshold ({size_gb:.2f}GB)"
                    )
                    met_requirement.add(file)
                else:
                    movies_to_check.append(
                        {"filename": file, "current_path": file_path, "current_size": size_gb}
                    )

    if not movies_to_check:
        logger.info("All scanned movies already meet the size requirement.")
        _save_tracking(met_requirement)
        return

    # Sort movies alphabetically by filename for better UX
    movies_to_check.sort(key=lambda x: x["filename"].lower())
    logger.info(f"Found {len(movies_to_check)} movies to potentially upgrade.")

    # 5. Search and Approval Phase
    upgrade_queue = []
    for movie in movies_to_check:
        filename = movie["filename"]
        current_path = movie["current_path"]

        # Strip extension for cleaner parsing and searching
        name_no_ext = os.path.splitext(filename)[0]
        parsed = parse_search_query(name_no_ext)
        query = parsed.title
        year = parsed.year

        print("\n" + "=" * 60)
        logger.info(f"Checking: {query} ({year or 'N/A'})")
        logger.info(f"Current File: {filename} ({movie['current_size']:.2f} GB)")
        print("-" * 60)

        try:
            results = await orchestrate_searches(
                query, "movie", context, year=year, max_size_gib=MAX_SEARCH_SIZE_GB
            )

            upgrades = [r for r in results if r.get("size_gib", 0) >= MIN_UPGRADE_SIZE_GB]

            if upgrades:
                best = upgrades[0]

                v_formats = ", ".join(best.get("matched_video_formats", [])) or "Standard"
                a_formats = ", ".join(best.get("matched_audio_formats", [])) or "Standard"
                channels = best.get("matched_audio_channels", "N/A")

                print("\n[!] UPGRADE CANDIDATE FOUND:")
                print(f"    Title:    {best['title']}")
                print(f"    Size:     {best['size_gib']:.2f} GB")
                print(f"    Health:   {best['seeders']} Seeders / {best['leechers']} Leechers")
                print(f"    Video:    {v_formats}")
                print(f"    Audio:    {a_formats} ({channels})")
                print(f"    Source:   {best['source']}")
                print(f"    Score:    {best['score']}")

                choice = (
                    input(f"\nAdd to upgrade queue for '{query}'? (y/n/q to start downloads): ")
                    .strip()
                    .lower()
                )

                if choice == "y":
                    logger.info(f"[QUEUED] Added to batch: {best['title']}")
                    upgrade_queue.append({"movie": movie, "torrent": best, "parsed": parsed})
                elif choice == "q":
                    logger.info("Ending approval phase and starting downloads...")
                    break
                else:
                    logger.info("[SKIPPED] User declined upgrade.")
            else:
                logger.info(f"[-] No upgrade found meeting {MIN_UPGRADE_SIZE_GB}GB requirement.")

        except Exception as e:
            logger.error(f"Error searching for '{query}': {e}")

    # 6. Execution Phase (Batch Processing)
    if not upgrade_queue:
        logger.info("No upgrades were approved.")
        _save_tracking(met_requirement)
        return

    print("\n" + "!" * 60)
    logger.info(f"STARTING BATCH DOWNLOADS ({len(upgrade_queue)} movies)")
    print("!" * 60 + "\n")

    for item in upgrade_queue:
        movie = item["movie"]
        best = item["torrent"]
        parsed = item["parsed"]
        current_path = movie["current_path"]

        try:
            logger.info(f"--- Processing: {best['title']} ---")
            ti, download_base = await download_torrent(best["page_url"], temp_path)

            new_file_path = find_largest_file(ti, download_base)
            if new_file_path:
                original_dir = os.path.dirname(current_path)
                _, ext = os.path.splitext(new_file_path)

                # Ensure the generator knows this is a movie so it includes the year
                name_info = parsed.__dict__.copy()
                name_info["type"] = "movie"

                final_name = generate_plex_filename(name_info, ext)
                final_path = os.path.join(original_dir, final_name)

                logger.info(f"Replacing '{current_path}' with '{final_path}'")

                if os.path.abspath(current_path) != os.path.abspath(final_path):
                    if os.path.exists(current_path):
                        os.remove(current_path)

                shutil.move(new_file_path, final_path)
                logger.info(f"SUCCESS: {final_name} upgraded.")
                met_requirement.add(final_name)
            else:
                logger.error(f"FAILED: Could not find movie file in torrent for {best['title']}")
        except Exception as e:
            logger.error(f"CRITICAL ERROR processing {best['title']}: {e}")

    _save_tracking(met_requirement)
    logger.info("\nAll queued upgrades processed.")


def _save_tracking(met_requirement_set):
    """Saves the tracking state to a JSON file."""
    state = {"met_requirement": sorted(list(met_requirement_set))}
    try:
        with open(TRACKING_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4)
    except Exception as e:
        logger.error(f"Failed to save tracking file: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(upgrade_movies())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    except Exception as e:
        logger.exception(f"Unhandled error: {e}")
