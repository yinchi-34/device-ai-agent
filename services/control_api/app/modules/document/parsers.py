from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element
from zipfile import ZipFile

import pypdfium2 as pdfium
from defusedxml import ElementTree
from pypdf import PdfReader

from app.modules.document.providers import PdfPageRenderer, VisionOcrProvider

_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f"{{{_WORD_NS}}}"


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
    def render_png(self, pdf_path: Path, page_number: int) -> bytes:
        document = pdfium.PdfDocument(pdf_path)
        try:
            page = document[page_number - 1]
            try:
                image = page.render(scale=2).to_pil()
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


async def parse_pdf(
    path: Path,
    *,
    renderer: PdfPageRenderer,
    vision: VisionOcrProvider,
    min_text_chars: int,
) -> ParsedDocument:
    reader = PdfReader(path)
    blocks: list[ParsedBlock] = []
    ocr_pages: list[int] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if len(text) >= min_text_chars:
            blocks.append(
                ParsedBlock(
                    content=text,
                    page_number=page_number,
                    extraction_method="pdf_text",
                )
            )
            continue
        ocr_pages.append(page_number)
        image_bytes = renderer.render_png(path, page_number)
        for vision_block in await vision.extract_page(image_bytes, page_number):
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
    return ParsedDocument(
        format="pdf",
        blocks=blocks,
        metadata={"page_count": len(reader.pages), "ocr_pages": ocr_pages},
    )
