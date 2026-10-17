import asyncio
import sys
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import auth
from app.db.base import Base
from app.db.session import async_session_factory
from app.main import create_app

# Import model packages so Base.metadata is fully populated.
from app.modules import device, diagnosis, document, testing  # noqa: F401
from tests import oidc


@pytest.fixture(autouse=True)
def _identity_provider(monkeypatch):
    """Point API auth at the in-process test identity provider."""
    monkeypatch.setenv("DEVICE_OIDC_ISSUER", oidc.ISSUER)
    monkeypatch.setenv("DEVICE_OIDC_AUDIENCE", oidc.AUDIENCE)
    monkeypatch.setenv("DEVICE_OIDC_JWKS_URL", oidc.JWKS_URL)

    async def fetch_json(url: str) -> dict:
        assert url == oidc.JWKS_URL
        return oidc.JWKS

    monkeypatch.setattr(auth, "_fetch_json", fetch_json)
    auth.reset_jwks_cache()


def _is_test_database_name(database_name: str) -> bool:
    return database_name.casefold().endswith("_test")


def pytest_asyncio_loop_factories(config, item):
    """Use the selector loop required by psycopg's async API on Windows."""
    del config, item
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


async def _truncate_all(session: AsyncSession) -> None:
    database_name = await session.scalar(text("SELECT current_database()"))
    if not isinstance(database_name, str) or not _is_test_database_name(
        database_name
    ):
        raise RuntimeError(
            "Refusing destructive test setup outside a *_test database; "
            f"connected database is {database_name!r}"
        )
    connection = await session.connection()
    existing_tables = await connection.run_sync(
        lambda sync_connection: set(inspect(sync_connection).get_table_names())
    )
    tables = ", ".join(
        table.name for table in Base.metadata.sorted_tables
        if table.name in existing_tables
    )
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
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=oidc.auth_headers(),
    ) as async_client:
        yield async_client
