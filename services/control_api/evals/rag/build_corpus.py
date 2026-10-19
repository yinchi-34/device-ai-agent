"""Render the synthetic manual corpus to real DOCX / PDF files.

Run from ``services/control_api``::

    python -m evals.rag.build_corpus

Requires ``python-docx`` and ``playwright`` (Chromium) — only needed to
regenerate the committed files under ``evals/rag/corpus/``. The evaluation
itself reads the committed files and does not need these packages.
"""

from __future__ import annotations

import html
import io
import json
from pathlib import Path

from evals.rag import corpus_spec as s

CORPUS_DIR = Path(__file__).parent / "corpus"


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


def _docx_table(document, header: list[str], rows: list[list[str]]) -> None:
    table = document.add_table(rows=1, cols=len(header))
    for cell, text in zip(table.rows[0].cells, header, strict=True):
        cell.text = text
    for row in rows:
        cells = table.add_row().cells
        for cell, text in zip(cells, row, strict=True):
            cell.text = text


def build_ahu200_docx(path: Path) -> None:
    from docx import Document

    d = Document()
    d.add_heading("AHU-200 组合式空调机组 用户手册（2025 版）", level=0)
    d.add_heading("1 版本说明", level=1)
    d.add_paragraph(s.ahu200_version_note(current=True))
    d.add_heading("2 安全须知", level=1)
    for text in s.AHU200_SAFETY_TEXT:
        d.add_paragraph(text)
    d.add_heading("3 技术参数", level=1)
    _docx_table(d, s.AHU200_SPEC_HEADER, s.AHU200_SPECS)
    d.add_heading("4 通讯设置", level=1)
    d.add_paragraph(s.AHU200_COMM_TEXT)
    d.add_heading("5 Modbus 寄存器表", level=1)
    d.add_paragraph("下表地址采用 4xxxx 表示法，报文中的寄存器偏移量 = 地址 - 40001。")
    _docx_table(d, s.AHU200_REGISTER_HEADER, s.ahu200_registers("0.01"))
    d.add_heading("6 报警代码", level=1)
    _docx_table(d, s.AHU200_ALARM_HEADER, s.ahu200_alarms("250"))
    d.add_heading("7 故障排查", level=1)
    for title, body in s.AHU200_TROUBLESHOOTING:
        d.add_heading(title, level=2)
        d.add_paragraph(body)
    d.add_heading("8 维护保养", level=1)
    for text in s.AHU200_MAINTENANCE:
        d.add_paragraph(text)
    _ahu_filler_docx(d)
    d.save(path)


def _ahu_filler_docx(d) -> None:
    d.add_heading("9 控制面板菜单", level=1)
    _docx_table(d, s.AHU_MENU_HEADER, s.AHU_MENU)
    d.add_heading("10 电气接线端子", level=1)
    _docx_table(d, s.AHU_TERMINAL_HEADER, s.AHU_TERMINALS)
    d.add_heading("11 安装", level=1)
    for text in s.AHU_INSTALL:
        d.add_paragraph(text)
    d.add_heading("12 调试", level=1)
    for text in s.AHU_COMMISSION:
        d.add_paragraph(text)


def build_ch500_docx(path: Path) -> None:
    from docx import Document

    d = Document()
    d.add_heading("CH-500 Water-cooled Chiller 冷水机组 BACnet 集成手册", level=0)
    d.add_heading("1 Communication 通讯", level=1)
    d.add_paragraph(s.CH500_COMM_TEXT)
    d.add_heading("2 BACnet Object List 对象列表", level=1)
    _docx_table(d, s.CH500_OBJECT_HEADER, s.CH500_OBJECTS)
    d.add_heading("3 Alarm Codes 报警代码", level=1)
    _docx_table(d, s.CH500_ALARM_HEADER, s.CH500_ALARMS)
    d.add_heading("4 Notes 注意事项", level=1)
    for text in s.CH500_NOTES:
        d.add_paragraph(text)
    d.add_heading("5 Commissioning 调试", level=1)
    for text in s.CH500_COMMISSION:
        d.add_paragraph(text)
    d.add_heading("6 Wiring 接线", level=1)
    _docx_table(d, s.CH500_WIRING_HEADER, s.CH500_WIRING)
    d.save(path)


# ---------------------------------------------------------------------------
# PDF via Chromium
# ---------------------------------------------------------------------------

_CSS = """
@page { size: A4; margin: 18mm 16mm; }
body { font-family: 'Noto Sans CJK SC', 'Noto Sans', sans-serif; font-size: 10.5pt; line-height: 1.45; }
h1 { font-size: 17pt; } h2 { font-size: 13pt; margin-top: 14pt; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0; }
td, th { border: 1px solid #444; padding: 2pt 4pt; font-size: 9.5pt; text-align: left; }
.page { page-break-after: always; }
.page:last-child { page-break-after: auto; }
.footer { color: #666; font-size: 8pt; margin-top: 10pt; }
"""


def _h(text: str) -> str:
    return html.escape(text)


def _html_table(header: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{_h(c)}</th>" for c in header)
    body = "".join(
        "<tr>" + "".join(f"<td>{_h(c)}</td>" for c in row) + "</tr>" for row in rows
    )
    return f"<table><tr>{head}</tr>{body}</table>"


def _paras(texts: list[str]) -> str:
    return "".join(f"<p>{_h(t)}</p>" for t in texts)


def _render_pdf(pages: list[str], footer: str) -> bytes:
    from playwright.sync_api import sync_playwright

    body = "".join(
        f'<div class="page">{page}<div class="footer">{_h(footer)}</div></div>'
        for page in pages
    )
    doc = f"<!doctype html><html><head><meta charset='utf-8'><style>{_CSS}</style></head><body>{body}</body></html>"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(doc, wait_until="load")
        pdf = page.pdf(format="A4", print_background=True)
        browser.close()
    return pdf


def _ahu_pdf_pages(
    title: str, version_note: str, specs, comm: str, reg_header, registers, alarms,
    troubleshooting, maintenance,
) -> list[str]:
    pages = [
        (f"<h1>{_h(title)}</h1><h2>1 版本说明</h2><p>{_h(version_note)}</p>"
        f"<h2>2 安全须知</h2>{_paras(s.AHU200_SAFETY_TEXT)}"
        f"<h2>3 技术参数</h2>{_html_table(s.AHU200_SPEC_HEADER, specs)}"
        f"<h2>4 通讯设置</h2><p>{_h(comm)}</p>"),
        f"<h2>5 Modbus 寄存器表</h2>{_html_table(reg_header, registers)}",
        f"<h2>6 报警代码</h2>{_html_table(s.AHU200_ALARM_HEADER, alarms)}",
    ]
    if troubleshooting:
        pages.append(
            "<h2>7 故障排查</h2>"
            + "".join(f"<h2>{_h(t)}</h2><p>{_h(b)}</p>" for t, b in troubleshooting)
            + f"<h2>8 维护保养</h2>{_paras(maintenance)}"
        )
    pages += [
        f"<h2>9 控制面板菜单</h2>{_html_table(s.AHU_MENU_HEADER, s.AHU_MENU)}",
        f"<h2>10 电气接线端子</h2>{_html_table(s.AHU_TERMINAL_HEADER, s.AHU_TERMINALS)}",
        f"<h2>11 安装</h2>{_paras(s.AHU_INSTALL)}<h2>12 调试</h2>{_paras(s.AHU_COMMISSION)}",
    ]
    return pages


def build_ahu200_v2023_pdf(path: Path) -> None:
    pages = _ahu_pdf_pages(
        "AHU-200 组合式空调机组 用户手册（2023 版）",
        s.ahu200_version_note(current=False),
        s.AHU200_SPECS,
        s.AHU200_COMM_TEXT,
        s.AHU200_REGISTER_HEADER,
        s.ahu200_registers("0.1"),
        s.ahu200_alarms("300"),
        s.AHU200_TROUBLESHOOTING,
        s.AHU200_MAINTENANCE,
    )
    path.write_bytes(_render_pdf(pages, "AHU-200 用户手册 2023 版"))


def build_ahu300_pdf(path: Path) -> None:
    pages = _ahu_pdf_pages(
        "AHU-300 组合式空调机组 用户手册",
        "本手册适用于 AHU-300 全系列，固件 V1.0 及以上。",
        s.AHU300_SPECS,
        s.AHU300_COMM_TEXT,
        s.AHU200_REGISTER_HEADER,
        s.AHU300_REGISTERS,
        s.AHU300_ALARMS,
        [],
        [],
    )
    path.write_bytes(_render_pdf(pages, "AHU-300 用户手册"))


def _vfd_pages() -> list[str]:
    return [
        ("<h1>VFD-750 Variable Frequency Drive — User Manual</h1>"
        f"<h2>1 Specifications</h2>{_html_table(s.VFD_SPECS_HEADER, s.VFD_SPECS)}"),
        f"<h2>2 Parameter list</h2>{_html_table(s.VFD_PARAM_HEADER, s.VFD_PARAMS)}",
        (f"<h2>3 Modbus register map</h2>{_html_table(s.VFD_REGISTER_HEADER, s.VFD_REGISTERS)}"
        f"<h2>3.1 Fault reset</h2><p>{_h(s.VFD_RESTART_TEXT)}</p>"
        f"<h2>3.2 Notes</h2><p>{_h(s.VFD_INJECTION_TEXT)}</p>"),
        f"<h2>4 Fault codes</h2>{_html_table(s.VFD_FAULT_HEADER, s.VFD_FAULTS)}",
        "<h2>5 Installation</h2>"
        + _paras(s.VFD_INSTALL_FILLER)
        + f"<p>{_h(s.VFD_INSTALL_KEY_FACT)}</p>"
        + _paras(s.VFD_INSTALL_FILLER_2),
        f"<h2>6 Keypad operation</h2>{_html_table(s.VFD_KEYPAD_HEADER, s.VFD_KEYPAD)}{_paras(s.VFD_KEYPAD_TEXT)}",
        f"<h2>7 Control terminals</h2>{_html_table(s.VFD_TERMINAL_HEADER, s.VFD_TERMINALS)}",
        f"<h2>8 Extended parameters</h2>{_html_table(s.VFD_P2_HEADER, s.VFD_P2)}",
        f"<h2>9 EMC installation</h2>{_paras(s.VFD_EMC)}",
    ]


def _scanned_page_pdf() -> bytes:
    """Render appendix B, rasterise it, and wrap the bitmap in an image-only PDF page."""
    import pypdfium2 as pdfium

    rows = [line.split(" | ") for line in s.VFD_SCANNED_LINES[:-1]]
    page_html = (
        f"<h2>{_h(s.VFD_SCANNED_TITLE)}</h2>{_html_table(rows[0], rows[1:])}"
        f"<p>{_h(s.VFD_SCANNED_LINES[-1])}</p>"
    )
    vector_pdf = _render_pdf([page_html], "VFD-750 User Manual")
    pdf = pdfium.PdfDocument(vector_pdf)
    image = pdf[0].render(scale=1.5).to_pil().convert("L")
    out = io.BytesIO()
    image.save(out, format="PDF", resolution=108.0)
    return out.getvalue()


def build_vfd750_pdf(path: Path) -> int:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(_render_pdf(_vfd_pages(), "VFD-750 User Manual"))).pages:
        writer.add_page(page)
    writer.add_page(PdfReader(io.BytesIO(_scanned_page_pdf())).pages[0])
    scanned_page_number = len(writer.pages)
    with path.open("wb") as fh:
        writer.write(fh)
    return scanned_page_number


# ---------------------------------------------------------------------------
# OCR fixture (simulated vision-model output for the scanned page)
# ---------------------------------------------------------------------------

# Typical OCR confusions on low-resolution scans: 0<->O, m<->rn, l<->1.
_OCR_CONFUSIONS = {
    "VFD-750 | 60 ohm | 75 ohm | 1000 W": "VFD-75O | 6O ohm | 75 ohrn | 10O0 W",
    "VFD-550 | 80 ohm | 100 ohm | 800 W": "VFD-550 | 80 ohm | 1OO ohm | 800 W",
}


def ocr_noise(line: str) -> str:
    return _OCR_CONFUSIONS.get(line, line)


def build_ocr_fixture() -> list[dict]:
    blocks = [{"content": s.VFD_SCANNED_TITLE, "content_type": "text", "confidence": 0.97}]
    for line in s.VFD_SCANNED_LINES[:-1]:
        noisy = ocr_noise(line)
        blocks.append(
            {
                "content": noisy,
                "content_type": "table",
                "confidence": 0.71 if noisy != line else 0.93,
                "cells": noisy.split(" | "),
            }
        )
    blocks.append(
        {"content": s.VFD_SCANNED_LINES[-1], "content_type": "text", "confidence": 0.95}
    )
    return blocks


def main() -> None:
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    build_ahu200_docx(CORPUS_DIR / "ahu200_manual_2025.docx")
    build_ahu200_v2023_pdf(CORPUS_DIR / "ahu200_manual_2023.pdf")
    build_ahu300_pdf(CORPUS_DIR / "ahu300_manual.pdf")
    scanned_page = build_vfd750_pdf(CORPUS_DIR / "vfd750_manual.pdf")
    build_ch500_docx(CORPUS_DIR / "ch500_bacnet_manual.docx")
    manifest = {
        "version": "1.0",
        "documents": [
            {"doc_id": "ahu200_2025", "file": "ahu200_manual_2025.docx", "device_model_id": 1,
             "device_model": "AHU-200", "language": "zh", "edition": "2025", "is_current": True},
            {"doc_id": "ahu200_2023", "file": "ahu200_manual_2023.pdf", "device_model_id": 1,
             "device_model": "AHU-200", "language": "zh", "edition": "2023", "is_current": False},
            {"doc_id": "ahu300", "file": "ahu300_manual.pdf", "device_model_id": 2,
             "device_model": "AHU-300", "language": "zh", "edition": "2024", "is_current": True},
            {"doc_id": "vfd750", "file": "vfd750_manual.pdf", "device_model_id": 3,
             "device_model": "VFD-750", "language": "en", "edition": "2024", "is_current": True},
            {"doc_id": "ch500", "file": "ch500_bacnet_manual.docx", "device_model_id": 4,
             "device_model": "CH-500", "language": "zh-en", "edition": "2024", "is_current": True},
        ],
        "ocr_fixtures": {
            "vfd750": {
                str(scanned_page): build_ocr_fixture(),
            }
        },
        "ocr_ground_truth": {
            "vfd750": {
                str(scanned_page): [s.VFD_SCANNED_TITLE, *s.VFD_SCANNED_LINES],
            }
        },
        "injected_markers": ["SYSTEM NOTE FOR AI ASSISTANTS"],
    }
    (CORPUS_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"corpus written to {CORPUS_DIR}")


if __name__ == "__main__":
    main()
