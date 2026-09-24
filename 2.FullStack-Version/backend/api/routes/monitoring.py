from fastapi import APIRouter, Depends, Response

from api.dependencies import get_service, require_local_origin
from api.schemas.monitoring import (
    DeleteResponse, EnabledUpdate, FolderValidationRequest,
    FolderSelectionResponse, FolderValidationResponse, MonitoringJobCreate, MonitoringJobResponse,
    MonitoringJobsResponse, MonitoringJobUpdate,
)
from api.service import EngineAPIService

router = APIRouter()


@router.post("/folders/validate", response_model=FolderValidationResponse,
             dependencies=[Depends(require_local_origin)], tags=["monitoring"])
def validate_folder(request: FolderValidationRequest,
                    service: EngineAPIService = Depends(get_service)):
    return service.monitoring.validate_folder(request.path)


@router.post("/folders/select", response_model=FolderSelectionResponse,
             dependencies=[Depends(require_local_origin)], tags=["monitoring"])
def select_folder(service: EngineAPIService = Depends(get_service)):
    return service.select_folder()


@router.get("/monitoring-jobs", response_model=MonitoringJobsResponse, tags=["monitoring"])
def list_jobs(service: EngineAPIService = Depends(get_service)):
    return service.monitoring.list_jobs()


@router.get("/monitoring-jobs/{job_id}", response_model=MonitoringJobResponse, tags=["monitoring"])
def get_job(job_id: str, service: EngineAPIService = Depends(get_service)):
    return service.monitoring.get_job(job_id)


@router.post("/monitoring-jobs", response_model=MonitoringJobResponse, status_code=201,
             dependencies=[Depends(require_local_origin)], tags=["monitoring"])
def create_job(request: MonitoringJobCreate,
               service: EngineAPIService = Depends(get_service)):
    return service.monitoring.create(request)


@router.put("/monitoring-jobs/{job_id}", response_model=MonitoringJobResponse,
            dependencies=[Depends(require_local_origin)], tags=["monitoring"])
def update_job(job_id: str, request: MonitoringJobUpdate,
               service: EngineAPIService = Depends(get_service)):
    return service.monitoring.update(job_id, request)


@router.patch("/monitoring-jobs/{job_id}/enabled", response_model=MonitoringJobResponse,
              dependencies=[Depends(require_local_origin)], tags=["monitoring"])
def set_enabled(job_id: str, request: EnabledUpdate,
                service: EngineAPIService = Depends(get_service)):
    return service.monitoring.set_enabled(job_id, request.enabled)


@router.delete("/monitoring-jobs/{job_id}", response_model=DeleteResponse,
               dependencies=[Depends(require_local_origin)], tags=["monitoring"])
def delete_job(job_id: str, service: EngineAPIService = Depends(get_service)):
    return DeleteResponse(id=service.monitoring.delete(job_id))
