from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class VisionBlock:
    content: str
    content_type: str = "text"
    bounding_box: list[float] | None = None
    confidence: float | None = None
    cells: list[str] | None = None


class VisionOcrProvider(Protocol):
    async def extract_page(
        self, image_bytes: bytes, page_number: int
    ) -> list[VisionBlock]:
        raise NotImplementedError


class PdfPageRenderer(Protocol):
    def render_png(self, pdf_path: Path, page_number: int) -> bytes:
        raise NotImplementedError
