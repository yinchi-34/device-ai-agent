from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from app.modules.document.chunking import NormalizedChunk


@dataclass(frozen=True)
class VisionBlock:
    content: str
    content_type: str = "text"
    bounding_box: list[float] | None = None
    confidence: float | None = None
    cells: list[str] | None = None


@dataclass(frozen=True)
class ExtractionResult:
    payload: dict[str, Any]
    raw_response: dict[str, Any]
    usage: dict[str, Any]


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    provider: str
    model: str
    dimension: int
    usage: dict[str, Any]


class ProviderError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str = "AI provider request failed",
        *,
        retryable: bool = False,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code

    def __repr__(self) -> str:
        return (
            f"ProviderError(code={self.code!r}, retryable={self.retryable!r}, "
            f"status_code={self.status_code!r})"
        )


class ExtractionModelProvider(Protocol):
    async def extract(
        self, chunks: Sequence["NormalizedChunk"], context: dict[str, Any]
    ) -> ExtractionResult:
        raise NotImplementedError


class VisionOcrProvider(Protocol):
    async def extract_page(
        self, image_bytes: bytes, page_number: int
    ) -> list[VisionBlock]:
        raise NotImplementedError


class EmbeddingProvider(Protocol):
    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        raise NotImplementedError


class PdfPageRenderer(Protocol):
    def render_png(self, pdf_path: Path, page_number: int) -> bytes:
        raise NotImplementedError


@dataclass
class FakeExtractionProvider:
    result: ExtractionResult
    failures: list[ProviderError] = field(default_factory=list)
    calls: list[tuple[Sequence["NormalizedChunk"], dict[str, Any]]] = field(
        default_factory=list
    )

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def extract(
        self, chunks: Sequence["NormalizedChunk"], context: dict[str, Any]
    ) -> ExtractionResult:
        self.calls.append((chunks, context))
        if self.failures:
            raise self.failures.pop(0)
        return self.result


@dataclass
class FakeVisionOcrProvider:
    blocks: list[VisionBlock]
    failures: list[ProviderError] = field(default_factory=list)
    calls: list[tuple[bytes, int]] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def extract_page(
        self, image_bytes: bytes, page_number: int
    ) -> list[VisionBlock]:
        self.calls.append((image_bytes, page_number))
        if self.failures:
            raise self.failures.pop(0)
        return self.blocks


@dataclass
class FakeEmbeddingProvider:
    result: EmbeddingResult
    failures: list[ProviderError] = field(default_factory=list)
    calls: list[Sequence[str]] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        self.calls.append(texts)
        if self.failures:
            raise self.failures.pop(0)
        return self.result
