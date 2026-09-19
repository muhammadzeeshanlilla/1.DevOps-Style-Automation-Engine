from fastapi import APIRouter
from api.schemas.responses import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse, tags=["health"])
def health():
    return HealthResponse()
