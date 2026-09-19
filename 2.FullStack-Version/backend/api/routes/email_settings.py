from fastapi import APIRouter, Depends

from api.dependencies import get_service, require_local_origin
from api.schemas.email_settings import (
    CredentialDeleteResponse, EmailSettingsResponse, EmailSettingsUpdate,
    EmailTestResponse,
)
from api.service import EngineAPIService

router = APIRouter(prefix="/email-settings")


@router.get("", response_model=EmailSettingsResponse, tags=["settings"])
def read_email_settings(service: EngineAPIService = Depends(get_service)):
    return service.email_settings.read()


@router.put("", response_model=EmailSettingsResponse,
            dependencies=[Depends(require_local_origin)], tags=["settings"])
def update_email_settings(request: EmailSettingsUpdate,
                          service: EngineAPIService = Depends(get_service)):
    return service.email_settings.update(request)


@router.post("/test", response_model=EmailTestResponse,
             dependencies=[Depends(require_local_origin)], tags=["settings"])
def test_email_settings(service: EngineAPIService = Depends(get_service)):
    return service.email_settings.test()


@router.delete("/credential", response_model=CredentialDeleteResponse,
               dependencies=[Depends(require_local_origin)], tags=["settings"])
def forget_email_credential(service: EngineAPIService = Depends(get_service)):
    return service.email_settings.forget()
