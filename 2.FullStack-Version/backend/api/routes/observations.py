from fastapi import APIRouter, Depends, Query
from api.dependencies import get_service
from api.schemas.responses import LogsResponse, StatusResponse, TasksResponse
from api.service import EngineAPIService

router = APIRouter()


@router.get("/status", response_model=StatusResponse, response_model_exclude_none=True, tags=["engine"])
def status(service: EngineAPIService = Depends(get_service)):
    return service.status()


@router.get("/tasks", response_model=TasksResponse, response_model_exclude_none=True, tags=["tasks"])
def tasks(service: EngineAPIService = Depends(get_service)):
    return service.tasks()


@router.get("/logs", response_model=LogsResponse, tags=["logs"])
def logs(limit: int = Query(default=100, ge=1, le=200), service: EngineAPIService = Depends(get_service)):
    return service.logs(limit)
