from app.modules.document.admin_review import (
    ReviewQueueStatus,
    derive_review_queue_status,
    review_queue_priority,
)


def test_review_queue_status_derives_operator_facing_state():
    assert derive_review_queue_status("validated", None, None) == "ready"
    assert derive_review_queue_status("validated", "parsing", None) == "processing"
    assert derive_review_queue_status("validated", "failed", None) == "failed"
    assert (
        derive_review_queue_status("validated", "awaiting_review", "valid")
        == "needs_review"
    )
    assert (
        derive_review_queue_status("validated", "awaiting_review", "invalid")
        == "invalid"
    )
    assert (
        derive_review_queue_status("validated", "awaiting_review", "approved")
        == "approved"
    )
    assert (
        derive_review_queue_status("validated", "completed", "imported")
        == "imported"
    )
    assert (
        derive_review_queue_status("validated", "awaiting_review", "rejected")
        == "rejected"
    )


def test_rejected_source_document_is_failed_before_run_state():
    assert (
        derive_review_queue_status("rejected", "awaiting_review", "valid")
        == "failed"
    )


def test_items_needing_attention_sort_before_completed_items():
    assert review_queue_priority(ReviewQueueStatus.NEEDS_REVIEW) < (
        review_queue_priority(ReviewQueueStatus.PROCESSING)
    )
    assert review_queue_priority(ReviewQueueStatus.INVALID) < (
        review_queue_priority(ReviewQueueStatus.IMPORTED)
    )
