from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base
from app.db.session import async_session_factory
from app.main import create_app

# Import model packages so Base.metadata is fully populated.
from app.modules import device, testing  # noqa: F401


async def _truncate_all(session: AsyncSession) -> None:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    if tables:
        await session.execute(
            text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
        )
        await session.commit()


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        await _truncate_all(session)
        yield session


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with async_session_factory() as session:
        await _truncate_all(session)
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client
