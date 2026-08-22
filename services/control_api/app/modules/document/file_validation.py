from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile, is_zipfile

from defusedxml import ElementTree
from pypdf import PdfReader
from pypdf.errors import PdfReadError

PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
ALLOWED_MEDIA_TYPES = {".pdf": PDF_MEDIA_TYPE, ".docx": DOCX_MEDIA_TYPE}
_DOCX_RISK_PATH_PARTS = ("word/embeddings/", "word/activex/")
_DOCX_RISK_FILE_NAMES = ("vbaproject.bin",)
_PDF_RISK_KEYS = {"/OpenAction", "/AA", "/JavaScript", "/EmbeddedFiles"}


@dataclass(frozen=True)
class ValidatedDocument:
    suffix: str
    media_type: str
    size_bytes: int
    file_hash_sha256: str
    metadata: dict[str, Any]


class DocumentValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_upload_metadata(
    original_file_name: str,
    declared_media_type: str | None,
    size_bytes: int,
    *,
    max_file_bytes: int,
) -> str:
    suffix = Path(original_file_name).suffix.lower()
    expected_media_type = ALLOWED_MEDIA_TYPES.get(suffix)
    if expected_media_type is None:
        raise DocumentValidationError(
            "document_type_not_allowed", "Only PDF and DOCX files are allowed"
        )
    if size_bytes <= 0:
        raise DocumentValidationError("document_empty", "The document is empty")
    if size_bytes > max_file_bytes:
        raise DocumentValidationError(
            "document_too_large", "The document exceeds the configured size limit"
        )
    if declared_media_type != expected_media_type:
        raise DocumentValidationError(
            "document_type_mismatch",
            "The declared media type does not match the file suffix",
        )
    return suffix


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _contains_pdf_risk(value: object, seen: set[int] | None = None) -> bool:
    seen = seen or set()
    object_id = id(value)
    if object_id in seen:
        return False
    seen.add(object_id)
    try:
        resolved = value.get_object()  # type: ignore[attr-defined]
    except (AttributeError, PdfReadError, RecursionError):
        resolved = value
    if isinstance(resolved, dict):
        if _PDF_RISK_KEYS.intersection(str(key) for key in resolved):
            return True
        return any(_contains_pdf_risk(item, seen) for item in resolved.values())
    if isinstance(resolved, (list, tuple)):
        return any(_contains_pdf_risk(item, seen) for item in resolved)
    return False


def _validate_pdf(path: Path, max_pdf_pages: int) -> dict[str, Any]:
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise DocumentValidationError(
                "document_type_mismatch", "The file is not a PDF document"
            )
    try:
        reader = PdfReader(path, strict=True)
        if reader.is_encrypted:
            raise DocumentValidationError(
                "document_pdf_encrypted", "Encrypted PDF files are not allowed"
            )
        page_count = len(reader.pages)
        if page_count > max_pdf_pages:
            raise DocumentValidationError(
                "document_pdf_too_many_pages",
                "The PDF exceeds the configured page limit",
            )
        if _contains_pdf_risk(reader.trailer):
            raise DocumentValidationError(
                "document_pdf_active_content",
                "PDF active or embedded content is not allowed",
            )
    except DocumentValidationError:
        raise
    except (PdfReadError, ValueError, TypeError, OSError) as exc:
        raise DocumentValidationError(
            "document_pdf_invalid", "The PDF structure is invalid"
        ) from exc
    return {"page_count": page_count}


def _validate_docx(path: Path) -> dict[str, Any]:
    if not is_zipfile(path):
        raise DocumentValidationError(
            "document_type_mismatch", "The file is not a DOCX document"
        )
    try:
        with ZipFile(path) as archive:
            names = archive.namelist()
            lowered_names = [name.lower() for name in names]
            required = {"[content_types].xml", "word/document.xml"}
            if not required.issubset(lowered_names):
                raise DocumentValidationError(
                    "document_docx_invalid", "Required DOCX parts are missing"
                )
            if any(
                name.endswith(_DOCX_RISK_FILE_NAMES)
                or any(part in name for part in _DOCX_RISK_PATH_PARTS)
                for name in lowered_names
            ):
                raise DocumentValidationError(
                    "document_docx_active_content",
                    "DOCX macros, embeddings, and ActiveX are not allowed",
                )
            for name in names:
                if name.lower().endswith((".xml", ".rels")):
                    root = ElementTree.fromstring(archive.read(name))
                    if name.lower().endswith(".rels") and any(
                        element.attrib.get("TargetMode", "").lower() == "external"
                        for element in root.iter()
                    ):
                        raise DocumentValidationError(
                            "document_docx_external_relationship",
                            "External DOCX relationships are not allowed",
                        )
    except DocumentValidationError:
        raise
    except (BadZipFile, KeyError, OSError, ElementTree.ParseError) as exc:
        raise DocumentValidationError(
            "document_docx_invalid", "The DOCX structure is invalid"
        ) from exc
    return {"part_count": len(names)}


def validate_file_content(
    path: Path, suffix: str, *, max_pdf_pages: int
) -> dict[str, Any]:
    normalized_suffix = suffix.lower()
    if normalized_suffix == ".pdf":
        return _validate_pdf(path, max_pdf_pages)
    if normalized_suffix == ".docx":
        return _validate_docx(path)
    raise DocumentValidationError(
        "document_type_not_allowed", "Only PDF and DOCX files are allowed"
    )


def validate_stored_document(
    path: Path,
    original_file_name: str,
    declared_media_type: str | None,
    *,
    max_file_bytes: int,
    max_pdf_pages: int,
) -> ValidatedDocument:
    size_bytes = path.stat().st_size
    suffix = validate_upload_metadata(
        original_file_name,
        declared_media_type,
        size_bytes,
        max_file_bytes=max_file_bytes,
    )
    metadata = validate_file_content(path, suffix, max_pdf_pages=max_pdf_pages)
    return ValidatedDocument(
        suffix=suffix,
        media_type=ALLOWED_MEDIA_TYPES[suffix],
        size_bytes=size_bytes,
        file_hash_sha256=sha256_file(path),
        metadata=metadata,
    )
