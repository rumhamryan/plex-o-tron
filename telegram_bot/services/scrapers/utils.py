import httpx
from ...config import logger

async def _get_page_html(url: str) -> str | None:
    """Fetches the HTML content of a URL."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text
    except httpx.RequestError as e:
        logger.error(f"Error fetching URL {url}: {e}")
        return None
