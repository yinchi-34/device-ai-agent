from collections.abc import AsyncIterable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import rmtree
from typing import BinaryIO, Protocol
from uuid import uuid4

from app.modules.document.file_validation import (
    ValidatedDocument,
    validate_stored_document,
)


@dataclass(frozen=True)
class StoredDocument:
    path: Path
    storage_uri: str
    original_file_name: str
    size_bytes: int


class DocumentStorage(Protocol):
    async def save_quarantine(
        self,
        original_file_name: str,
        chunks: AsyncIterable[bytes] | Iterable[bytes],
    ) -> StoredDocument:
        raise NotImplementedError

    def promote(self, stored: StoredDocument, file_hash: str) -> StoredDocument:
        raise NotImplementedError

    def open(self, storage_uri: str) -> BinaryIO:
        raise NotImplementedError

    def remove_quarantine(self, stored: StoredDocument) -> None:
        raise NotImplementedError


async def _iterate_chunks(
    chunks: AsyncIterable[bytes] | Iterable[bytes],
) -> AsyncIterable[bytes]:
    if isinstance(chunks, AsyncIterable):
        async for chunk in chunks:
            yield chunk
    else:
        for chunk in chunks:
            yield chunk


class FileSystemDocumentStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()

    async def save_quarantine(
        self,
        original_file_name: str,
        chunks: AsyncIterable[bytes] | Iterable[bytes],
    ) -> StoredDocument:
        suffix = Path(original_file_name).suffix.lower()
        if suffix not in {".pdf", ".docx"}:
            suffix = ".bin"
        month = datetime.now(UTC).strftime("%Y%m")
        relative_path = Path("quarantine") / month / str(uuid4()) / f"raw{suffix}"
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=False)
        size_bytes = 0
        try:
            with path.open("xb") as stream:
                async for chunk in _iterate_chunks(chunks):
                    if not isinstance(chunk, bytes):
                        raise TypeError("document chunks must be bytes")
                    stream.write(chunk)
                    size_bytes += len(chunk)
        except BaseException:
            rmtree(path.parent, ignore_errors=True)
            raise
        return StoredDocument(
            path=path,
            storage_uri=relative_path.as_posix(),
            original_file_name=original_file_name,
            size_bytes=size_bytes,
        )

    def promote(self, stored: StoredDocument, file_hash: str) -> StoredDocument:
        if not stored.path.resolve().is_relative_to(self.root / "quarantine"):
            raise ValueError("Only quarantined documents can be promoted")
        month = datetime.now(UTC).strftime("%Y%m")
        suffix = stored.path.suffix
        relative_path = Path("original") / month / file_hash / f"raw{suffix}"
        destination = self.root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            stored.path.unlink()
        else:
            stored.path.replace(destination)
        rmtree(stored.path.parent, ignore_errors=True)
        return StoredDocument(
            path=destination,
            storage_uri=relative_path.as_posix(),
            original_file_name=stored.original_file_name,
            size_bytes=stored.size_bytes,
        )

    def open(self, storage_uri: str) -> BinaryIO:
        path = (self.root / Path(storage_uri)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Storage URI escapes the document root")
        return path.open("rb")

    def remove_quarantine(self, stored: StoredDocument) -> None:
        quarantine_root = (self.root / "quarantine").resolve()
        path = stored.path.resolve()
        if path.is_relative_to(quarantine_root):
            rmtree(path.parent, ignore_errors=True)


async def validate_and_promote(
    storage: FileSystemDocumentStorage,
    *,
    original_file_name: str,
    declared_media_type: str | None,
    chunks: AsyncIterable[bytes] | Iterable[bytes],
    max_file_bytes: int,
    max_pdf_pages: int,
) -> tuple[StoredDocument, ValidatedDocument]:
    stored = await storage.save_quarantine(original_file_name, chunks)
    try:
        validated = validate_stored_document(
            stored.path,
            original_file_name,
            declared_media_type,
            max_file_bytes=max_file_bytes,
            max_pdf_pages=max_pdf_pages,
        )
        promoted = storage.promote(stored, validated.file_hash_sha256)
        return promoted, validated
    except BaseException:
        storage.remove_quarantine(stored)
        raise
