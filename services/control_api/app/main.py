import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from app.core import readiness
from app.core.auth import require_identity
from app.core.errors import DomainError
from app.core.logging import configure_logging, request_id_var
from app.modules.device.router import router as device_router
from app.modules.diagnosis.router import router as diagnosis_router
from app.modules.document.admin_auth import router as admin_auth_router
from app.modules.document.admin_router import router as document_admin_router
from app.modules.document.router import router as document_router
from app.modules.testing.router import router as testing_router

http_logger = logging.getLogger("app.http")

# A caller-supplied request id is kept only if it is short and plain.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Runs under uvicorn after it has set up its own logging, so ours wins.
    configure_logging()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Device AI Agent Control API", lifespan=lifespan)

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        incoming = request.headers.get("X-Request-ID", "")
        request_id = incoming if _SAFE_REQUEST_ID.match(incoming) else uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            # The path only: query strings may carry search text.
            http_logger.info(
                "http_request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "request_id": request_id,
                },
            )
            request_id_var.reset(token)

    @app.middleware("http")
    async def add_admin_security_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/admin" or request.url.path.startswith("/admin/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.exception_handler(DomainError)
    async def domain_error_handler(
        request: Request, exc: DomainError
    ) -> JSONResponse:
        content: dict[str, object] = {"code": exc.code, "message": exc.message}
        if exc.details:
            content["details"] = exc.details
        return JSONResponse(
            status_code=exc.status_code,
            content=content,
            headers=exc.headers,
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        checks = await readiness.run_checks()
        is_ready = all(result == "ok" for result in checks.values())
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={"status": "ready" if is_ready else "unavailable", "checks": checks},
        )

    api_auth = [Depends(require_identity)]
    app.include_router(device_router, dependencies=api_auth)
    app.include_router(admin_auth_router)
    app.include_router(document_admin_router)
    app.include_router(document_router, dependencies=api_auth)
    app.include_router(testing_router, dependencies=api_auth)
    app.include_router(diagnosis_router, dependencies=api_auth)
    return app


app = create_app()
