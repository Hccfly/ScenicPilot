from __future__ import annotations

import copy
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from scenic_pilot.tools.contracts import ToolPolicy, ToolProviderError


@dataclass(slots=True)
class _CacheEntry:
    value: dict[str, object]
    stored_at: float


@dataclass(slots=True)
class _CircuitState:
    consecutive_failures: int = 0
    open_until: float = 0.0


class ResilientToolRunner:
    """Synchronous retry/cache/circuit policy around timeout-aware providers."""

    def __init__(
        self,
        policy: ToolPolicy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_audit_events: int = 1000,
    ) -> None:
        self.policy = policy or ToolPolicy()
        self.policy.validate()
        self.clock = clock
        self.sleep = sleep
        self.now = now
        self._cache: dict[str, _CacheEntry] = {}
        self._circuits: dict[str, _CircuitState] = {}
        self._audit: deque[dict[str, Any]] = deque(maxlen=max_audit_events)
        self._lock = threading.RLock()

    @property
    def audit_events(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(copy.deepcopy(list(self._audit)))

    def execute(
        self,
        *,
        tool_name: str,
        cache_key: str,
        operation: Callable[[float], dict[str, object]] | None,
        fallback: Callable[[], dict[str, object]] | None = None,
    ) -> dict[str, object]:
        call_id = str(uuid.uuid4())
        started = self.clock()
        cached = self._cache_entry(cache_key)
        if cached and started - cached.stored_at <= self.policy.cache_ttl_seconds:
            return self._finish(
                cached.value,
                tool_name=tool_name,
                call_id=call_id,
                started=started,
                attempts=0,
                cache_status="hit",
                circuit_state="closed",
            )

        if operation is None:
            return self._degraded(
                tool_name=tool_name,
                call_id=call_id,
                started=started,
                attempts=0,
                reason="provider_not_configured",
                errors=[],
                cached=cached,
                fallback=fallback,
                circuit_state="disabled",
            )

        circuit = self._circuit(tool_name)
        if circuit.open_until > started:
            return self._degraded(
                tool_name=tool_name,
                call_id=call_id,
                started=started,
                attempts=0,
                reason="circuit_open",
                errors=[],
                cached=cached,
                fallback=fallback,
                circuit_state="open",
            )
        if circuit.open_until:
            circuit.open_until = 0.0
            circuit.consecutive_failures = 0

        errors: list[dict[str, object]] = []
        for attempt in range(1, self.policy.max_attempts + 1):
            try:
                value = operation(self.policy.timeout_seconds)
                if not isinstance(value, dict):
                    raise ToolProviderError(
                        "invalid_result", "tool provider did not return a mapping", retryable=False
                    )
                with self._lock:
                    self._cache[cache_key] = _CacheEntry(copy.deepcopy(value), self.clock())
                    circuit.consecutive_failures = 0
                    circuit.open_until = 0.0
                return self._finish(
                    value,
                    tool_name=tool_name,
                    call_id=call_id,
                    started=started,
                    attempts=attempt,
                    cache_status="miss",
                    circuit_state="closed",
                    errors=errors,
                )
            except ToolProviderError as error:
                errors.append(
                    {
                        "attempt": attempt,
                        "code": error.code,
                        "message": str(error),
                        "retryable": error.retryable,
                    }
                )
                if not error.retryable:
                    break
            except TimeoutError as error:
                errors.append(
                    {
                        "attempt": attempt,
                        "code": "timeout",
                        "message": str(error) or "tool call timed out",
                        "retryable": True,
                    }
                )
            except Exception as error:  # provider isolation boundary
                errors.append(
                    {
                        "attempt": attempt,
                        "code": "provider_exception",
                        "message": f"{type(error).__name__}: {error}",
                        "retryable": True,
                    }
                )
            if attempt < self.policy.max_attempts and self.policy.retry_backoff_seconds:
                self.sleep(self.policy.retry_backoff_seconds * attempt)

        with self._lock:
            circuit.consecutive_failures += 1
            if circuit.consecutive_failures >= self.policy.circuit_breaker_threshold:
                circuit.open_until = self.clock() + self.policy.circuit_reset_seconds
        return self._degraded(
            tool_name=tool_name,
            call_id=call_id,
            started=started,
            attempts=len(errors),
            reason=str(errors[-1]["code"]) if errors else "provider_failed",
            errors=errors,
            cached=cached,
            fallback=fallback,
            circuit_state="open" if circuit.open_until else "closed",
        )

    def _cache_entry(self, key: str) -> _CacheEntry | None:
        with self._lock:
            return self._cache.get(key)

    def _circuit(self, tool_name: str) -> _CircuitState:
        with self._lock:
            return self._circuits.setdefault(tool_name, _CircuitState())

    def _degraded(
        self,
        *,
        tool_name: str,
        call_id: str,
        started: float,
        attempts: int,
        reason: str,
        errors: list[dict[str, object]],
        cached: _CacheEntry | None,
        fallback: Callable[[], dict[str, object]] | None,
        circuit_state: str,
    ) -> dict[str, object]:
        now = self.clock()
        if cached and now - cached.stored_at <= self.policy.stale_ttl_seconds:
            value = copy.deepcopy(cached.value)
            value["status"] = "degraded"
            value["stale"] = True
            value["degradation_reason"] = reason
            return self._finish(
                value,
                tool_name=tool_name,
                call_id=call_id,
                started=started,
                attempts=attempts,
                cache_status="stale",
                circuit_state=circuit_state,
                errors=errors,
                degraded=True,
            )
        if fallback is not None:
            value = copy.deepcopy(fallback())
            value["fallback_status"] = value.get("status", "unknown")
            value["status"] = "degraded"
            value["degraded"] = True
            value["degradation_reason"] = reason
            return self._finish(
                value,
                tool_name=tool_name,
                call_id=call_id,
                started=started,
                attempts=attempts,
                cache_status="miss",
                circuit_state=circuit_state,
                errors=errors,
                degraded=True,
            )
        return self._finish(
            {
                "status": "unavailable",
                "provider": "none",
                "message": "工具当前不可用。",
                "degradation_reason": reason,
            },
            tool_name=tool_name,
            call_id=call_id,
            started=started,
            attempts=attempts,
            cache_status="miss",
            circuit_state=circuit_state,
            errors=errors,
            degraded=True,
        )

    def _finish(
        self,
        value: dict[str, object],
        *,
        tool_name: str,
        call_id: str,
        started: float,
        attempts: int,
        cache_status: str,
        circuit_state: str,
        errors: list[dict[str, object]] | None = None,
        degraded: bool = False,
    ) -> dict[str, object]:
        result = copy.deepcopy(value)
        trace = {
            "schema_version": "scenicpilot-tool-trace-v1",
            "call_id": call_id,
            "tool": tool_name,
            "attempts": attempts,
            "max_attempts": self.policy.max_attempts,
            "timeout_seconds": self.policy.timeout_seconds,
            "cache_status": cache_status,
            "circuit_state": circuit_state,
            "degraded": degraded,
            "latency_ms": round(max(0.0, self.clock() - started) * 1000, 3),
            "errors": errors or [],
        }
        result["tool_trace"] = trace
        event = {
            "observed_at": self.now().isoformat(),
            **trace,
            "status": result.get("status", "unknown"),
            "provider": result.get("provider", "unknown"),
        }
        with self._lock:
            self._audit.append(event)
        return result
