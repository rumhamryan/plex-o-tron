from __future__ import annotations

from abc import ABC, abstractmethod

from ..schemas import DiscoveryRequest, DiscoveryResult, ProviderConfig


class BaseProvider(ABC):
    """Base class for torrent discovery providers."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @abstractmethod
    async def search(self, request: DiscoveryRequest) -> list[DiscoveryResult]:
        """Execute a provider search and return normalized torrent candidates."""

    def health_check(self) -> bool:
        """Optional lightweight health signal for providers that can support it."""
        return True
