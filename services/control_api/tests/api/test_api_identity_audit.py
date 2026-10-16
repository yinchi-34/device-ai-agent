from sqlalchemy import select

from app.modules.document.models import CandidateReviewEvent
from tests import oidc
from tests.document.test_candidate_review import make_candidate


async def test_candidate_review_records_authenticated_actor(client, db_session):
    candidate = await make_candidate(db_session)
    await db_session.commit()

    response = await client.post(
        f"/api/v1/extraction-candidates/{candidate.id}/reject",
        json={"revision": 1, "note": "wrong register map"},
        headers=oidc.auth_headers(email="alice@example.com"),
    )

    assert response.status_code == 200
    events = (
        await db_session.scalars(
            select(CandidateReviewEvent).where(
                CandidateReviewEvent.candidate_id == candidate.id
            )
        )
    ).all()
    assert [event.actor for event in events] == ["alice@example.com"]
