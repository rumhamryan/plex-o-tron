import asyncio
import sys
import os
from unittest.mock import Mock

# Add the project root to the Python path to allow absolute imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../")))

from telegram_bot.services.scrapers.one_three_three_seven_x import scrape_1337x


async def main():
    print("Interactive testing for 1337x scraper.")
    print("Enter 'exit' to quit at any time.")

    # Mock the context object required by scrape_1337x
    # It needs bot_data.SEARCH_CONFIG.preferences for both movies and tv
    mock_context = Mock()
    mock_context.bot_data = {
        "SEARCH_CONFIG": {
            "preferences": {
                "movies": {
                    "quality": ["1080p", "720p"],
                    "keywords_included": [],
                    "keywords_excluded": [],
                    "min_seeders": 1,
                    "max_size_gb": 100,
                },
                "tv": {
                    "quality": ["1080p", "720p"],
                    "keywords_included": [],
                    "keywords_excluded": [],
                    "min_seeders": 1,
                    "max_size_gb": 100,
                },
            }
        }
    }

    while True:
        try:
            media_type = input("\nEnter media type ('movie' or 'tv'): ").strip().lower()
            if media_type == "exit":
                break
            if media_type not in {"movie", "tv"}:
                print("Invalid media type. Please enter 'movie' or 'tv'.")
                continue

            raw_title = input("Enter title/query (e.g., 'The Mandalorian'): ").strip()
            if raw_title.lower() == "exit":
                break
            if not raw_title:
                print("Title cannot be empty.")
                continue

            final_query = raw_title
            if media_type == "tv":
                season_str = input("Enter season number (e.g., 2): ").strip()
                if season_str.lower() == "exit":
                    break
                episode_str = input("Enter episode number (e.g., 3): ").strip()
                if episode_str.lower() == "exit":
                    break
                if not (season_str.isdigit() and episode_str.isdigit()):
                    print("Season and episode must be numeric.")
                    continue
                season = int(season_str)
                episode = int(episode_str)
                final_query = f"{raw_title} S{season:02d}E{episode:02d}"

            limit_str = input(
                "Enter limit for results (default 15, press Enter for default): "
            )
            if limit_str.lower().strip() == "exit":
                break
            limit = int(limit_str) if limit_str.strip() else 15

            print(
                f"\nFetching 1337x results for: '{final_query}' (Media Type: {media_type}, Limit: {limit})..."
            )

            results = await scrape_1337x(
                query=final_query,
                media_type=media_type,
                search_url_template="",  # Not directly used by scrape_1337x, config is loaded internally
                context=mock_context,
                limit=limit,
            )

            print("\n--- 1337x Scraper Results ---")
            if results:
                for i, item in enumerate(results):
                    print(f"Result {i+1}:")
                    for key, value in item.items():
                        print(f"  {key}: {value}")
                    print("-" * 20)
            else:
                print("No results found.")
            print("---------------------------\n")

        except ValueError:
            print("Invalid input. Please enter a valid number for limit.")
        except Exception as e:
            print(f"An error occurred: {e}")
            import traceback

            traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
