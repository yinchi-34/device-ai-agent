import json

import httpx
import pytest

from app.modules.document.chunking import NormalizedChunk
from app.modules.document.openai_compatible import (
    OpenAICompatibleEmbeddingProvider,
    OpenAICompatibleExtractionProvider,
    OpenAICompatibleVisionProvider,
)
from app.modules.document.providers import ProviderError


def sample_chunk():
    return NormalizedChunk(
        stable_ref="pdf:page:1:block:1",
        content_type="text",
        content="Run status 40001",
        page_number=1,
        block_index=1,
        bounding_box=None,
        table_metadata={},
        extraction_method="pdf_text",
        extraction_confidence=None,
        content_hash="a" * 64,
        token_count=5,
    )


async def test_extraction_adapter_returns_json_and_usage():
    def fake_chat_completion(request):
        assert request.headers["Authorization"] == "Bearer secret"
        body = json.loads(request.content)
        assert body["model"] == "extract"
        response_format = body["response_format"]
        assert body["max_tokens"] == 8192
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["strict"] is True
        schema = response_format["json_schema"]["schema"]
        user_payload = json.loads(body["messages"][1]["content"])
        serialized_schema = json.dumps(schema)
        assert "\"$defs\"" not in serialized_schema
        assert "\"$ref\"" not in serialized_schema
        assert "EVERY distinct" in body["messages"][0]["content"]
        assert "protocol point" in body["messages"][0]["content"]
        assert set(schema["required"]) == {
            "schema_version",
            "device_model",
            "profile",
            "protocols",
            "points",
            "capability_matches",
            "point_bindings",
        }
        assert schema["properties"]["schema_version"]["const"] == "1.0"
        assert user_payload["domain_constraints"] == {
            "protocol_values": [
                "simulator",
                "modbus_tcp",
                "modbus_rtu",
                "bacnet_ip",
                "mqtt",
                "opcua",
            ],
            "access_mode_values": ["R", "W", "RW"],
            "point_role_values": [
                "control",
                "status",
                "verify",
                "measurement",
                "input",
                "output",
            ],
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"schema_version": "1.0"})
                        }
                    }
                ],
                "usage": {"total_tokens": 42},
            },
        )

    provider = OpenAICompatibleExtractionProvider(
        base_url="https://example.test/v1",
        api_key="secret",
        model="extract",
        transport=httpx.MockTransport(fake_chat_completion),
    )

    result = await provider.extract(chunks=[sample_chunk()], context={})

    assert result.payload["schema_version"] == "1.0"
    assert result.usage["total_tokens"] == 42


async def test_api_key_is_never_in_provider_error():
    def fail(request):
        raise httpx.ConnectError("secret-key network failure", request=request)

    provider = OpenAICompatibleExtractionProvider(
        base_url="https://example.test/v1",
        api_key="secret-key",
        model="extract",
        transport=httpx.MockTransport(fail),
    )

    with pytest.raises(ProviderError) as excinfo:
        await provider.extract(chunks=[sample_chunk()], context={})

    assert excinfo.value.code == "provider_connection_error"
    assert "secret-key" not in str(excinfo.value)
    assert "secret-key" not in repr(excinfo.value)


async def test_embedding_adapter_rejects_dimension_mismatch():
    def fake_embeddings(request):
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 0, "embedding": [1.0, 0.0]},
                    {"index": 1, "embedding": [1.0, 0.0, 0.0]},
                ],
                "usage": {},
            },
        )

    provider = OpenAICompatibleEmbeddingProvider(
        base_url="https://example.test/v1",
        api_key="embed-secret",
        model="embed",
        transport=httpx.MockTransport(fake_embeddings),
    )

    with pytest.raises(ProviderError) as excinfo:
        await provider.embed(["first", "second"])

    assert excinfo.value.code == "provider_embedding_dimension_mismatch"


async def test_vision_adapter_parses_blocks_and_sends_data_url():
    def fake_vision(request):
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["response_format"]["json_schema"]["name"] == "vision_blocks"
        image = body["messages"][0]["content"][1]["image_url"]["url"]
        assert image.startswith("data:image/png;base64,")
        prompt = body["messages"][0]["content"][0]["text"]
        assert '"blocks"' in prompt
        assert '"content"' in prompt
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "blocks": [
                                        {
                                            "content": "Run 40001",
                                            "bounding_box": [1, 2, 3, 4],
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    provider = OpenAICompatibleVisionProvider(
        base_url="https://example.test/v1",
        api_key="vision-secret",
        model="vision",
        transport=httpx.MockTransport(fake_vision),
    )

    blocks = await provider.extract_page(b"png", 3)

    assert blocks[0].content == "Run 40001"
    assert blocks[0].bounding_box == [1, 2, 3, 4]


async def test_vision_adapter_normalizes_common_block_aliases():
    def fake_vision(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "blocks": [
                                        {
                                            "type": "table",
                                            "text": "Offset 1 Voltage L1-N",
                                            "bbox": [1, 2, 3, 4],
                                            "cells": ["1", "Voltage L1-N"],
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            },
        )

    provider = OpenAICompatibleVisionProvider(
        base_url="https://example.test/v1",
        api_key="vision-secret",
        model="vision",
        transport=httpx.MockTransport(fake_vision),
    )

    blocks = await provider.extract_page(b"png", 3)

    assert blocks[0].content == "Offset 1 Voltage L1-N"
    assert blocks[0].content_type == "table"
    assert blocks[0].bounding_box == [1, 2, 3, 4]


async def test_malformed_vision_response_is_retryable():
    def fake_vision(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"unexpected": []})}}
                ]
            },
        )

    provider = OpenAICompatibleVisionProvider(
        base_url="https://example.test/v1",
        api_key="vision-secret",
        model="vision",
        transport=httpx.MockTransport(fake_vision),
    )

    with pytest.raises(ProviderError) as excinfo:
        await provider.extract_page(b"png", 3)

    assert excinfo.value.code == "provider_malformed_vision_response"
    assert excinfo.value.retryable is True


async def test_extraction_adapter_uses_fragment_schema_for_grouped_agent():
    def fake_fragment(request):
        body = json.loads(request.content)
        response_format = body["response_format"]["json_schema"]
        assert response_format["name"] == "profile_candidate_fragment"
        schema = response_format["schema"]
        assert "null" in {
            item.get("type")
            for item in schema["properties"]["device_model"]["anyOf"]
        }
        assert "untrusted" in body["messages"][0]["content"].lower()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "schema_version": "1.0",
                                    "device_model": None,
                                    "profile": None,
                                    "protocols": [],
                                    "points": [],
                                    "capability_matches": [],
                                    "point_bindings": [],
                                    "unresolved_suggestions": [],
                                }
                            )
                        }
                    }
                ],
                "usage": {"total_tokens": 12},
            },
        )

    provider = OpenAICompatibleExtractionProvider(
        base_url="https://example.test/v1",
        api_key="secret",
        model="extract",
        transport=httpx.MockTransport(fake_fragment),
    )

    result = await provider.extract(
        chunks=[sample_chunk()],
        context={
            "extraction_mode": "fragment",
            "group_index": 1,
            "group_count": 2,
        },
    )

    assert result.payload["device_model"] is None


async def test_malformed_extraction_json_is_retryable():
    def malformed(request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "{\"schema_version\":"}}],
                "usage": {},
            },
        )

    provider = OpenAICompatibleExtractionProvider(
        base_url="https://example.test/v1",
        api_key="secret",
        model="extract",
        transport=httpx.MockTransport(malformed),
    )

    with pytest.raises(ProviderError) as excinfo:
        await provider.extract(chunks=[sample_chunk()], context={})

    assert excinfo.value.code == "provider_malformed_json"
    assert excinfo.value.retryable is True


async def test_fragment_payload_missing_required_keys_is_retryable():
    def invalid_fragment(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"schema_version": "1.0"})
                        }
                    }
                ],
                "usage": {},
            },
        )

    provider = OpenAICompatibleExtractionProvider(
        base_url="https://example.test/v1",
        api_key="secret",
        model="extract",
        transport=httpx.MockTransport(invalid_fragment),
    )

    with pytest.raises(ProviderError) as excinfo:
        await provider.extract(
            chunks=[sample_chunk()],
            context={"extraction_mode": "fragment"},
        )

    assert excinfo.value.code == "provider_invalid_fragment"
    assert excinfo.value.retryable is True
