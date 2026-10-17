import asyncio
import json
import os
import threading
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol
from unicodedata import category
from xml.etree.ElementTree import Element
from zipfile import ZipFile

import pypdfium2 as pdfium
from defusedxml import ElementTree
from pypdf import PdfReader

from app.modules.document.providers import (
    PdfPageRenderer,
    ProviderError,
    VisionBlock,
    VisionOcrProvider,
)

_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f"{{{_WORD_NS}}}"
# PDFium is not thread-safe; OCR pages render from worker threads.
_PDFIUM_LOCK = threading.Lock()


@dataclass(frozen=True)
class ParsedBlock:
    content: str
    content_type: str = "text"
    page_number: int | None = None
    extraction_method: str = "pdf_text"
    bounding_box: list[float] | None = None
    confidence: float | None = None
    cells: list[str] | None = None
    table_index: int | None = None
    row_index: int | None = None


@dataclass(frozen=True)
class ParsedDocument:
    format: str
    blocks: list[ParsedBlock] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class Pypdfium2PageRenderer:
    def __init__(self, *, scale: float = 2.0, max_image_side_px: int = 2500):
        self.scale = scale
        self.max_image_side_px = max_image_side_px

    def render_png(self, pdf_path: Path, page_number: int) -> bytes:
        with _PDFIUM_LOCK:
            return self._render_png(pdf_path, page_number)

    def _render_png(self, pdf_path: Path, page_number: int) -> bytes:
        document = pdfium.PdfDocument(pdf_path)
        try:
            page = document[page_number - 1]
            try:
                longest_side = max(page.get_size())
                scale = self.scale
                if longest_side > 0:
                    scale = min(scale, self.max_image_side_px / longest_side)
                image = page.render(scale=scale).to_pil()
                stream = BytesIO()
                image.save(stream, format="PNG")
                return stream.getvalue()
            finally:
                page.close()
        finally:
            document.close()


def _element_text(element: Element) -> str:
    return "".join(node.text or "" for node in element.iter(f"{_W}t")).strip()


def parse_docx(path: Path) -> ParsedDocument:
    blocks: list[ParsedBlock] = []
    table_index = 0
    with ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    body = root.find(f"{_W}body")
    if body is None:
        return ParsedDocument(format="docx", metadata={"block_count": 0})
    for element in body:
        if element.tag == f"{_W}p":
            content = _element_text(element)
            if not content:
                continue
            style = element.find(f"{_W}pPr/{_W}pStyle")
            style_name = style.attrib.get(f"{_W}val", "") if style is not None else ""
            blocks.append(
                ParsedBlock(
                    content=content,
                    content_type=(
                        "heading" if style_name.lower().startswith("heading") else "text"
                    ),
                    extraction_method="docx_xml",
                )
            )
        elif element.tag == f"{_W}tbl":
            table_index += 1
            for row_index, row in enumerate(element.findall(f"{_W}tr"), start=1):
                cells = [_element_text(cell) for cell in row.findall(f"{_W}tc")]
                blocks.append(
                    ParsedBlock(
                        content=" | ".join(cells),
                        content_type="table_row",
                        extraction_method="docx_xml",
                        cells=cells,
                        table_index=table_index,
                        row_index=row_index,
                    )
                )
    return ParsedDocument(
        format="docx", blocks=blocks, metadata={"block_count": len(blocks)}
    )

def _is_usable_pdf_text(text: str, *, min_text_chars: int) -> bool:
    if len(text) < min_text_chars:
        return False
    control_count = sum(
        category(character) == "Cc" and character not in "\n\r\t"
        for character in text
    )
    return control_count / len(text) <= 0.01


@dataclass(frozen=True)
class PdfParseOptions:
    min_text_chars: int = 40
    ocr_concurrency: int = 4
    page_max_attempts: int = 3
    retry_base_delay_seconds: float = 1.0


class PageCache(Protocol):
    def get(self, key: str) -> list[VisionBlock] | None:
        raise NotImplementedError

    def put(self, key: str, blocks: list[VisionBlock]) -> None:
        raise NotImplementedError


class FileSystemPageCache:
    """OCR results per page, so a retried run only re-OCRs the pages that failed."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / f"{key}.json").resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Page cache key escapes the cache root")
        return path

    def get(self, key: str) -> list[VisionBlock] | None:
        path = self._path(key)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return [VisionBlock(**block) for block in raw["blocks"]]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def put(self, key: str, blocks: list[VisionBlock]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(
                {"blocks": [asdict(block) for block in blocks]}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
        temporary.replace(path)


def _page_cache_key(file_hash: str, ocr_identity: str, page_number: int) -> str:
    safe_identity = "".join(
        character if character.isalnum() or character in "-_." else "_"
        for character in ocr_identity
    )
    return f"{file_hash}/{safe_identity}/page-{page_number:05d}"


def _usable_vision_blocks(blocks: list[VisionBlock]) -> list[VisionBlock]:
    usable = []
    for block in blocks:
        content = block.content.strip()
        if not content and block.cells:
            content = " | ".join(cell.strip() for cell in block.cells)
        if content:
            usable.append(
                block if content == block.content else VisionBlock(
                    content=content,
                    content_type=block.content_type,
                    bounding_box=block.bounding_box,
                    confidence=block.confidence,
                    cells=block.cells,
                )
            )
    return usable


@dataclass
class _OcrPageResult:
    blocks: list[VisionBlock]
    attempts: int
    cached: bool


async def _ocr_page(
    path: Path,
    page_number: int,
    *,
    renderer: PdfPageRenderer,
    vision: VisionOcrProvider,
    options: PdfParseOptions,
    cache: PageCache | None,
    cache_key: str | None,
    sleep: Callable[[float], Awaitable[None]],
) -> _OcrPageResult:
    if cache is not None and cache_key is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            return _OcrPageResult(blocks=cached, attempts=0, cached=True)
    image_bytes = await asyncio.to_thread(renderer.render_png, path, page_number)
    attempt = 0
    while True:
        attempt += 1
        try:
            blocks = _usable_vision_blocks(
                await vision.extract_page(image_bytes, page_number)
            )
            break
        except ProviderError as exc:
            if not exc.retryable or attempt >= options.page_max_attempts:
                raise
            await sleep(options.retry_base_delay_seconds * 2 ** (attempt - 1))
    if cache is not None and cache_key is not None:
        cache.put(cache_key, blocks)
    return _OcrPageResult(blocks=blocks, attempts=attempt, cached=False)


async def parse_pdf(
    path: Path,
    *,
    renderer: PdfPageRenderer,
    vision: VisionOcrProvider,
    min_text_chars: int | None = None,
    options: PdfParseOptions | None = None,
    cache: PageCache | None = None,
    file_hash: str | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ParsedDocument:
    options = options or PdfParseOptions()
    if min_text_chars is not None:
        options = replace(options, min_text_chars=min_text_chars)
    reader = PdfReader(path)
    page_texts: dict[int, str] = {}
    ocr_pages: list[int] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if _is_usable_pdf_text(text, min_text_chars=options.min_text_chars):
            page_texts[page_number] = text
        else:
            ocr_pages.append(page_number)

    ocr_identity = getattr(vision, "cache_identity", None)
    use_cache = cache is not None and file_hash is not None and bool(ocr_identity)
    semaphore = asyncio.Semaphore(max(1, options.ocr_concurrency))

    async def run(page_number: int) -> _OcrPageResult:
        async with semaphore:
            return await _ocr_page(
                path,
                page_number,
                renderer=renderer,
                vision=vision,
                options=options,
                cache=cache if use_cache else None,
                cache_key=(
                    _page_cache_key(file_hash, ocr_identity, page_number)
                    if use_cache
                    else None
                ),
                sleep=sleep,
            )

    ocr_results: dict[int, _OcrPageResult] = {}
    if ocr_pages:
        try:
            async with asyncio.TaskGroup() as group:
                tasks = {
                    page_number: group.create_task(run(page_number))
                    for page_number in ocr_pages
                }
        except* ProviderError as errors:
            raise errors.exceptions[0] from None
        ocr_results = {page: task.result() for page, task in tasks.items()}

    blocks: list[ParsedBlock] = []
    pages: list[dict[str, Any]] = []
    for page_number in range(1, len(reader.pages) + 1):
        if page_number in page_texts:
            blocks.append(
                ParsedBlock(
                    content=page_texts[page_number],
                    page_number=page_number,
                    extraction_method="pdf_text",
                )
            )
            pages.append(
                {"page": page_number, "method": "pdf_text", "block_count": 1}
            )
            continue
        result = ocr_results[page_number]
        for vision_block in result.blocks:
            blocks.append(
                ParsedBlock(
                    content=vision_block.content,
                    content_type=vision_block.content_type,
                    page_number=page_number,
                    extraction_method="vision_ocr",
                    bounding_box=vision_block.bounding_box,
                    confidence=vision_block.confidence,
                    cells=vision_block.cells,
                )
            )
        pages.append(
            {
                "page": page_number,
                "method": "vision_ocr",
                "block_count": len(result.blocks),
                "attempts": result.attempts,
                "cached": result.cached,
            }
        )
    return ParsedDocument(
        format="pdf",
        blocks=blocks,
        metadata={
            "page_count": len(reader.pages),
            "ocr_pages": ocr_pages,
            "ocr_empty_pages": [
                page for page, result in ocr_results.items() if not result.blocks
            ],
            "pages": pages,
        },
    )
