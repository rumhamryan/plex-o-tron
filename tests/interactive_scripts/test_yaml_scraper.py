import asyncio
import sys
import os
from unittest.mock import Mock
from pathlib import Path

# Add the project root to the Python path to allow absolute imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../")))

from telegram_bot.services.scrapers.yaml import scrape_yaml_site


async def main():
    print("Interactive testing for YAML-configured Scraper.")
    print("Enter 'exit' to quit at any time.")

    # Mock the context object required by scrape_yaml_site
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

    # List available YAML configs for user guidance
    config_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "telegram_bot"
        / "scrapers"
        / "configs"
    )
    available_configs = [f.stem for f in config_dir.glob("*.yaml")]
    print(f"\nAvailable site_names from configs: {', '.join(available_configs)}")

    while True:
        try:
            site_name = input("Enter site_name (e.g., '1337x', 'eztv'): ")
            if site_name.lower() == "exit":
                break
            if site_name not in available_configs:
                print(f"Warning: '{site_name}' config not found. Results may be empty.")

            query = input("Enter query (e.g., 'The Expanse'): ")
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

            base_query_for_filter = input(
                "Enter base query for filter (optional, press Enter to skip): "
            )
            if base_query_for_filter.lower() == "exit":
                break
            base_query_for_filter = (
                base_query_for_filter.strip() if base_query_for_filter.strip() else None
            )

            print(
                f"\nFetching YAML scraper results for '{site_name}': '{query}' (Media Type: {media_type}, Limit: {limit})..."
            )

            results = await scrape_yaml_site(
                query=query,
                media_type=media_type,
                _search_url_template="",  # Not used by scrape_yaml_site
                context=mock_context,
                site_name=site_name,
                limit=limit,
                base_query_for_filter=base_query_for_filter,
            )

            print(f"\n--- {site_name} Scraper Results ---")
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
