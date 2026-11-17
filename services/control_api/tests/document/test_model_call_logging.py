import json
import logging

import httpx
import pytest

from app.core.logging import JsonFormatter
from app.modules.document.openai_compatible import (
    OpenAICompatibleExtractionProvider,
    OpenAICompatibleVisionProvider,
)
from app.modules.document.providers import ProviderError
from tests.document.test_openai_compatible import sample_chunk


def _model_call_records(caplog):
    return [r for r in caplog.records if r.getMessage() == "model_call"]


async def test_rate_limited_call_is_logged_without_secrets(caplog):
    caplog.set_level(logging.INFO, logger="app.model")
    provider = OpenAICompatibleExtractionProvider(
        base_url="https://llm.test/v1",
        api_key="sk-very-secret",
        model="extract-model",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, json={"error": "slow down"})
        ),
    )

    with pytest.raises(ProviderError):
        await provider.extract([sample_chunk()], {})

    [record] = _model_call_records(caplog)
    assert record.purpose == "extract"
    assert record.model == "extract-model"
    assert record.status_code == 429
    assert record.error_code == "provider_rate_limited"
    assert record.retryable is True
    assert record.duration_ms >= 0
    rendered = "\n".join(JsonFormatter().format(r) for r in caplog.records)
    assert "sk-very-secret" not in rendered
    assert "Run status 40001" not in rendered


async def test_vision_call_logs_and_accumulates_usage(caplog):
    caplog.set_level(logging.INFO, logger="app.model")
    body = {
        "choices": [{"message": {"content": json.dumps({"blocks": []})}}],
        "usage": {"prompt_tokens": 1500, "completion_tokens": 20, "total_tokens": 1520},
    }
    provider = OpenAICompatibleVisionProvider(
        base_url="https://llm.test/v1",
        api_key="k",
        model="vision-model",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )

    await provider.extract_page(b"png", 1)
    await provider.extract_page(b"png", 2)

    records = _model_call_records(caplog)
    assert [r.purpose for r in records] == ["ocr", "ocr"]
    assert records[0].status_code == 200
    assert records[0].total_tokens == 1520
    assert provider.usage_totals == {
        "calls": 2,
        "prompt_tokens": 3000,
        "completion_tokens": 40,
        "total_tokens": 3040,
    }
