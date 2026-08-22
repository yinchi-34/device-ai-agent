import base64
import json
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

import httpx

from app.modules.document.chunking import NormalizedChunk
from app.modules.document.providers import (
    EmbeddingResult,
    ExtractionResult,
    ProviderError,
    VisionBlock,
)


class _OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 120,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.post(path, json=payload)
        except httpx.TimeoutException:
            raise ProviderError(
                "provider_timeout", retryable=True
            ) from None
        except httpx.TransportError:
            raise ProviderError(
                "provider_connection_error", retryable=True
            ) from None
        if response.status_code == 429:
            raise ProviderError(
                "provider_rate_limited",
                retryable=True,
                status_code=response.status_code,
            )
        if response.status_code >= 500:
            raise ProviderError(
                "provider_unavailable",
                retryable=True,
                status_code=response.status_code,
            )
        if response.is_error:
            raise ProviderError(
                "provider_request_rejected", status_code=response.status_code
            )
        try:
            value = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ProviderError("provider_malformed_response") from None
        if not isinstance(value, dict):
            raise ProviderError("provider_malformed_response")
        return value


def _chat_content(response: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ProviderError("provider_malformed_response") from None
    if not isinstance(content, str):
        raise ProviderError("provider_malformed_response")
    usage = response.get("usage", {})
    return content, usage if isinstance(usage, dict) else {}


class OpenAICompatibleExtractionProvider(_OpenAICompatibleProvider):
    async def extract(
        self, chunks: Sequence[NormalizedChunk], context: dict[str, Any]
    ) -> ExtractionResult:
        response = await self._post(
            "/chat/completions",
            {
                "model": self.model,
                "response_format": {"type": "json_object"},
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Extract one ProfileCandidatePayload JSON object. "
                            "Use only the supplied evidence references."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "context": context,
                                "chunks": [asdict(chunk) for chunk in chunks],
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
            },
        )
        content, usage = _chat_content(response)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            raise ProviderError("provider_malformed_json") from None
        if not isinstance(payload, dict):
            raise ProviderError("provider_malformed_json")
        return ExtractionResult(
            payload=payload, raw_response=response, usage=usage
        )


class OpenAICompatibleVisionProvider(_OpenAICompatibleProvider):
    async def extract_page(
        self, image_bytes: bytes, page_number: int
    ) -> list[VisionBlock]:
        image_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode()
        response = await self._post(
            "/chat/completions",
            {
                "model": self.model,
                "response_format": {"type": "json_object"},
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    f"Extract ordered text/table blocks from page "
                                    f"{page_number} as JSON blocks."
                                ),
                            },
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    }
                ],
            },
        )
        content, _ = _chat_content(response)
        try:
            payload = json.loads(content)
            blocks = payload["blocks"]
            return [VisionBlock(**block) for block in blocks]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            raise ProviderError("provider_malformed_vision_response") from None


class OpenAICompatibleEmbeddingProvider(_OpenAICompatibleProvider):
    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        response = await self._post(
            "/embeddings", {"model": self.model, "input": list(texts)}
        )
        try:
            data = sorted(response["data"], key=lambda item: item["index"])
            vectors = [item["embedding"] for item in data]
            dimensions = {len(vector) for vector in vectors}
        except (KeyError, TypeError):
            raise ProviderError("provider_malformed_embedding_response") from None
        if len(vectors) != len(texts):
            raise ProviderError("provider_embedding_count_mismatch")
        if len(dimensions) != 1:
            raise ProviderError("provider_embedding_dimension_mismatch")
        dimension = next(iter(dimensions), 0)
        if any(
            not isinstance(value, (float, int))
            for vector in vectors
            for value in vector
        ):
            raise ProviderError("provider_malformed_embedding_response")
        usage = response.get("usage", {})
        return EmbeddingResult(
            vectors=[[float(value) for value in vector] for vector in vectors],
            provider="openai_compatible",
            model=self.model,
            dimension=dimension,
            usage=usage if isinstance(usage, dict) else {},
        )
