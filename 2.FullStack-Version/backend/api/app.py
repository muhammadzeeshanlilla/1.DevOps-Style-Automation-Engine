"""Run from backend: python -m uvicorn api.app:app --host 127.0.0.1 --port 8000."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from api.dependencies import VITE_ORIGIN
from api.routes import engine, health, observations
from api.service import APIError, EngineAPIService


def create_app(service=None):
    application = FastAPI(title="DevOps-Style Automation Engine API", version="1.0.0",
                          description="Local Phase 1 adapters for the existing CLI engine.", debug=False)
    application.state.engine_service = service if service is not None else EngineAPIService()
    application.include_router(health.router, prefix="/api")
    application.include_router(observations.router, prefix="/api")
    application.include_router(engine.router, prefix="/api")

    @application.exception_handler(APIError)
    async def public_error(request: Request, error: APIError):
        return JSONResponse(status_code=error.status_code, content={"detail": error.detail})

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        # Default validation responses can echo untrusted supplied values.
        return JSONResponse(status_code=422, content={"detail": "Invalid API request parameters."})

    @application.middleware("http")
    async def safe_responses(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            # Local errors may contain private paths or credentials: never serialize them.
            response = JSONResponse(status_code=503, content={"detail": "API operation unavailable."})
        response.headers["Cache-Control"] = "no-store"
        return response

    application.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    # Wrap the whole ASGI app so safe error responses receive CORS headers too.
    return CORSMiddleware(application, allow_origins=[VITE_ORIGIN], allow_credentials=False,
                          allow_methods=["GET", "POST"], allow_headers=["Content-Type"])


app = create_app()
