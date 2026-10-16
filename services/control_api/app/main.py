from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.auth import require_api_token
from app.core.errors import DomainError
from app.modules.device.router import router as device_router
from app.modules.diagnosis.router import router as diagnosis_router
from app.modules.document.admin_auth import router as admin_auth_router
from app.modules.document.admin_router import router as document_admin_router
from app.modules.document.router import router as document_router
from app.modules.testing.router import router as testing_router


def create_app() -> FastAPI:
    app = FastAPI(title="Device AI Agent Control API")

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
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message},
            headers=exc.headers,
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    api_auth = [Depends(require_api_token)]
    app.include_router(device_router, dependencies=api_auth)
    app.include_router(admin_auth_router)
    app.include_router(document_admin_router)
    app.include_router(document_router, dependencies=api_auth)
    app.include_router(testing_router, dependencies=api_auth)
    app.include_router(diagnosis_router, dependencies=api_auth)
    return app


app = create_app()
