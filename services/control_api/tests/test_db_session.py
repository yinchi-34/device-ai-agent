from sqlalchemy import text

from app.db.session import async_session_factory


async def test_database_session_executes_select_one():
    async with async_session_factory() as session:
        value = await session.scalar(text("select 1"))
    assert value == 1
