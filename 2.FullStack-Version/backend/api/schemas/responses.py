from typing import Literal, Optional
from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: Literal["automation-engine-api"] = "automation-engine-api"
    api_version: Literal[1] = 1


class RuntimeTask(BaseModel):
    id: str
    type: Literal["email", "folder_report", "unavailable"]
    trigger: Literal["daily", "interval", "file_event"]
    enabled: bool
    schedule: str


class CurrentExecution(BaseModel):
    id: str
    type: Literal["email", "folder_report", "unavailable"]
    source: Literal["daily", "interval", "file_event"]
    started_at: str
    phase: Literal["action", "lifecycle notification"]


class LastResult(BaseModel):
    id: str
    status: Literal["SUCCESS", "FAILED", "SKIPPED"]
    finished_at: str


class MonitorStatus(BaseModel):
    label: str
    state: Literal["alive", "stopped", "unavailable"]


class RuntimeDetails(BaseModel):
    published_at: str
    lifecycle: Literal["STARTING", "RUNNING", "STOPPING"]
    tasks: list[RuntimeTask]
    configured_count: int
    enabled_count: int
    current: Optional[CurrentExecution] = None
    last_result: Optional[LastResult] = None
    scheduler: Literal["alive", "stopped", "unavailable"]
    monitors: list[MonitorStatus]
    queued_events: int


class RuntimeObservation(BaseModel):
    available: bool = False
    reason: str = "No owned engine instance."
    age_seconds: Optional[float] = None
    snapshot: Optional[RuntimeDetails] = None


class StatusResponse(BaseModel):
    state: Literal["STOPPED", "STARTING", "RUNNING", "STOPPING", "UNKNOWN", "LEGACY"]
    pid: Optional[int] = None
    runtime: RuntimeObservation


class OperationResponse(BaseModel):
    state: Literal["STARTING", "RUNNING", "STOPPING", "START_REQUESTED", "STOPPED", "REQUESTED"]
    detail: str


class TaskTrigger(BaseModel):
    type: Literal["daily", "interval", "file_event"]
    hour: Optional[int] = None
    minute: Optional[int] = None
    every_minutes: Optional[int] = None
    events: Optional[list[Literal["new", "modified", "deleted"]]] = None


class ConfiguredTask(BaseModel):
    id: str
    type: Literal["email", "folder_report"]
    enabled: bool
    trigger: TaskTrigger


class TasksResponse(BaseModel):
    source: Literal["configuration_on_disk"] = "configuration_on_disk"
    schema_version: Literal[2] = 2
    changes_require_restart: Literal[True] = True
    count: int
    tasks: list[ConfiguredTask]


class LogEntry(BaseModel):
    timestamp: str
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    message: str


class LogsResponse(BaseModel):
    source: Literal["sanitized_engine_log"] = "sanitized_engine_log"
    count: int
    truncated: bool
    entries: list[LogEntry]
