from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class ProviderHealthState:
    provider_name: str
    failures: int = 0
    last_failure_time: float = 0.0
    is_offline: bool = False


class CircuitBreaker:
    """Tracks provider failures so unhealthy indexers can cool down."""

    FAILURE_THRESHOLD = 3
    COOLDOWN_SECONDS = 15 * 60

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._states: dict[str, ProviderHealthState] = {}

    def _get_state(self, provider_name: str) -> ProviderHealthState:
        state = self._states.get(provider_name)
        if state is None:
            state = ProviderHealthState(provider_name=provider_name)
            self._states[provider_name] = state
        return state

    def is_healthy(self, provider_name: str) -> bool:
        state = self._get_state(provider_name)
        if not state.is_offline:
            return True

        if self._clock() - state.last_failure_time >= self.COOLDOWN_SECONDS:
            state.is_offline = False
            state.failures = 0
            return True

        return False

    def record_success(self, provider_name: str) -> None:
        state = self._get_state(provider_name)
        state.failures = 0
        state.is_offline = False

    def record_failure(self, provider_name: str) -> None:
        state = self._get_state(provider_name)
        state.failures += 1
        state.last_failure_time = self._clock()
        if state.failures >= self.FAILURE_THRESHOLD:
            state.is_offline = True
