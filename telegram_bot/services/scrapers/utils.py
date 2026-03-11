import httpx

from ...config import logger
from .adapters import fetch_page


async def _get_page_html(url: str) -> str | None:
    """Fetches the HTML content of a URL."""
    try:
        response = await fetch_page(url)
        response.raise_for_status()
        return response.text
    except httpx.RequestError as e:
        logger.error(f"Error fetching URL {url}: {e}")
        return None
