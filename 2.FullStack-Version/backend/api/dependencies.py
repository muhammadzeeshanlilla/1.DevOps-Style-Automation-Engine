from fastapi import Request
from api.service import APIError, EngineAPIService

VITE_ORIGIN = "http://localhost:5173"


def get_service(request: Request) -> EngineAPIService:
    return request.app.state.engine_service


def require_local_origin(request: Request):
    # CORS alone does not stop cross-site simple POSTs to an unauthenticated API.
    origin = request.headers.get("origin")
    same_origin = str(request.base_url).rstrip("/")
    if origin is not None and origin not in {VITE_ORIGIN, same_origin}:
        raise APIError(403, "This browser origin cannot control the local engine.")
