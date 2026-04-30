from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from typing import Any

import httpx

from ....config import logger
from ....utils import parse_codec, parse_torrent_name
from ..exceptions import ProviderSearchError
from ..schemas import DiscoveryRequest, DiscoveryResult
from .base import BaseProvider

_DEFAULT_TORZNAB_TYPES = {
    "movie": "search",
    "tv": "tvsearch",
}
_RESOLUTION_PATTERN = re.compile(r"(?i)\b(2160p|1080p|720p|480p|4k)\b")


async def fetch_page(
    url: str,
    *,
    timeout: float = 30,
    follow_redirects: bool = True,
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects) as client:
        return await client.get(url)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _safe_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return parsed if parsed >= 0 else 0


def _direct_child_text(item: ET.Element, child_name: str) -> str | None:
    for child in item:
        if _local_name(child.tag) == child_name and child.text:
            stripped = child.text.strip()
            if stripped:
                return stripped
    return None


def _torznab_attrs(item: ET.Element) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for child in item.iter():
        if _local_name(child.tag) != "attr":
            continue
        name = child.attrib.get("name")
        value = child.attrib.get("value")
        if isinstance(name, str) and isinstance(value, str):
            attrs[name.strip().casefold()] = value.strip()
    return attrs


def _enclosure(item: ET.Element) -> tuple[str | None, int]:
    for child in item:
        if _local_name(child.tag) != "enclosure":
            continue
        url = child.attrib.get("url")
        length = _safe_int(child.attrib.get("length"))
        if isinstance(url, str) and url.strip():
            return url.strip(), length
        return None, length
    return None, 0


def _extract_info_hash_from_magnet(magnet_url: str | None) -> str | None:
    if not magnet_url or not magnet_url.startswith("magnet:"):
        return None
    parsed = urllib.parse.urlsplit(magnet_url)
    params = urllib.parse.parse_qs(parsed.query)
    for xt_value in params.get("xt", []):
        lowered = xt_value.casefold()
        marker = "urn:btih:"
        if marker in lowered:
            return xt_value[lowered.index(marker) + len(marker) :].strip() or None
    return None


def _build_magnet_from_info_hash(info_hash: str | None, title: str) -> str | None:
    if not info_hash:
        return None
    cleaned = info_hash.strip()
    if len(cleaned) < 10:
        return None
    return f"magnet:?xt=urn:btih:{cleaned}&dn={urllib.parse.quote_plus(title)}"


def _resolution_from_title(title: str) -> str | None:
    match = _RESOLUTION_PATTERN.search(title)
    return match.group(1).lower() if match else None


def _iter_items(root: ET.Element) -> Iterable[ET.Element]:
    for element in root.iter():
        if _local_name(element.tag) == "item":
            yield element


class TorznabProvider(BaseProvider):
    """Discovery provider for Torznab-compatible RSS/XML endpoints."""

    async def search(self, request: DiscoveryRequest) -> list[DiscoveryResult]:
        url = self.build_search_url(request)
        try:
            response = await fetch_page(
                url,
                timeout=self.config.timeout_seconds,
                follow_redirects=True,
            )
            response.raise_for_status()
        except Exception as exc:
            logger.error(
                "[DISCOVERY] %s: Torznab request failed for %r: %s: %s",
                self.config.name,
                request.query,
                type(exc).__name__,
                exc,
            )
            raise ProviderSearchError(
                f"Torznab request failed for {request.query!r}",
                provider_name=self.config.name,
            ) from exc

        return self.parse_xml(response.text)

    def build_search_url(self, request: DiscoveryRequest) -> str:
        category = self.config.categories.get(
            request.media_type,
            "5000" if request.media_type == "tv" else "2000",
        )
        torznab_type = _DEFAULT_TORZNAB_TYPES[request.media_type]
        replacements = {
            "{query}": urllib.parse.quote_plus(request.query),
            "{QUERY}": urllib.parse.quote_plus(request.query),
            "{type}": urllib.parse.quote_plus(torznab_type),
            "{TYPE}": urllib.parse.quote_plus(torznab_type),
            "{category}": urllib.parse.quote_plus(category),
            "{CATEGORY}": urllib.parse.quote_plus(category),
            "{cat}": urllib.parse.quote_plus(category),
            "{CAT}": urllib.parse.quote_plus(category),
        }

        url = self.config.search_url.strip()
        replaced_any = False
        for placeholder, value in replacements.items():
            if placeholder in url:
                replaced_any = True
                url = url.replace(placeholder, value)

        if replaced_any:
            return url

        parsed = urllib.parse.urlsplit(url)
        params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        existing_keys = {key.casefold() for key, _ in params}
        if "t" not in existing_keys:
            params.append(("t", torznab_type))
        if "q" not in existing_keys:
            params.append(("q", request.query))
        if "cat" not in existing_keys:
            params.append(("cat", category))

        return urllib.parse.urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urllib.parse.urlencode(params),
                parsed.fragment,
            )
        )

    def parse_xml(self, xml_content: str) -> list[DiscoveryResult]:
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as exc:
            logger.error("[DISCOVERY] %s: Invalid Torznab XML: %s", self.config.name, exc)
            return []

        results: list[DiscoveryResult] = []
        for item in _iter_items(root):
            result = self._map_item_to_result(item)
            if result is not None:
                results.append(result)
        return results

    def _map_item_to_result(self, item: ET.Element) -> DiscoveryResult | None:
        title = _direct_child_text(item, "title")
        if not title:
            return None

        attrs = _torznab_attrs(item)
        enclosure_url, enclosure_size = _enclosure(item)
        link = _direct_child_text(item, "link")
        comments = _direct_child_text(item, "comments")
        guid = _direct_child_text(item, "guid")

        explicit_magnet_url = attrs.get("magneturl")
        if link and link.startswith("magnet:"):
            explicit_magnet_url = link

        info_hash = attrs.get("infohash") or _extract_info_hash_from_magnet(explicit_magnet_url)
        magnet_url = explicit_magnet_url or _build_magnet_from_info_hash(info_hash, title)

        download_url = explicit_magnet_url or link or enclosure_url or magnet_url
        if not download_url:
            return None

        size_bytes = (
            _safe_int(attrs.get("size"))
            or _safe_int(_direct_child_text(item, "size"))
            or enclosure_size
        )
        if size_bytes <= 0:
            return None

        seeders = _safe_int(attrs.get("seeders"))
        peers = _safe_int(attrs.get("peers"))
        leechers = _safe_int(attrs.get("leechers"))
        if leechers == 0 and peers > seeders:
            leechers = peers - seeders

        parsed_name = parse_torrent_name(title)
        parsed_year = parsed_name.get("year")
        year = _safe_int(parsed_year) or None
        uploader = (
            attrs.get("uploader")
            or attrs.get("poster")
            or _direct_child_text(item, "author")
            or None
        )

        raw_data = {
            "guid": guid,
            "link": link,
            "comments": comments,
            "enclosure_url": enclosure_url,
            "attrs": dict(attrs),
        }

        try:
            return DiscoveryResult(
                title=title,
                download_url=download_url,
                source=self.config.name,
                size_bytes=size_bytes,
                seeders=seeders,
                leechers=leechers,
                info_url=comments or guid,
                magnet_url=magnet_url,
                info_hash=info_hash,
                uploader=uploader,
                year=year,
                codec=parse_codec(title),
                resolution=_resolution_from_title(title),
                raw_data=raw_data,
            )
        except ValueError as exc:
            logger.warning(
                "[DISCOVERY] %s: Dropping invalid Torznab item %r: %s",
                self.config.name,
                title,
                exc,
            )
            return None
