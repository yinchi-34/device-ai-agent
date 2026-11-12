import httpx
import pytest

from app.modules.diagnosis.contracts import (
    AgentDecision,
    DiagnosisInput,
    DiagnosisStep,
    ToolDefinition,
)
from app.modules.diagnosis.gateway import ReadPointRequest
from app.modules.diagnosis.openai_compatible import OpenAICompatibleDiagnosisProvider
from app.modules.diagnosis.resilience import (
    CircuitBreaker,
    ModelProviderError,
    ModelRoute,
    ResilientDiagnosisProvider,
    RetryingReadGateway,
    RetryPolicy,
)
from app.modules.diagnosis.tools import repeated_read_policy_error

_INPUT = DiagnosisInput(device_id=1, symptom="offline", max_steps=4)
_TOOLS = [ToolDefinition("get_device_context", "ctx", {"type": "object"})]


class ScriptedProvider:
    """Raises the scripted errors in order, then succeeds."""

    def __init__(self, *errors: ModelProviderError) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def decide(self, *, diagnosis, steps, tools, memory=None):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return AgentDecision("get_device_context", {}, usage={"total_tokens": 5})


class RecordingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _route(name, provider, **breaker_kwargs):
    return ModelRoute(name, provider, CircuitBreaker(**breaker_kwargs))


def _transient(code="model_timeout", **kwargs):
    return ModelProviderError(code, retryable=True, **kwargs)


async def _decide(provider):
    return await provider.decide(diagnosis=_INPUT, steps=[], tools=_TOOLS)


# --- backoff -----------------------------------------------------------------


def test_backoff_is_exponential_capped_and_jittered():
    policy = RetryPolicy(max_attempts=5, base_delay_seconds=1, max_delay_seconds=4)

    assert [policy.delay_for(n, rand=lambda: 1.0) for n in (1, 2, 3, 4)] == [
        1,
        2,
        4,
        4,
    ]
    assert policy.delay_for(3, rand=lambda: 0.25) == 1.0


def test_backoff_honours_retry_after_but_never_beyond_the_cap():
    policy = RetryPolicy(base_delay_seconds=0.5, max_delay_seconds=8)

    assert policy.delay_for(1, retry_after_seconds=3, rand=lambda: 0.0) == 3
    assert policy.delay_for(1, retry_after_seconds=60, rand=lambda: 0.0) == 8


# --- model retries and fallback ---------------------------------------------


async def test_transient_model_errors_are_retried_without_costing_agent_steps():
    primary = ScriptedProvider(_transient(), _transient("model_server_error"))
    sleep = RecordingSleep()
    provider = ResilientDiagnosisProvider(
        [_route("primary", primary)],
        retry_policy=RetryPolicy(max_attempts=3),
        sleep=sleep,
        rand=lambda: 1.0,
    )

    decision = await _decide(provider)

    assert primary.calls == 3
    assert sleep.delays == [0.5, 1.0]
    assert decision.usage == {
        "total_tokens": 5,
        "model_route": "primary",
        "model_attempts": 3,
        "fallback_used": False,
    }


async def test_non_retryable_error_skips_retries_and_degrades_to_fallback():
    primary = ScriptedProvider(
        ModelProviderError("model_request_rejected", retryable=False)
    )
    fallback = ScriptedProvider()
    provider = ResilientDiagnosisProvider(
        [_route("primary", primary), _route("fallback", fallback)],
        sleep=RecordingSleep(),
    )

    decision = await _decide(provider)

    assert primary.calls == 1
    assert fallback.calls == 1
    assert decision.usage["fallback_used"] is True
    assert decision.usage["model_route"] == "fallback"


async def test_exhausted_routes_raise_one_classified_error_with_attempt_log():
    provider = ResilientDiagnosisProvider(
        [_route("primary", ScriptedProvider(*[_transient()] * 5))],
        retry_policy=RetryPolicy(max_attempts=2),
        sleep=RecordingSleep(),
    )

    with pytest.raises(ModelProviderError) as excinfo:
        await _decide(provider)

    assert excinfo.value.code == "diagnosis_model_unavailable"
    assert excinfo.value.detail["attempts"] == [
        {"route": "primary", "attempt": 1, "error_code": "model_timeout"},
        {"route": "primary", "attempt": 2, "error_code": "model_timeout"},
    ]


async def test_open_breaker_fails_fast_and_routes_straight_to_fallback():
    primary = ScriptedProvider(*[_transient()] * 10)
    fallback = ScriptedProvider()
    primary_route = _route("primary", primary, failure_threshold=2, cooldown_seconds=60)
    provider = ResilientDiagnosisProvider(
        [primary_route, _route("fallback", fallback)],
        retry_policy=RetryPolicy(max_attempts=3),
        sleep=RecordingSleep(),
    )

    await _decide(provider)  # two failures open the primary breaker
    assert primary_route.breaker.state == "open"
    calls_before = primary.calls

    await _decide(provider)

    assert primary.calls == calls_before
    assert fallback.calls == 2


def test_breaker_half_opens_after_cooldown_and_allows_a_single_probe():
    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=1, cooldown_seconds=10, clock=lambda: now[0]
    )
    breaker.record_failure()
    assert not breaker.allow()

    now[0] = 10.0
    assert breaker.allow()
    assert not breaker.allow()  # only one probe while half-open
    breaker.record_failure()
    assert breaker.state == "open"

    now[0] = 20.0
    assert breaker.allow()
    breaker.record_success()
    assert breaker.state == "closed"


async def test_non_retryable_probe_releases_a_half_open_breaker():
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=5, clock=lambda: now[0])
    breaker.record_failure()
    now[0] = 5.0
    provider = ResilientDiagnosisProvider(
        [
            ModelRoute(
                "primary",
                ScriptedProvider(
                    ModelProviderError("model_request_rejected", retryable=False)
                ),
                breaker,
            )
        ],
        sleep=RecordingSleep(),
    )

    with pytest.raises(ModelProviderError):
        await _decide(provider)

    assert breaker.state == "closed"
    assert breaker.allow()


# --- provider error classification -------------------------------------------


@pytest.mark.parametrize(
    ("response", "code", "retryable"),
    [
        (httpx.Response(429, headers={"retry-after": "2"}), "model_rate_limited", True),
        (httpx.Response(503), "model_server_error", True),
        (httpx.Response(400), "model_request_rejected", False),
        (httpx.Response(401), "model_request_rejected", False),
        (httpx.Response(200, json={"choices": []}), "model_malformed_response", True),
    ],
)
async def test_openai_provider_classifies_failures(response, code, retryable):
    provider = OpenAICompatibleDiagnosisProvider(
        base_url="https://model.test/v1",
        api_key="secret",
        model="m",
        transport=httpx.MockTransport(lambda request: response),
    )

    with pytest.raises(ModelProviderError) as excinfo:
        await _decide(provider)

    assert excinfo.value.code == code
    assert excinfo.value.retryable is retryable
    if code == "model_rate_limited":
        assert excinfo.value.retry_after_seconds == 2


async def test_openai_provider_classifies_timeouts_as_retryable():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    provider = OpenAICompatibleDiagnosisProvider(
        base_url="https://model.test/v1",
        api_key="secret",
        model="m",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ModelProviderError) as excinfo:
        await _decide(provider)

    assert (excinfo.value.code, excinfo.value.retryable) == ("model_timeout", True)


# --- gateway retries ---------------------------------------------------------


class ScriptedGateway:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    async def read_point(self, request):
        self.calls += 1
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


_REQUEST = ReadPointRequest(
    device_id=1,
    connection_id=2,
    protocol="modbus_tcp",
    point_code="temperature",
    address_text="40001",
    address_spec={},
    data_type="uint16",
    access_mode="R",
    connection_config={},
    secret_ref=None,
)


async def test_gateway_retries_transient_faults_below_the_model():
    inner = ScriptedGateway(
        {"status": "gateway_error", "error_code": "gateway_unavailable"},
        {"status": "success", "raw_words": [1]},
    )
    gateway = RetryingReadGateway(inner, sleep=RecordingSleep())

    result = await gateway.read_point(_REQUEST)

    assert inner.calls == 2
    assert result == {"status": "success", "raw_words": [1], "gateway_attempts": 2}


@pytest.mark.parametrize("status", ["connection_refused", "modbus_exception"])
async def test_gateway_never_retries_deterministic_device_answers(status):
    inner = ScriptedGateway({"status": status})
    gateway = RetryingReadGateway(inner, sleep=RecordingSleep())

    result = await gateway.read_point(_REQUEST)

    assert inner.calls == 1
    assert result["gateway_attempts"] == 1


async def test_gateway_breaker_degrades_reads_to_tool_unavailable():
    inner = ScriptedGateway(
        {"status": "gateway_error", "error_code": "gateway_connection_error"}
    )
    gateway = RetryingReadGateway(
        inner,
        retry_policy=RetryPolicy(max_attempts=2),
        breaker=CircuitBreaker(failure_threshold=2, cooldown_seconds=60),
        sleep=RecordingSleep(),
    )

    await gateway.read_point(_REQUEST)
    degraded = await gateway.read_point(_REQUEST)

    assert inner.calls == 2
    assert degraded["status"] == "tool_unavailable"
    assert degraded["error_code"] == "read_gateway_circuit_open"


# --- model-level repeat guard -------------------------------------------------


def _read_step(sequence, result):
    return DiagnosisStep(
        sequence_number=sequence,
        tool_name="read_point",
        arguments={"connection_id": 2, "point_code": "temperature"},
        rationale="",
        result=result,
    )


def test_transient_gateway_error_is_bounded_like_a_timeout():
    arguments = {"connection_id": 2, "point_code": "temperature"}
    transient = {"status": "gateway_error", "error_code": "gateway_unavailable"}

    assert repeated_read_policy_error(arguments, [_read_step(1, transient)]) is None
    blocked = repeated_read_policy_error(
        arguments, [_read_step(n, transient) for n in (1, 2, 3)]
    )
    assert blocked["error_code"] == "read_retry_limit_reached"


@pytest.mark.parametrize(
    "result",
    [
        {"status": "gateway_error", "error_code": "gateway_request_rejected"},
        {"status": "tool_unavailable", "error_code": "read_gateway_circuit_open"},
    ],
)
def test_non_transient_failures_are_not_repeated(result):
    blocked = repeated_read_policy_error(
        {"connection_id": 2, "point_code": "temperature"}, [_read_step(1, result)]
    )

    assert blocked["error_code"] == "deterministic_read_result"


# --- wiring ----------------------------------------------------------------


def test_router_builds_primary_and_fallback_routes(monkeypatch):
    from app.modules.diagnosis.router import (
        build_diagnosis_provider,
        build_read_gateway,
    )

    monkeypatch.setenv("DEVICE_DIAGNOSIS_LLM_MODEL", "big-model")
    monkeypatch.setenv("DEVICE_DIAGNOSIS_LLM_FALLBACK_MODEL", "small-model")
    monkeypatch.setenv("DEVICE_DIAGNOSIS_READ_GATEWAY_URL", "http://gateway.test")

    provider = build_diagnosis_provider()

    assert [route.name for route in provider.routes] == [
        "primary:big-model",
        "fallback:small-model",
    ]
    assert isinstance(build_read_gateway(), RetryingReadGateway)
