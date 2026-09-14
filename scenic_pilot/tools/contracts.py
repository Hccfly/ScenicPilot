from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol


ToolName = Literal["weather", "distance", "budget", "opening_hours"]


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    timeout_seconds: float = 3.0
    max_attempts: int = 2
    retry_backoff_seconds: float = 0.2
    cache_ttl_seconds: float = 600.0
    stale_ttl_seconds: float = 3600.0
    circuit_breaker_threshold: int = 3
    circuit_reset_seconds: float = 30.0

    def validate(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("tool timeout must be positive")
        if self.max_attempts <= 0:
            raise ValueError("tool max attempts must be positive")
        if self.retry_backoff_seconds < 0:
            raise ValueError("tool retry backoff cannot be negative")
        if self.cache_ttl_seconds < 0 or self.stale_ttl_seconds < 0:
            raise ValueError("tool cache TTL values cannot be negative")
        if self.stale_ttl_seconds < self.cache_ttl_seconds:
            raise ValueError("stale TTL must be greater than or equal to cache TTL")
        if self.circuit_breaker_threshold <= 0 or self.circuit_reset_seconds <= 0:
            raise ValueError("tool circuit breaker values must be positive")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ToolProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class WeatherProvider(Protocol):
    def fetch(
        self, *, location: str, date: str | None, timeout_seconds: float
    ) -> dict[str, object]: ...


class DistanceProvider(Protocol):
    def fetch(
        self, *, origin: str, destination: str, timeout_seconds: float
    ) -> dict[str, object]: ...


class WeatherTool(Protocol):
    def forecast(self, *, location: str, date: str | None = None) -> dict[str, object]: ...


class DistanceTool(Protocol):
    def estimate(self, *, origin: str, destination: str) -> dict[str, object]: ...


class BudgetTool(Protocol):
    def estimate(self, *, party_size: int, duration_minutes: int) -> dict[str, object]: ...


class OpeningHoursTool(Protocol):
    def check(self, *, venue: str, date: str | None = None) -> dict[str, object]: ...
