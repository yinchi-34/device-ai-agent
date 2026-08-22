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
        assert body["response_format"] == {"type": "json_object"}
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
        image = body["messages"][0]["content"][1]["image_url"]["url"]
        assert image.startswith("data:image/png;base64,")
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
