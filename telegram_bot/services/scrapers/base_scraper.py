# telegram_bot/services/scrapers/base_scraper.py

from abc import ABC, abstractmethod
from typing import Any


class Scraper(ABC):
    """
    Abstract base class for all scrapers.
    """

    @abstractmethod
    async def search(
        self,
        query: str,
        media_type: str,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """
        Search for media on the scraper's site.

        Args:
            query: The search query.
            media_type: The type of media to search for (e.g., "movie", "tv").
            **kwargs: Additional keyword arguments for the scraper.

        Returns:
            A list of dictionaries, where each dictionary represents a search result.
        """
        pass
