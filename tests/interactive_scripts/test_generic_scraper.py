import asyncio
import sys
import os

# Add the project root to the Python path to allow absolute imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../")))

from telegram_bot.services.scrapers.generic import scrape_generic_page


async def main():
    print("Interactive testing for Generic Scraper.")
    print("Enter 'exit' to quit at any time.")

    while True:
        try:
            query = input("\nEnter query (e.g., 'ubuntu 22.04'): ")
            if query.lower() == "exit":
                break

            media_type = input(
                "Enter media type (e.g., 'software', 'movie', 'tv'): "
            ).lower()
            if media_type.lower() == "exit":
                break

            search_url = input("Enter search URL (e.g., 'https://linuxtracker.org/'): ")
            if search_url.lower() == "exit":
                break

            print(
                f"\nFetching generic results for: '{query}' (Media Type: {media_type}, Search URL: {search_url})..."
            )

            results = await scrape_generic_page(
                query=query, media_type=media_type, search_url=search_url
            )

            print("\n--- Generic Scraper Results ---")
            if results:
                for i, item in enumerate(results):
                    print(f"Result {i+1}:")
                    for key, value in item.items():
                        print(f"  {key}: {value}")
                    print("-" * 20)
            else:
                print("No results found.")
            print("---------------------------\n")

        except Exception as e:
            print(f"An error occurred: {e}")
            import traceback

            traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
