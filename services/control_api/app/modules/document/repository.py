from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.document.models import (
    DocumentChunk,
    ProfileExtractionCandidate,
    ProfileExtractionRun,
    SourceDocument,
)


class DocumentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_document(self, document_id: int) -> SourceDocument | None:
        return await self.session.get(SourceDocument, document_id)

    async def get_run(self, run_id: int) -> ProfileExtractionRun | None:
        return await self.session.get(ProfileExtractionRun, run_id)

    async def create_run(self, run: ProfileExtractionRun) -> ProfileExtractionRun:
        self.session.add(run)
        await self.session.flush()
        return run

    async def get_candidate_for_update(
        self, candidate_id: int
    ) -> ProfileExtractionCandidate | None:
        statement = (
            select(ProfileExtractionCandidate)
            .where(ProfileExtractionCandidate.id == candidate_id)
            .with_for_update()
        )
        return await self.session.scalar(statement)

    async def list_run_chunks(self, run_id: int) -> list[DocumentChunk]:
        statement = (
            select(DocumentChunk)
            .where(DocumentChunk.extraction_run_id == run_id)
            .order_by(DocumentChunk.block_index, DocumentChunk.id)
        )
        return list((await self.session.scalars(statement)).all())
