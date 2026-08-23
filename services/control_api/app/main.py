from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.errors import DomainError
from app.modules.device.router import router as device_router
from app.modules.diagnosis.router import router as diagnosis_router
from app.modules.document.router import router as document_router
from app.modules.testing.router import router as testing_router


def create_app() -> FastAPI:
    app = FastAPI(title="Device AI Agent Control API")

    @app.exception_handler(DomainError)
    async def domain_error_handler(
        request: Request, exc: DomainError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.code, "message": exc.message},
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(device_router)
    app.include_router(document_router)
    app.include_router(testing_router)
    app.include_router(diagnosis_router)
    return app


app = create_app()
