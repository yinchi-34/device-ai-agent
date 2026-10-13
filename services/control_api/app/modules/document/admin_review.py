from enum import StrEnum

from app.modules.document.enums import (
    CandidateStatus,
    ExtractionRunStatus,
    SourceDocumentStatus,
)

EXTRACTION_RUN_LABELS = {
    ExtractionRunStatus.QUEUED.value: "等待处理",
    ExtractionRunStatus.VALIDATING.value: "校验文档中",
    ExtractionRunStatus.PARSING.value: "解析文档中",
    ExtractionRunStatus.EXTRACTING.value: "AI 提取中",
    ExtractionRunStatus.VALIDATING_OUTPUT.value: "校验 AI 结果中",
    ExtractionRunStatus.AWAITING_REVIEW.value: "等待人工审核",
    ExtractionRunStatus.IMPORTING.value: "导入中",
    ExtractionRunStatus.COMPLETED.value: "已完成",
    ExtractionRunStatus.FAILED.value: "处理失败",
    ExtractionRunStatus.CANCELED.value: "已取消",
}


class ReviewQueueStatus(StrEnum):
    READY = "ready"
    PROCESSING = "processing"
    NEEDS_REVIEW = "needs_review"
    INVALID = "invalid"
    APPROVED = "approved"
    REJECTED = "rejected"
    IMPORTED = "imported"
    FAILED = "failed"


REVIEW_QUEUE_FILTERS = (
    (ReviewQueueStatus.NEEDS_REVIEW, "待审核"),
    (ReviewQueueStatus.INVALID, "校验失败"),
    (ReviewQueueStatus.PROCESSING, "处理中"),
    (ReviewQueueStatus.READY, "待处理"),
    (ReviewQueueStatus.APPROVED, "已批准"),
    (ReviewQueueStatus.REJECTED, "已拒绝"),
    (ReviewQueueStatus.IMPORTED, "已导入"),
    (ReviewQueueStatus.FAILED, "处理失败"),
)

REVIEW_QUEUE_LABELS = dict(REVIEW_QUEUE_FILTERS)
_REVIEW_QUEUE_PRIORITY = {
    status: index for index, (status, _) in enumerate(REVIEW_QUEUE_FILTERS)
}

_ACTIVE_RUN_STATUSES = {
    ExtractionRunStatus.QUEUED.value,
    ExtractionRunStatus.VALIDATING.value,
    ExtractionRunStatus.PARSING.value,
    ExtractionRunStatus.EXTRACTING.value,
    ExtractionRunStatus.VALIDATING_OUTPUT.value,
    ExtractionRunStatus.IMPORTING.value,
}

_CANDIDATE_STATUS_MAP = {
    CandidateStatus.PENDING.value: ReviewQueueStatus.NEEDS_REVIEW,
    CandidateStatus.VALID.value: ReviewQueueStatus.NEEDS_REVIEW,
    CandidateStatus.INVALID.value: ReviewQueueStatus.INVALID,
    CandidateStatus.APPROVED.value: ReviewQueueStatus.APPROVED,
    CandidateStatus.REJECTED.value: ReviewQueueStatus.REJECTED,
    CandidateStatus.IMPORTED.value: ReviewQueueStatus.IMPORTED,
}


def derive_review_queue_status(
    document_status: str,
    run_status: str | None,
    candidate_status: str | None,
) -> ReviewQueueStatus:
    if document_status != SourceDocumentStatus.VALIDATED.value:
        return ReviewQueueStatus.FAILED
    if candidate_status in _CANDIDATE_STATUS_MAP:
        return _CANDIDATE_STATUS_MAP[candidate_status]
    if run_status in {
        ExtractionRunStatus.FAILED.value,
        ExtractionRunStatus.CANCELED.value,
    }:
        return ReviewQueueStatus.FAILED
    if run_status in _ACTIVE_RUN_STATUSES:
        return ReviewQueueStatus.PROCESSING
    if run_status == ExtractionRunStatus.AWAITING_REVIEW.value:
        return ReviewQueueStatus.NEEDS_REVIEW
    return ReviewQueueStatus.READY


def review_queue_priority(status: ReviewQueueStatus) -> int:
    return _REVIEW_QUEUE_PRIORITY[status]
