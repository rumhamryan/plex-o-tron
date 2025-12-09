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
            query = input("\nEnter query (e.g., 'The Mandalorian'): ")
            if query.lower() == "exit":
                break

            media_type = input("Enter media type ('movie' or 'tv'): ").lower()
            if media_type.lower() == "exit":
                break
            if media_type not in ["movie", "tv"]:
                print("Invalid media type. Please enter 'movie' or 'tv'.")
                continue

            limit_str = input(
                "Enter limit for results (default 15, press Enter for default): "
            )
            limit = int(limit_str) if limit_str.strip() else 15

            print(
                f"\nFetching 1337x results for: '{query}' (Media Type: {media_type}, Limit: {limit})..."
            )

            results = await scrape_1337x(
                query=query,
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
