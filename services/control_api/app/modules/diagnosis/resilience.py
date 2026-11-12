"""Runtime-level reliability primitives for the diagnosis agent.

The agent loop treats *tool* failures as evidence (they are returned to the model
as observations), but *infrastructure* failures -- a model endpoint timing out,
a gateway hiccup -- are retried here, below the model, so they never consume the
model's step budget and never reach the audit trail as fake evidence.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from app.modules.diagnosis.contracts import (
    AgentDecision,
    DiagnosisInput,
    DiagnosisModelProvider,
    DiagnosisStep,
    ToolDefinition,
    WorkingMemory,
)
from app.modules.diagnosis.gateway import ReadOnlyRegisterGateway, ReadPointRequest

Sleep = Callable[[float], Awaitable[None]]


class ModelProviderError(RuntimeError):
    """A classified model-call failure.

    ``retryable`` separates transient faults (timeouts, 429, 5xx, a malformed
    sample) from faults that will fail identically on retry (bad request, auth).
    """

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        retry_after_seconds: float | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.detail = detail or {}


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")

    def delay_for(
        self,
        attempt: int,
        *,
        retry_after_seconds: float | None = None,
        rand: Callable[[], float] = random.random,
    ) -> float:
        """Exponential backoff with full jitter, honouring Retry-After."""
        cap = min(self.max_delay_seconds, self.base_delay_seconds * 2 ** (attempt - 1))
        delay = rand() * cap
        if retry_after_seconds is not None:
            delay = max(delay, min(retry_after_seconds, self.max_delay_seconds))
        return delay


class CircuitBreaker:
    """Consecutive-failure breaker: closed -> open -> half-open (one probe)."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.clock = clock
        self.consecutive_failures = 0
        self.opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if self.clock() - self.opened_at >= self.cooldown_seconds:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        state = self.state
        if state == "closed":
            return True
        if state == "half_open" and not self._probe_in_flight:
            self._probe_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self._probe_in_flight or self.consecutive_failures >= self.failure_threshold:
            self.opened_at = self.clock()
        self._probe_in_flight = False


_BREAKERS: dict[str, CircuitBreaker] = {}


def shared_breaker(
    key: str, *, failure_threshold: int, cooldown_seconds: float
) -> CircuitBreaker:
    """Process-local breaker registry.

    Providers are built per request, so breaker state must outlive them. With
    several worker processes each process keeps its own view; that is an accepted
    trade-off versus a shared store for a single-tenant deployment.
    """
    breaker = _BREAKERS.get(key)
    if breaker is None:
        breaker = CircuitBreaker(
            failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds
        )
        _BREAKERS[key] = breaker
    return breaker


def reset_shared_breakers() -> None:
    _BREAKERS.clear()


@dataclass(frozen=True)
class ModelRoute:
    name: str
    provider: DiagnosisModelProvider
    breaker: CircuitBreaker


class ResilientDiagnosisProvider:
    """Retry with backoff on the primary model, then degrade to fallbacks."""

    provider_name = "resilient"

    def __init__(
        self,
        routes: Sequence[ModelRoute],
        *,
        retry_policy: RetryPolicy | None = None,
        sleep: Sleep = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        if not routes:
            raise ValueError("at least one model route is required")
        self.routes = list(routes)
        self.retry_policy = retry_policy or RetryPolicy()
        self.sleep = sleep
        self.rand = rand

    async def decide(
        self,
        *,
        diagnosis: DiagnosisInput,
        steps: Sequence[DiagnosisStep],
        tools: Sequence[ToolDefinition],
        memory: WorkingMemory | None = None,
    ) -> AgentDecision:
        attempts: list[dict[str, Any]] = []
        for route_index, route in enumerate(self.routes):
            for attempt in range(1, self.retry_policy.max_attempts + 1):
                if not route.breaker.allow():
                    attempts.append({"route": route.name, "error_code": "circuit_open"})
                    break
                try:
                    decision = await route.provider.decide(
                        diagnosis=diagnosis, steps=steps, tools=tools, memory=memory
                    )
                except ModelProviderError as exc:
                    attempts.append(
                        {"route": route.name, "attempt": attempt, "error_code": exc.code}
                    )
                    if not exc.retryable:
                        # Client-side faults say nothing about availability.
                        break
                    route.breaker.record_failure()
                    if attempt == self.retry_policy.max_attempts:
                        break
                    await self.sleep(
                        self.retry_policy.delay_for(
                            attempt,
                            retry_after_seconds=exc.retry_after_seconds,
                            rand=self.rand,
                        )
                    )
                    continue
                route.breaker.record_success()
                return replace(
                    decision,
                    usage={
                        **decision.usage,
                        "model_route": route.name,
                        "model_attempts": len(attempts) + 1,
                        "fallback_used": route_index > 0,
                    },
                )
        raise ModelProviderError(
            "diagnosis_model_unavailable",
            retryable=False,
            detail={"attempts": attempts},
        )


_TRANSIENT_GATEWAY_ERRORS = {"gateway_connection_error", "gateway_unavailable"}


def is_transient_read_result(result: dict[str, Any]) -> bool:
    status = result.get("status")
    if status == "timeout":
        return True
    return status == "gateway_error" and result.get("error_code") in (
        _TRANSIENT_GATEWAY_ERRORS
    )


class RetryingReadGateway:
    """Retries idempotent point reads below the model, behind a breaker.

    Only transient results are retried. ``connection_refused`` and Modbus
    exceptions are deterministic device answers and are returned immediately.
    When the gateway itself keeps failing, the breaker opens and reads degrade to
    an explicit ``tool_unavailable`` observation, which steers the model towards
    escalation instead of burning its budget.
    """

    def __init__(
        self,
        inner: ReadOnlyRegisterGateway,
        *,
        retry_policy: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
        sleep: Sleep = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self.inner = inner
        self.retry_policy = retry_policy or RetryPolicy(
            max_attempts=2, base_delay_seconds=0.2, max_delay_seconds=2.0
        )
        self.breaker = breaker or CircuitBreaker()
        self.sleep = sleep
        self.rand = rand

    async def read_point(self, request: ReadPointRequest) -> dict[str, Any]:
        result: dict[str, Any] = {}
        attempt = 0
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            if not self.breaker.allow():
                return {
                    "status": "tool_unavailable",
                    "error_code": "read_gateway_circuit_open",
                    "gateway_attempts": attempt - 1,
                }
            result = await self.inner.read_point(request)
            if result.get("status") == "gateway_error" and is_transient_read_result(
                result
            ):
                self.breaker.record_failure()
            else:
                self.breaker.record_success()
            if not is_transient_read_result(result):
                break
            if attempt < self.retry_policy.max_attempts:
                await self.sleep(self.retry_policy.delay_for(attempt, rand=self.rand))
        return {**result, "gateway_attempts": attempt}
