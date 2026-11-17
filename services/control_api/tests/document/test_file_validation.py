from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject

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


def test_allows_pdf_internal_goto_open_action(tmp_path):
    path = tmp_path / "manual.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    writer.root_object.update(
        {
            NameObject("/OpenAction"): DictionaryObject(
                {
                    NameObject("/S"): NameObject("/GoTo"),
                    NameObject("/D"): ArrayObject(
                        [page.indirect_reference, NameObject("/Fit")]
                    ),
                }
            )
        }
    )
    with path.open("wb") as stream:
        writer.write(stream)

    metadata = validate_file_content(path, ".pdf", max_pdf_pages=10)

    assert metadata == {"page_count": 1}


def test_rejects_pdf_javascript_open_action(tmp_path):
    path = tmp_path / "script.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.root_object.update(
        {
            NameObject("/OpenAction"): DictionaryObject(
                {
                    NameObject("/S"): NameObject("/JavaScript"),
                    NameObject("/JS"): TextStringObject("app.alert('unsafe')"),
                }
            )
        }
    )
    with path.open("wb") as stream:
        writer.write(stream)

    with pytest.raises(DocumentValidationError) as excinfo:
        validate_file_content(path, ".pdf", max_pdf_pages=10)

    assert excinfo.value.code == "document_pdf_active_content"


def _write_minimal_docx(path, extra_entries=()):
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>',
        )
        for name, data in extra_entries:
            archive.writestr(name, data)


def test_rejects_docx_whose_uncompressed_size_exceeds_limit_without_reading_it(
    tmp_path, monkeypatch
):
    from app.modules.document import file_validation

    path = tmp_path / "bomb.docx"
    _write_minimal_docx(path, [("word/padding.xml", b"0" * (2 * 1024 * 1024))])
    assert path.stat().st_size < 64 * 1024

    def fail_if_parsed(*_args, **_kwargs):
        raise AssertionError("DOCX parts must not be parsed before size checks")

    monkeypatch.setattr(file_validation.ElementTree, "fromstring", fail_if_parsed)

    with pytest.raises(DocumentValidationError) as excinfo:
        validate_file_content(
            path,
            ".docx",
            max_pdf_pages=10,
            max_docx_uncompressed_bytes=1024 * 1024,
        )

    assert excinfo.value.code == "document_docx_too_large"


def test_rejects_docx_with_too_many_entries(tmp_path):
    path = tmp_path / "many.docx"
    _write_minimal_docx(path, [(f"word/media/{i}.bin", b"x") for i in range(20)])

    with pytest.raises(DocumentValidationError) as excinfo:
        validate_file_content(path, ".docx", max_pdf_pages=10, max_docx_entries=10)

    assert excinfo.value.code == "document_docx_too_large"


def test_accepts_docx_within_limits(tmp_path):
    path = tmp_path / "ok.docx"
    _write_minimal_docx(path)

    metadata = validate_file_content(
        path,
        ".docx",
        max_pdf_pages=10,
        max_docx_uncompressed_bytes=1024 * 1024,
        max_docx_entries=10,
    )

    assert metadata["part_count"] == 2


def test_docx_too_large_maps_to_payload_too_large():
    from app.modules.document.application_service import map_validation_error

    error = map_validation_error(
        DocumentValidationError("document_docx_too_large", "too large")
    )

    assert error.status_code == 413
