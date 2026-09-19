from fastapi import APIRouter, Depends, Response
from api.dependencies import get_service, require_local_origin
from api.schemas.responses import OperationResponse
from api.service import EngineAPIService

router = APIRouter(prefix="/engine", dependencies=[Depends(require_local_origin)])


@router.post("/start", response_model=OperationResponse, status_code=202, tags=["engine"])
def start_engine(service: EngineAPIService = Depends(get_service)):
    return service.start()


@router.post("/stop", response_model=OperationResponse, tags=["engine"])
def stop_engine(response: Response, service: EngineAPIService = Depends(get_service)):
    response.status_code, result = service.stop()
    return result
