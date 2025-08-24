from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class TorrentData:
    """Structured information for a scraped torrent.

    Attributes:
        title: Human-friendly torrent title.
        magnet_url: Direct magnet link for the torrent.
        seeders: Number of seeders reported by the site.
        leechers: Number of leechers reported by the site.
        size_bytes: Total size of the torrent in bytes.
        source: Name of the site where the torrent was found.
        score: Aggregate score used for ranking results.
        uploader: Optional uploader name if provided by the source site.
        codec: Optional codec information parsed from the title.
        year: Optional release year parsed from the title.
    """

    title: str
    magnet_url: str
    seeders: int
    leechers: int
    size_bytes: int
    source: str
    score: int = 0
    uploader: Optional[str] = None
    codec: Optional[str] = None
    year: Optional[int] = None
