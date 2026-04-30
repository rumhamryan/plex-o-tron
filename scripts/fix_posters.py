import os
import sys
import configparser
from plexapi.server import PlexServer


def load_config():
    config_path = os.path.join(os.path.dirname(__file__), "..", "config.ini")
    if not os.path.exists(config_path):
        print(f"Error: config.ini not found at {config_path}")
        sys.exit(1)

    # Standard configparser might fail on the [search] section due to JSON formatting
    # So we read the file and extract the [plex] section manually or strip the problematic parts
    with open(config_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    plex_lines = []
    in_plex = False
    for line in lines:
        if line.strip() == "[plex]":
            in_plex = True
        elif in_plex and line.startswith("["):
            break
        if in_plex:
            plex_lines.append(line)

    plex_config = configparser.ConfigParser()
    plex_config.read_string("".join(plex_lines))

    url = plex_config.get("plex", "plex_url", fallback=None)
    token = plex_config.get("plex", "plex_token", fallback=None)

    if not url or not token:
        print("Error: plex_url or plex_token missing from config.ini")
        sys.exit(1)

    return url, token


def fix_posters():
    baseurl, token = load_config()

    # Check for a specific movie title passed as an argument
    target_title = sys.argv[1] if len(sys.argv) > 1 else None

    print(f"Connecting to Plex at {baseurl}...")

    try:
        plex = PlexServer(baseurl, token)
        movies_section = plex.library.section("Movies")

        if target_title:
            print(f"Test Mode: Searching for '{target_title}'...")
            movies = movies_section.search(title=target_title)
            if not movies:
                print(f"Error: Could not find movie matching '{target_title}'")
                return
        else:
            print("Full Mode: Processing entire library...")
            movies = movies_section.all()
    except Exception as e:
        print(f"Failed to connect to Plex: {e}")
        return

    print(f"Found {len(movies)} movie(s). Processing...")

    fixed_count = 0
    skipped_count = 0

    for movie in movies:
        title = movie.title

        # Skip if "Collection" is in the title
        if "Collection" in title:
            print(f"[-] Skipping: {title} (Collection)")
            skipped_count += 1
            continue

        try:
            posters = movie.posters()
            if not posters:
                print(f"[!] No posters found for: {title}")
                continue

            # The first poster in the list is almost always the "official" one
            # or the one Plex thinks is best from the primary agent.
            official_poster = posters[0]

            # Only update if the current one isn't already the first one
            if not official_poster.selected:
                print(f"[+] Updating: {title}")
                movie.setPoster(official_poster)
                # Lock the poster to prevent automatic changes later
                movie.lockPoster()
                fixed_count += 1
            else:
                print(f"[.] Already set: {title}")

        except Exception as e:
            print(f"[X] Error processing {title}: {e}")

    print("\n" + "=" * 30)
    print("Done!")
    print(f"Fixed: {fixed_count}")
    print(f"Skipped: {skipped_count}")
    print("=" * 30)


if __name__ == "__main__":
    fix_posters()
