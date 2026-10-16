import asyncio
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from PIL import Image
from pypdf import PdfWriter

from app.modules.document.parsers import (
    FileSystemPageCache,
    PdfParseOptions,
    Pypdfium2PageRenderer,
    _is_usable_pdf_text,
    parse_docx,
    parse_pdf,
)
from app.modules.document.providers import ProviderError, VisionBlock


def make_docx(path):
    document_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body>
        <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Registers</w:t></w:r></w:p>
        <w:p><w:r><w:t>Run status -10 °C</w:t></w:r></w:p>
        <w:tbl>
          <w:tr><w:tc><w:p><w:r><w:t>Code</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Address</w:t></w:r></w:p></w:tc></w:tr>
          <w:tr><w:tc><w:p><w:r><w:t>run</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>40001</w:t></w:r></w:p></w:tc></w:tr>
        </w:tbl>
      </w:body>
    </w:document>"""
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    return path


def test_docx_preserves_paragraph_table_order(tmp_path):
    parsed = parse_docx(make_docx(tmp_path / "manual.docx"))

    assert [block.content_type for block in parsed.blocks] == [
        "heading",
        "text",
        "table_row",
        "table_row",
    ]
    assert parsed.blocks[1].content == "Run status -10 °C"
    assert parsed.blocks[2].cells == ["Code", "Address"]
def test_pdf_text_with_many_control_characters_requires_vision():
    normal = "Communication settings and Modbus register values. " * 3
    garbled = "3$&\x03FRPPXQLFDWLRQ\x03VHWWLQJV\x03" * 4

    assert _is_usable_pdf_text(normal, min_text_chars=40) is True
    assert _is_usable_pdf_text(garbled, min_text_chars=40) is False




class FakeVisionOcrProvider:
    def __init__(self, blocks):
        self.blocks = blocks
        self.pages = []

    async def extract_page(self, image_bytes: bytes, page_number: int):
        assert image_bytes == b"rendered"
        self.pages.append(page_number)
        return self.blocks


class FakePdfPageRenderer:
    def render_png(self, pdf_path, page_number):
        assert pdf_path.suffix == ".pdf"
        assert page_number == 1
        return b"rendered"


async def test_low_text_pdf_uses_vision_provider(tmp_path):
    path = tmp_path / "scanned.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with path.open("wb") as stream:
        writer.write(stream)
    vision = FakeVisionOcrProvider(
        blocks=[
            VisionBlock(
                content="Run status 40001",
                bounding_box=[1, 2, 3, 4],
                confidence=0.96,
            )
        ]
    )

    parsed = await parse_pdf(
        path,
        renderer=FakePdfPageRenderer(),
        vision=vision,
        min_text_chars=40,
    )

    assert vision.pages == [1]
    assert parsed.blocks[0].extraction_method == "vision_ocr"
    assert parsed.blocks[0].page_number == 1
    assert parsed.blocks[0].bounding_box == [1, 2, 3, 4]
    assert parsed.blocks[0].confidence == 0.96


def make_blank_pdf(path, pages):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


class AnyPageRenderer:
    def render_png(self, pdf_path, page_number):
        return f"page-{page_number}".encode()


class ScriptedVision:
    """Returns one block per page; failures are consumed per page in order."""

    cache_identity = "fake-vision-v1"

    def __init__(self, failures=None, *, delay=0.0):
        self.failures = {page: list(errors) for page, errors in (failures or {}).items()}
        self.calls = []
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0

    async def extract_page(self, image_bytes, page_number):
        self.calls.append(page_number)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            pending = self.failures.get(page_number)
            if pending:
                raise pending.pop(0)
            return [VisionBlock(content=f"text of page {page_number}")]
        finally:
            self.in_flight -= 1


async def no_sleep(seconds):
    return None


async def test_ocr_retries_only_the_failing_page(tmp_path):
    path = make_blank_pdf(tmp_path / "scan.pdf", 3)
    vision = ScriptedVision(
        {2: [ProviderError("provider_rate_limited", retryable=True)]}
    )
    delays = []

    async def record_sleep(seconds):
        delays.append(seconds)

    parsed = await parse_pdf(
        path,
        renderer=AnyPageRenderer(),
        vision=vision,
        options=PdfParseOptions(retry_base_delay_seconds=0.5),
        sleep=record_sleep,
    )

    assert sorted(vision.calls) == [1, 2, 2, 3]
    assert delays == [0.5]
    assert [block.page_number for block in parsed.blocks] == [1, 2, 3]
    assert parsed.metadata["pages"][1] == {
        "page": 2,
        "method": "vision_ocr",
        "block_count": 1,
        "attempts": 2,
        "cached": False,
    }


async def test_ocr_gives_up_after_max_attempts(tmp_path):
    path = make_blank_pdf(tmp_path / "scan.pdf", 1)
    errors = [ProviderError("provider_timeout", retryable=True) for _ in range(3)]
    vision = ScriptedVision({1: errors})

    with pytest.raises(ProviderError) as excinfo:
        await parse_pdf(
            path,
            renderer=AnyPageRenderer(),
            vision=vision,
            options=PdfParseOptions(page_max_attempts=3),
            sleep=no_sleep,
        )

    assert excinfo.value.code == "provider_timeout"
    assert vision.calls == [1, 1, 1]


async def test_non_retryable_ocr_error_is_not_retried(tmp_path):
    path = make_blank_pdf(tmp_path / "scan.pdf", 1)
    vision = ScriptedVision({1: [ProviderError("provider_request_rejected")]})

    with pytest.raises(ProviderError):
        await parse_pdf(
            path, renderer=AnyPageRenderer(), vision=vision, sleep=no_sleep
        )

    assert vision.calls == [1]


async def test_ocr_runs_pages_concurrently_up_to_the_limit(tmp_path):
    path = make_blank_pdf(tmp_path / "scan.pdf", 6)
    vision = ScriptedVision(delay=0.01)

    parsed = await parse_pdf(
        path,
        renderer=AnyPageRenderer(),
        vision=vision,
        options=PdfParseOptions(ocr_concurrency=2),
    )

    assert vision.max_in_flight == 2
    assert [block.page_number for block in parsed.blocks] == [1, 2, 3, 4, 5, 6]


async def test_page_cache_skips_pages_that_already_succeeded(tmp_path):
    path = make_blank_pdf(tmp_path / "scan.pdf", 3)
    cache = FileSystemPageCache(tmp_path / "page-cache")
    failing = ScriptedVision(
        {3: [ProviderError("provider_request_rejected")]}
    )

    with pytest.raises(ProviderError):
        await parse_pdf(
            path,
            renderer=AnyPageRenderer(),
            vision=failing,
            options=PdfParseOptions(ocr_concurrency=1),
            cache=cache,
            file_hash="a" * 64,
        )

    retry = ScriptedVision()
    parsed = await parse_pdf(
        path,
        renderer=AnyPageRenderer(),
        vision=retry,
        cache=cache,
        file_hash="a" * 64,
    )

    assert retry.calls == [3]
    assert [page["cached"] for page in parsed.metadata["pages"]] == [
        True,
        True,
        False,
    ]
    assert parsed.blocks[0].content == "text of page 1"


async def test_page_cache_is_keyed_by_ocr_identity(tmp_path):
    path = make_blank_pdf(tmp_path / "scan.pdf", 1)
    cache = FileSystemPageCache(tmp_path / "page-cache")
    await parse_pdf(
        path,
        renderer=AnyPageRenderer(),
        vision=ScriptedVision(),
        cache=cache,
        file_hash="b" * 64,
    )
    other_model = ScriptedVision()
    other_model.cache_identity = "other-model"

    await parse_pdf(
        path,
        renderer=AnyPageRenderer(),
        vision=other_model,
        cache=cache,
        file_hash="b" * 64,
    )

    assert other_model.calls == [1]


async def test_empty_ocr_blocks_are_dropped_and_table_cells_fill_content(tmp_path):
    path = make_blank_pdf(tmp_path / "scan.pdf", 1)

    class Vision:
        async def extract_page(self, image_bytes, page_number):
            return [
                VisionBlock(content="  "),
                VisionBlock(content="", content_type="table", cells=["40001", "Run"]),
            ]

    parsed = await parse_pdf(path, renderer=AnyPageRenderer(), vision=Vision())

    assert [block.content for block in parsed.blocks] == ["40001 | Run"]
    assert parsed.metadata["ocr_empty_pages"] == []


async def test_blank_ocr_page_is_reported(tmp_path):
    path = make_blank_pdf(tmp_path / "scan.pdf", 1)

    class Vision:
        async def extract_page(self, image_bytes, page_number):
            return []

    parsed = await parse_pdf(path, renderer=AnyPageRenderer(), vision=Vision())

    assert parsed.blocks == []
    assert parsed.metadata["ocr_empty_pages"] == [1]


def test_renderer_caps_the_longest_image_side(tmp_path):
    path = tmp_path / "poster.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=2000, height=1000)
    with path.open("wb") as stream:
        writer.write(stream)

    image = Image.open(
        BytesIO(Pypdfium2PageRenderer(max_image_side_px=800).render_png(path, 1))
    )

    assert max(image.size) <= 800


def test_page_cache_rejects_keys_outside_its_root(tmp_path):
    cache = FileSystemPageCache(tmp_path / "page-cache")

    with pytest.raises(ValueError):
        cache.get("../../escape")
