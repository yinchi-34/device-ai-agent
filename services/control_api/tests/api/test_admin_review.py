import json

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.main import create_app
from app.modules.document.models import (
    DocumentChunk,
    ProfileExtractionCandidate,
    ProfileExtractionRun,
    SourceDocument,
)
from tests.document.test_candidate_review import make_candidate
from tests.document.test_extraction_pipeline import candidate_payload


async def _admin_client(monkeypatch):
    monkeypatch.setenv("DEVICE_ADMIN_USERNAME", "reviewer")
    monkeypatch.setenv("DEVICE_ADMIN_PASSWORD", "review-secret")
    return AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
        auth=("reviewer", "review-secret"),
    )


async def test_admin_console_requires_credentials(client, monkeypatch):
    monkeypatch.setenv("DEVICE_ADMIN_USERNAME", "reviewer")
    monkeypatch.setenv("DEVICE_ADMIN_PASSWORD", "review-secret")

    response = await client.get("/admin/documents")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Basic"


async def test_admin_document_list_renders_existing_review_item(
    db_session, monkeypatch
):
    await make_candidate(db_session)
    await db_session.commit()

    async with await _admin_client(monkeypatch) as client:
        response = await client.get("/admin/documents")

    assert response.status_code == 200
    assert "manual.docx" in response.text
    assert "awaiting_review" in response.text
    assert "valid" in response.text


async def test_admin_document_list_filters_failed_runs_and_shows_error(
    db_session, monkeypatch
):
    await make_candidate(db_session)
    failed_document = SourceDocument(
        original_file_name="broken-manual.pdf",
        media_type="application/pdf",
        file_size_bytes=10,
        file_hash_sha256="c" * 64,
        storage_uri="original/broken-manual.pdf",
        validation_status="validated",
    )
    db_session.add(failed_document)
    await db_session.flush()
    db_session.add(
        ProfileExtractionRun(
            source_document_id=failed_document.id,
            status="failed",
            extraction_provider="fake",
            extraction_model="extract-v1",
            embedding_provider="fake",
            embedding_model="embed-v1",
            error_code="provider_timeout",
            error_detail={"message": "model timed out"},
        )
    )
    await db_session.commit()

    async with await _admin_client(monkeypatch) as client:
        response = await client.get("/admin/documents?status=failed")

    assert response.status_code == 200
    assert "broken-manual.pdf" in response.text
    assert "provider_timeout" in response.text
    assert "model timed out" in response.text
    assert "manual.docx" not in response.text


async def test_admin_document_list_paginates_results(db_session, monkeypatch):
    for index in range(21):
        db_session.add(
            SourceDocument(
                original_file_name=f"manual-{index:02}.pdf",
                media_type="application/pdf",
                file_size_bytes=10,
                file_hash_sha256=f"{index:064x}",
                storage_uri=f"original/manual-{index:02}.pdf",
                validation_status="validated",
            )
        )
    await db_session.commit()

    async with await _admin_client(monkeypatch) as client:
        first_page = await client.get("/admin/documents?page=1")
        second_page = await client.get("/admin/documents?page=2")

    assert first_page.status_code == 200
    assert second_page.status_code == 200
    assert "manual-20.pdf" in first_page.text
    assert "manual-00.pdf" not in first_page.text
    assert "manual-00.pdf" in second_page.text
    assert "第 2 / 2 页" in second_page.text


async def test_candidate_review_shows_payload_and_traceable_evidence(
    db_session, monkeypatch
):
    candidate = await make_candidate(db_session)
    await db_session.commit()

    async with await _admin_client(monkeypatch) as client:
        response = await client.get(f"/admin/candidates/{candidate.id}")

    assert response.status_code == 200
    assert "Run status register 40001" in response.text
    assert "docx:block:1" in response.text
    assert "reviewed-payload" in response.text


async def test_candidate_evidence_links_target_source_card_and_pdf_page(
    db_session, monkeypatch
):
    candidate = await make_candidate(db_session)
    chunk = await db_session.scalar(
        select(DocumentChunk).where(
            DocumentChunk.extraction_run_id == candidate.extraction_run_id
        )
    )
    chunk.page_number = 12
    chunk.bounding_box = [10.0, 20.0, 100.0, 40.0]
    await db_session.commit()

    async with await _admin_client(monkeypatch) as client:
        response = await client.get(f"/admin/candidates/{candidate.id}")

    assert response.status_code == 200
    assert 'class="evidence-link"' in response.text
    assert 'data-evidence-ref="docx:block:1"' in response.text
    assert 'data-evidence-card="docx:block:1"' in response.text
    assert 'data-page="12"' in response.text
    assert "scrollIntoView" in response.text


async def test_candidate_review_renders_structured_sections_and_change_diff(
    db_session, monkeypatch
):
    candidate = await make_candidate(db_session)
    reviewed = candidate_payload()
    reviewed["profile"]["version"]["value"] = "2.0-reviewed"
    candidate.reviewed_payload = reviewed
    await db_session.commit()

    async with await _admin_client(monkeypatch) as client:
        response = await client.get(f"/admin/candidates/{candidate.id}")

    assert response.status_code == 200
    assert "设备型号" in response.text
    assert "协议" in response.text
    assert "能力匹配" in response.text
    assert 'data-review-path="/profile/version/value"' in response.text
    assert "2.0-reviewed" in response.text
    assert "原值：1.0" in response.text
    assert "人工修改" in response.text
    assert "高级 JSON 编辑" in response.text


async def test_candidate_structured_fields_can_be_saved_and_validated(
    db_session, monkeypatch
):
    candidate = await make_candidate(db_session)
    await db_session.commit()

    async with await _admin_client(monkeypatch) as client:
        page = await client.get(f"/admin/candidates/{candidate.id}")
        saved = await client.post(
            f"/admin/candidates/{candidate.id}/save-fields",
            data={
                "revision": "1",
                "field:/profile/version/value": "2.0-fields",
                "field:/protocols/0/config_schema": '{"type":"object"}',
            },
        )

    assert page.status_code == 200
    assert 'name="field:/profile/version/value"' in page.text
    assert saved.status_code == 303
    refreshed = await db_session.get(ProfileExtractionCandidate, candidate.id)
    await db_session.refresh(refreshed)
    assert refreshed.revision == 2
    assert refreshed.status == "valid"
    assert refreshed.reviewed_payload["profile"]["version"]["value"] == "2.0-fields"
    assert refreshed.reviewed_payload["protocols"][0]["config_schema"] == {
        "type": "object"
    }


async def test_candidate_json_can_be_saved_with_revision_check(
    db_session, monkeypatch
):
    candidate = await make_candidate(db_session)
    await db_session.commit()
    payload = candidate_payload()
    payload["profile"]["version"]["value"] = "2.0-reviewed"

    async with await _admin_client(monkeypatch) as client:
        response = await client.post(
            f"/admin/candidates/{candidate.id}/save",
            data={
                "revision": "1",
                "payload_json": json.dumps(payload),
            },
        )

    assert response.status_code == 303
    refreshed = await db_session.get(ProfileExtractionCandidate, candidate.id)
    await db_session.refresh(refreshed)
    assert refreshed.revision == 2
    assert refreshed.reviewed_payload["profile"]["version"]["value"] == (
        "2.0-reviewed"
    )


async def test_candidate_can_be_approved_then_imported_from_console(
    db_session, monkeypatch
):
    candidate = await make_candidate(db_session)
    await db_session.commit()

    async with await _admin_client(monkeypatch) as client:
        approved = await client.post(
            f"/admin/candidates/{candidate.id}/approve",
            data={"revision": "1"},
        )
        imported = await client.post(f"/admin/candidates/{candidate.id}/import")

    assert approved.status_code == 303
    assert imported.status_code == 303
    refreshed = await db_session.get(ProfileExtractionCandidate, candidate.id)
    await db_session.refresh(refreshed)
    assert refreshed.status == "imported"
    assert refreshed.imported_profile_version_id is not None


async def test_candidate_can_be_rejected_with_review_note(
    db_session, monkeypatch
):
    candidate = await make_candidate(db_session)
    await db_session.commit()

    async with await _admin_client(monkeypatch) as client:
        response = await client.post(
            f"/admin/candidates/{candidate.id}/reject",
            data={"revision": "1", "note": "wrong device model"},
        )

    assert response.status_code == 303
    refreshed = await db_session.get(ProfileExtractionCandidate, candidate.id)
    await db_session.refresh(refreshed)
    assert refreshed.status == "rejected"
    assert refreshed.review_note == "wrong device model"
