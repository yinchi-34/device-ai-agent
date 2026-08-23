from zipfile import ZIP_DEFLATED, ZipFile

from pypdf import PdfWriter

from app.modules.document.parsers import (
    _is_usable_pdf_text,
    parse_docx,
    parse_pdf,
)
from app.modules.document.providers import VisionBlock


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
