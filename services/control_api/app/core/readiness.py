"""Readiness checks behind GET /ready.

/health only says the process is up. /ready also checks that the database and
Redis answer, so a container that cannot reach them is not sent traffic.
"""

import asyncio
import logging

import redis.asyncio as redis
from sqlalchemy import text

from app.core.config import Settings
from app.db.session import engine

CHECK_TIMEOUT_SECONDS = 2.0

logger = logging.getLogger("app.readiness")


async def check_database() -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def check_redis() -> None:
    client = redis.from_url(
        Settings().redis_url,
        socket_connect_timeout=CHECK_TIMEOUT_SECONDS,
        socket_timeout=CHECK_TIMEOUT_SECONDS,
    )
    try:
        await client.ping()
    finally:
        await client.aclose()


async def run_checks() -> dict[str, str]:
    results: dict[str, str] = {}
    # Looked up at call time so tests can replace a check on the module.
    for name, check in (("database", check_database), ("redis", check_redis)):
        try:
            await asyncio.wait_for(check(), CHECK_TIMEOUT_SECONDS)
            results[name] = "ok"
        except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
            logger.warning(
                "readiness_check_failed",
                extra={"check": name, "error_type": type(exc).__name__},
            )
            results[name] = "error"
    return results
