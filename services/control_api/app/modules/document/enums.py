from enum import StrEnum


class SourceDocumentStatus(StrEnum):
    VALIDATED = "validated"
    REJECTED = "rejected"


class ExtractionRunStatus(StrEnum):
    QUEUED = "queued"
    VALIDATING = "validating"
    PARSING = "parsing"
    EXTRACTING = "extracting"
    VALIDATING_OUTPUT = "validating_output"
    AWAITING_REVIEW = "awaiting_review"
    RETRY_SCHEDULED = "retry_scheduled"
    IMPORTING = "importing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


TERMINAL_RUN_STATUSES = frozenset(
    {
        ExtractionRunStatus.COMPLETED.value,
        ExtractionRunStatus.FAILED.value,
        ExtractionRunStatus.CANCELED.value,
    }
)


class CandidateStatus(StrEnum):
    PENDING = "pending"
    VALID = "valid"
    INVALID = "invalid"
    APPROVED = "approved"
    REJECTED = "rejected"
    IMPORTED = "imported"


class ReviewAction(StrEnum):
    SAVED = "saved"
    APPROVED = "approved"
    REJECTED = "rejected"
    REOPENED = "reopened"
    IMPORTED = "imported"


class ChunkContentType(StrEnum):
    TEXT = "text"
    HEADING = "heading"
    TABLE = "table"
    TABLE_ROW = "table_row"
    IMAGE_CAPTION = "image_caption"


class ExtractionMethod(StrEnum):
    PDF_TEXT = "pdf_text"
    DOCX_XML = "docx_xml"
    VISION_OCR = "vision_ocr"
    MULTIMODAL = "multimodal"
