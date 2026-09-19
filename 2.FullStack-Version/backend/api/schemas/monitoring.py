from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DailySchedule(StrictModel):
    type: Literal["daily"]
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)


class MinutesSchedule(StrictModel):
    type: Literal["minutes"]
    every: int = Field(ge=1)


class HoursSchedule(StrictModel):
    type: Literal["hours"]
    every: int = Field(ge=1)


ScheduleInput = Annotated[
    Union[DailySchedule, MinutesSchedule, HoursSchedule],
    Field(discriminator="type"),
]


class MonitoringJobCreate(StrictModel):
    id: str = Field(min_length=1, max_length=256, pattern=r"^[a-z0-9_-]+$")
    folder_path: str = Field(min_length=1, max_length=4096)
    schedule: ScheduleInput
    enabled: bool


class MonitoringJobUpdate(StrictModel):
    folder_path: str = Field(min_length=1, max_length=4096)
    schedule: ScheduleInput
    enabled: bool


class EnabledUpdate(StrictModel):
    enabled: bool


class FolderValidationRequest(StrictModel):
    path: str = Field(min_length=1, max_length=4096)


class FolderValidationResponse(BaseModel):
    valid: bool
    exists: bool
    is_directory: bool
    accessible: bool


class ScheduleResponse(BaseModel):
    type: Literal["daily", "interval"]
    hour: int | None = None
    minute: int | None = None
    every_minutes: int | None = None


class MonitoringJobResponse(BaseModel):
    id: str
    folder_path: str
    enabled: bool
    schedule: ScheduleResponse
    monitored_events: list[Literal["new", "modified", "deleted"]] = [
        "new", "modified", "deleted"
    ]


class MonitoringJobsResponse(BaseModel):
    count: int
    jobs: list[MonitoringJobResponse]


class DeleteResponse(BaseModel):
    deleted: Literal[True] = True
    id: str

