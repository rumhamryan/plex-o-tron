import asyncio
import warnings

import wikipedia
from bs4 import GuessedAtParserWarning

from ....config import logger


async def _fetch_html_from_page(page: wikipedia.WikipediaPage) -> str | None:
    """Fetches HTML content from a WikipediaPage object in a thread-safe manner."""

    def _get_html():
        try:
            return page.html()
        except Exception as e:
            logger.error(f"[WIKI] Error fetching HTML for '{page.title}': {e}")
            return None

    return await asyncio.to_thread(_get_html)


warnings.filterwarnings("ignore", category=GuessedAtParserWarning, module=r"^wikipedia\.wikipedia$")
