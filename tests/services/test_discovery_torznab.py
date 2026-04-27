from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from telegram_bot.services.discovery import DiscoveryRequest, ProviderConfig
from telegram_bot.services.discovery.exceptions import ProviderSearchError
from telegram_bot.services.discovery.providers import TorznabProvider


class DummyResponse:
    def __init__(self, text: str, *, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code < 400:
            return
        request = httpx.Request("GET", "http://indexer.local/api")
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError("request failed", request=request, response=response)


def _provider(search_url: str | None = None) -> TorznabProvider:
    return TorznabProvider(
        ProviderConfig(
            name="Prowlarr 1337x",
            type="torznab",
            search_url=search_url
            or "http://127.0.0.1:9696/1/api?apikey=KEY&t={type}&q={query}&cat={category}",
        )
    )


def test_torznab_build_search_url_substitutes_and_encodes_placeholders() -> None:
    provider = _provider()
    request = DiscoveryRequest(query="Alien Romulus", media_type="movie")

    url = provider.build_search_url(request)

    assert url == "http://127.0.0.1:9696/1/api?apikey=KEY&t=search&q=Alien+Romulus&cat=2000"


def test_torznab_build_search_url_appends_missing_query_params() -> None:
    provider = _provider("http://127.0.0.1:9117/api/v2.0/indexers/1337x/results/torznab/api")
    request = DiscoveryRequest(query="Example Show S01E02", media_type="tv")

    url = provider.build_search_url(request)

    assert url == (
        "http://127.0.0.1:9117/api/v2.0/indexers/1337x/results/torznab/api"
        "?t=tvsearch&q=Example+Show+S01E02&cat=5000"
    )


def test_torznab_parse_xml_maps_magnet_attrs_to_discovery_result() -> None:
    provider = _provider()
    xml = """
    <rss xmlns:torznab="http://torznab.com/schemas/2015/feed">
      <channel>
        <item>
          <title>Example.Movie.2024.2160p.WEB-DL.x265</title>
          <guid>https://indexer.local/details/123</guid>
          <comments>https://indexer.local/comments/123</comments>
          <torznab:attr name="magneturl" value="magnet:?xt=urn:btih:ABC123ABC123&amp;dn=Example" />
          <torznab:attr name="infohash" value="ABC123ABC123" />
          <torznab:attr name="size" value="3221225472" />
          <torznab:attr name="seeders" value="42" />
          <torznab:attr name="peers" value="50" />
          <torznab:attr name="uploader" value="TrustedUploader" />
        </item>
      </channel>
    </rss>
    """

    results = provider.parse_xml(xml)

    assert len(results) == 1
    result = results[0]
    assert result.title == "Example.Movie.2024.2160p.WEB-DL.x265"
    assert result.download_url.startswith("magnet:?xt=urn:btih:ABC123ABC123")
    assert result.magnet_url == result.download_url
    assert result.info_hash == "ABC123ABC123"
    assert result.info_url == "https://indexer.local/comments/123"
    assert result.size_bytes == 3 * 1024**3
    assert result.seeders == 42
    assert result.leechers == 8
    assert result.source == "Prowlarr 1337x"
    assert result.uploader == "TrustedUploader"
    assert result.year == 2024
    assert result.codec == "x265"
    assert result.resolution == "2160p"
    assert result.raw_data["attrs"]["seeders"] == "42"


def test_torznab_parse_xml_uses_link_and_enclosure_fallbacks() -> None:
    provider = _provider()
    xml = """
    <rss>
      <channel>
        <item>
          <title>Example.Show.S01E02.1080p.WEB.x264</title>
          <guid>https://indexer.local/details/456</guid>
          <link>https://indexer.local/download/456</link>
          <enclosure url="https://indexer.local/enclosure/456" length="1610612736" />
          <torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed" name="seeders" value="9" />
          <torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed" name="leechers" value="3" />
          <torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed" name="infohash" value="DEF456DEF456" />
        </item>
      </channel>
    </rss>
    """

    results = provider.parse_xml(xml)

    assert len(results) == 1
    result = results[0]
    assert result.download_url == "https://indexer.local/download/456"
    assert (
        result.magnet_url
        == "magnet:?xt=urn:btih:DEF456DEF456&dn=Example.Show.S01E02.1080p.WEB.x264"
    )
    assert result.info_url == "https://indexer.local/details/456"
    assert result.size_bytes == 1_610_612_736
    assert result.seeders == 9
    assert result.leechers == 3
    assert result.codec == "x264"
    assert result.resolution == "1080p"


def test_torznab_parse_xml_skips_items_without_download_or_size() -> None:
    provider = _provider()
    xml = """
    <rss>
      <channel>
        <item>
          <title>No Download 2024 1080p</title>
          <torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed" name="size" value="123" />
        </item>
        <item>
          <title>No Size 2024 1080p</title>
          <link>magnet:?xt=urn:btih:ABC123ABC123</link>
        </item>
      </channel>
    </rss>
    """

    assert provider.parse_xml(xml) == []


def test_torznab_parse_xml_returns_empty_for_malformed_xml() -> None:
    provider = _provider()

    assert provider.parse_xml("<rss><channel>") == []


@pytest.mark.asyncio
async def test_torznab_search_fetches_configured_url(mocker) -> None:
    provider = _provider()
    xml = """
    <rss>
      <channel>
        <item>
          <title>Example.Movie.2024.1080p.x264</title>
          <link>magnet:?xt=urn:btih:ABC123ABC123</link>
          <size>1073741824</size>
          <torznab:attr xmlns:torznab="http://torznab.com/schemas/2015/feed" name="seeders" value="25" />
        </item>
      </channel>
    </rss>
    """
    fetch_mock = mocker.patch(
        "telegram_bot.services.discovery.providers.torznab.fetch_page",
        new=AsyncMock(return_value=DummyResponse(xml)),
    )

    results = await provider.search(DiscoveryRequest(query="Example Movie", media_type="movie"))

    fetch_mock.assert_awaited_once_with(
        "http://127.0.0.1:9696/1/api?apikey=KEY&t=search&q=Example+Movie&cat=2000",
        timeout=8.0,
        follow_redirects=True,
    )
    assert len(results) == 1
    assert results[0].seeders == 25


@pytest.mark.asyncio
async def test_torznab_search_raises_provider_error_on_request_failure(mocker) -> None:
    provider = _provider()
    mocker.patch(
        "telegram_bot.services.discovery.providers.torznab.fetch_page",
        new=AsyncMock(return_value=DummyResponse("", status_code=503)),
    )

    with pytest.raises(ProviderSearchError):
        await provider.search(DiscoveryRequest(query="Example Movie", media_type="movie"))
