from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from pypdf import PdfWriter

from app.modules.document.file_validation import (
    DocumentValidationError,
    validate_file_content,
    validate_upload_metadata,
)

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def test_rejects_suffix_magic_mismatch(tmp_path):
    path = tmp_path / "manual.docx"
    path.write_bytes(b"%PDF-1.7\n")

    with pytest.raises(DocumentValidationError) as excinfo:
        validate_file_content(path, ".docx", max_pdf_pages=300)

    assert excinfo.value.code == "document_type_mismatch"


@pytest.mark.parametrize(
    ("name", "media_type", "size", "code"),
    [
        ("manual.txt", "text/plain", 12, "document_type_not_allowed"),
        ("manual.pdf", "application/pdf", 0, "document_empty"),
        ("manual.pdf", "application/pdf", 101, "document_too_large"),
        ("manual.pdf", DOCX_MEDIA_TYPE, 12, "document_type_mismatch"),
    ],
)
def test_upload_metadata_rejects_unsafe_inputs(name, media_type, size, code):
    with pytest.raises(DocumentValidationError) as excinfo:
        validate_upload_metadata(name, media_type, size, max_file_bytes=100)

    assert excinfo.value.code == code


def test_rejects_encrypted_pdf(tmp_path):
    path = tmp_path / "secret.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    with path.open("wb") as stream:
        writer.write(stream)

    with pytest.raises(DocumentValidationError) as excinfo:
        validate_file_content(path, ".pdf", max_pdf_pages=10)

    assert excinfo.value.code == "document_pdf_encrypted"


def test_rejects_docx_external_relationship(tmp_path):
    path = tmp_path / "external.docx"
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>',
        )
        archive.writestr(
            "word/_rels/document.xml.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="link" Target="https://example.com" TargetMode="External"/></Relationships>',
        )

    with pytest.raises(DocumentValidationError) as excinfo:
        validate_file_content(path, ".docx", max_pdf_pages=10)

    assert excinfo.value.code == "document_docx_external_relationship"
