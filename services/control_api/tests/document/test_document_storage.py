
import pytest

from app.modules.document.file_validation import DocumentValidationError
from app.modules.document.storage import (
    FileSystemDocumentStorage,
    validate_and_promote,
)


async def test_storage_uses_generated_path_and_preserves_original_name(tmp_path):
    storage = FileSystemDocumentStorage(tmp_path)

    saved = await storage.save_quarantine(
        original_file_name="../../manual.pdf",
        chunks=[b"%PDF-1.7\n"],
    )

    assert saved.path.is_relative_to(tmp_path / "quarantine")
    assert ".." not in saved.path.name
    assert saved.original_file_name == "../../manual.pdf"
    assert saved.size_bytes == 9


async def test_promotion_uses_hash_path_and_open_reads_content(tmp_path):
    storage = FileSystemDocumentStorage(tmp_path)
    saved = await storage.save_quarantine("manual.pdf", [b"content"])

    promoted = storage.promote(saved, "a" * 64)

    assert promoted.storage_uri.startswith("original/")
    assert promoted.path.is_relative_to(tmp_path / "original")
    assert not saved.path.exists()
    with storage.open(promoted.storage_uri) as stream:
        assert stream.read() == b"content"


async def test_failed_validation_removes_only_quarantine_directory(tmp_path):
    storage = FileSystemDocumentStorage(tmp_path)

    with pytest.raises(DocumentValidationError):
        await validate_and_promote(
            storage,
            original_file_name="manual.docx",
            declared_media_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            chunks=[b"%PDF-1.7\n"],
            max_file_bytes=1024,
            max_pdf_pages=10,
        )

    assert list((tmp_path / "quarantine").rglob("raw.*")) == []
    assert not (tmp_path / "original").exists()


async def test_quarantine_write_stops_at_size_limit_and_leaves_nothing(tmp_path):
    storage = FileSystemDocumentStorage(tmp_path)
    written: list[int] = []

    def chunks():
        for _ in range(10):
            written.append(1)
            yield b"x" * 100

    with pytest.raises(DocumentValidationError) as raised:
        await storage.save_quarantine("manual.pdf", chunks(), max_bytes=250)

    assert raised.value.code == "document_too_large"
    assert len(written) == 3
    assert [p for p in (tmp_path / "quarantine").rglob("*") if p.is_file()] == []


async def test_quarantine_accepts_file_exactly_at_size_limit(tmp_path):
    storage = FileSystemDocumentStorage(tmp_path)

    saved = await storage.save_quarantine(
        "manual.pdf", [b"x" * 100, b"x" * 150], max_bytes=250
    )

    assert saved.size_bytes == 250
    assert saved.path.stat().st_size == 250


async def test_validate_and_promote_enforces_size_limit_while_streaming(tmp_path):
    storage = FileSystemDocumentStorage(tmp_path)
    consumed: list[int] = []

    def chunks():
        for _ in range(100):
            consumed.append(1)
            yield b"x" * 1024

    with pytest.raises(DocumentValidationError) as raised:
        await validate_and_promote(
            storage,
            original_file_name="manual.pdf",
            declared_media_type="application/pdf",
            chunks=chunks(),
            max_file_bytes=4096,
            max_pdf_pages=10,
        )

    assert raised.value.code == "document_too_large"
    assert len(consumed) == 5
    assert list((tmp_path / "quarantine").rglob("raw.*")) == []
