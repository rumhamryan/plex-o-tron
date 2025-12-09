import asyncio
import os
import sys

# Add the project root to the Python path to allow absolute imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../")))

from telegram_bot.services.scrapers.yts import scrape_yts


class MockContext:
    def __init__(self):
        self.bot_data = {
            "SEARCH_CONFIG": {
                "preferences": {
                    "movies": {
                        "quality": ["1080p", "720p"],
                        "keywords_included": [],
                        "keywords_excluded": [],
                        "min_seeders": 1,
                        "max_size_gb": 100,
                    }
                }
            }
        }


async def run_single_scrape() -> None:
    """Run a deterministic scrape to quickly validate Stage 1 behavior."""
    query = "Superman"
    year = 2025
    resolution = "1080p"
    print(f"Running YTS scrape for {query!r} ({year}, {resolution})")

    context = MockContext()
    search_url_template = (
        "https://yts.lt/browse-movies/{query}/{quality}/all/0/latest/{year}/all"
    )

    results = await scrape_yts(
        query=query,
        media_type="movie",
        search_url_template=search_url_template,
        context=context,
        year=year,
        resolution=resolution,
    )

    print(f"Found {len(results)} torrent(s).")
    for idx, item in enumerate(results, 1):
        print(f"\nResult {idx}:")
        for key, value in item.items():
            print(f"  {key}: {value}")


if __name__ == "__main__":
    asyncio.run(run_single_scrape())
