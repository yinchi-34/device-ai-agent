from app.modules.document.chunking import build_chunks, group_chunks_for_extraction
from app.modules.document.parsers import ParsedBlock, ParsedDocument


def test_build_chunks_assigns_stable_refs_and_hashes():
    parsed = ParsedDocument(
        format="pdf",
        blocks=[
            ParsedBlock(
                content="Run status 40001",
                content_type="text",
                page_number=3,
                extraction_method="pdf_text",
            ),
            ParsedBlock(
                content="Code | Address",
                content_type="table_row",
                page_number=3,
                extraction_method="vision_ocr",
                table_index=1,
                row_index=4,
                cells=["Code", "Address"],
            ),
        ],
        metadata={},
    )

    chunks = build_chunks(parsed)

    assert [chunk.stable_ref for chunk in chunks] == [
        "pdf:page:3:block:1",
        "pdf:page:3:table:1:row:4",
    ]
    assert len(chunks[0].content_hash) == 64
    assert chunks[1].table_metadata == {"cells": ["Code", "Address"]}


def test_grouping_never_splits_a_table_row():
    parsed = ParsedDocument(
        format="docx",
        blocks=[
            ParsedBlock(
                content="A" * 20,
                content_type="text",
                extraction_method="docx_xml",
            ),
            ParsedBlock(
                content="B" * 80,
                content_type="table_row",
                extraction_method="docx_xml",
                table_index=1,
                row_index=1,
                cells=["B" * 80],
            ),
        ],
        metadata={},
    )

    groups = group_chunks_for_extraction(build_chunks(parsed), max_tokens=10)

    assert len(groups) == 2
    assert groups[1][0].content == "B" * 80


def test_pdf_table_rows_get_table_refs_and_header_flag():
    parsed = ParsedDocument(
        format="pdf",
        blocks=[
            ParsedBlock(
                content="地址 名称",
                content_type="table_row",
                page_number=6,
                table_index=1,
                row_index=1,
                is_table_header=True,
            ),
            ParsedBlock(
                content="30001 送风温度",
                content_type="table_row",
                page_number=7,
                table_index=1,
                row_index=2,
            ),
        ],
    )

    chunks = build_chunks(parsed)

    assert [chunk.stable_ref for chunk in chunks] == [
        "pdf:page:6:table:1:row:1",
        "pdf:page:7:table:1:row:2",
    ]
    assert chunks[0].table_metadata == {"header": True}
    assert chunks[1].table_metadata == {}


def test_chunks_carry_section_and_table_header_context():
    parsed = ParsedDocument(
        format="pdf",
        blocks=[
            ParsedBlock(
                content="第 6 章 寄存器表", content_type="heading", page_number=6
            ),
            ParsedBlock(
                content="地址 名称",
                content_type="table_row",
                page_number=6,
                table_index=1,
                row_index=1,
                is_table_header=True,
            ),
            ParsedBlock(
                content="30001 送风温度",
                content_type="table_row",
                page_number=6,
                table_index=1,
                row_index=2,
            ),
            ParsedBlock(
                content="第 7 章 控制逻辑", content_type="heading", page_number=8
            ),
            ParsedBlock(content="制冷模式下调节冷水阀。", page_number=8),
        ],
    )

    chunks = build_chunks(parsed)

    assert [chunk.context_text for chunk in chunks] == [
        "",
        "第 6 章 寄存器表",
        "第 6 章 寄存器表\n地址 名称",
        "",
        "第 7 章 控制逻辑",
    ]
    assert chunks[2].content == "30001 送风温度"
