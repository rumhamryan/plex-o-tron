from __future__ import annotations


class ProviderSearchError(RuntimeError):
    """Raised when a discovery provider cannot complete a search."""

    def __init__(self, message: str, *, provider_name: str | None = None) -> None:
        super().__init__(message)
        self.provider_name = provider_name
