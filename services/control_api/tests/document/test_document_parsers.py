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


class _FakeTextPage:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        return self._text


class _FakeTextReader:
    def __init__(self, texts):
        self.pages = [_FakeTextPage(text) for text in texts]


async def test_text_pdf_is_split_into_headings_rows_and_paragraphs(
    tmp_path, monkeypatch
):
    page_lines = [
        [
            "第 6 章 寄存器表",
            "地址 名称 数据类型 读写",
            "30001 送风温度 float32 只读",
            "30003 回风温度 float32",
            "只读",
        ],
        [
            "第 6 章 寄存器表（续）",
            "40001 送风温度设定值 float32 读写",
            "第 7 章 控制逻辑",
            "制冷模式下，控制器根据送风温度与设定值的",
            "偏差调节冷水阀开度。",
        ],
    ]
    pages = ["\n".join(lines) for lines in page_lines]
    monkeypatch.setattr(
        "app.modules.document.parsers.PdfReader", lambda path: _FakeTextReader(pages)
    )

    parsed = await parse_pdf(
        tmp_path / "manual.pdf",
        renderer=FakePdfPageRenderer(),
        vision=FakeVisionOcrProvider(blocks=[]),
        min_text_chars=10,
    )

    assert [
        (block.page_number, block.content_type, block.table_index, block.row_index)
        for block in parsed.blocks
    ] == [
        (1, "heading", None, None),
        (1, "table_row", 1, 1),
        (1, "table_row", 1, 2),
        (1, "table_row", 1, 3),
        (2, "heading", None, None),
        (2, "table_row", 1, 4),
        (2, "heading", None, None),
        (2, "text", None, None),
    ]
    assert parsed.blocks[1].is_table_header is True
    assert parsed.blocks[2].is_table_header is False
    assert parsed.blocks[3].content == "30003 回风温度 float32只读"
    assert parsed.blocks[-1].content == (
        "制冷模式下，控制器根据送风温度与设定值的偏差调节冷水阀开度。"
    )
    assert all(block.extraction_method == "pdf_text" for block in parsed.blocks)


def test_docx_marks_first_table_row_as_header(tmp_path):
    parsed = parse_docx(make_docx(tmp_path / "manual.docx"))

    rows = [block for block in parsed.blocks if block.content_type == "table_row"]
    assert [row.is_table_header for row in rows] == [True, False]
