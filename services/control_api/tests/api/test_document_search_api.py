from app.modules.document import router as document_router
from tests.document.test_document_retrieval import (
    embedding_provider,
    make_document_with_chunk,
)


async def test_search_requires_scope(client):
    response = await client.post(
        "/api/v1/document-search", json={"query": "run status", "limit": 5}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "document_search_scope_required"


async def test_search_returns_scoped_evidence(
    client, db_session, monkeypatch
):
    document = await make_document_with_chunk(
        db_session,
        stable_ref="pdf:page:1:block:1",
        content="run status register",
        vector=[1.0, 0.0, 0.0],
    )
    await db_session.commit()
    monkeypatch.setattr(
        document_router,
        "build_embedding_provider",
        lambda: embedding_provider([1.0, 0.0, 0.0]),
    )

    response = await client.post(
        "/api/v1/document-search",
        json={
            "query": "run status",
            "source_document_id": document.id,
            "limit": 5,
        },
    )

    assert response.status_code == 200
    assert response.json()[0]["stable_ref"] == "pdf:page:1:block:1"
    assert response.json()[0]["similarity"] == 1.0
