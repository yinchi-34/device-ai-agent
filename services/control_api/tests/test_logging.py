import json
import logging

from app.core import readiness
from app.core.logging import JsonFormatter, request_id_var


def _format(record_kwargs):
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=record_kwargs.pop("msg"),
        args=(),
        exc_info=None,
    )
    for key, value in record_kwargs.items():
        setattr(record, key, value)
    return json.loads(JsonFormatter().format(record))


def test_json_formatter_emits_one_object_with_extra_fields():
    token = request_id_var.set("req-123")
    try:
        line = _format({"msg": "model_call", "run_id": 7, "status_code": 429})
    finally:
        request_id_var.reset(token)

    assert line["event"] == "model_call"
    assert line["level"] == "info"
    assert line["logger"] == "app.test"
    assert line["run_id"] == 7
    assert line["status_code"] == 429
    assert line["request_id"] == "req-123"
    assert "ts" in line


async def test_requests_are_logged_with_request_id(client, caplog):
    caplog.set_level(logging.INFO, logger="app.http")

    response = await client.get("/health", headers={"X-Request-ID": "abc-123"})

    assert response.headers["X-Request-ID"] == "abc-123"
    records = [r for r in caplog.records if r.getMessage() == "http_request"]
    assert len(records) == 1
    record = records[0]
    assert record.method == "GET"
    assert record.path == "/health"
    assert record.status_code == 200
    assert record.duration_ms >= 0
    assert record.request_id == "abc-123"


async def test_request_id_is_generated_when_missing_or_unsafe(client):
    response = await client.get("/health", headers={"X-Request-ID": "bad id\n"})

    generated = response.headers["X-Request-ID"]
    assert generated != "bad id\n"
    assert len(generated) == 32


async def test_ready_reports_ok_when_dependencies_answer(client, monkeypatch):
    async def ok():
        return None

    monkeypatch.setattr(readiness, "check_redis", ok)

    response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok", "redis": "ok"},
    }


async def test_ready_returns_503_when_redis_is_down(client, monkeypatch):
    async def down():
        raise ConnectionError("refused")

    monkeypatch.setattr(readiness, "check_redis", down)

    response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "checks": {"database": "ok", "redis": "error"},
    }
