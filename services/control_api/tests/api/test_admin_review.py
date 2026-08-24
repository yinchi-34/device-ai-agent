import json

from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.modules.document.models import ProfileExtractionCandidate
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
