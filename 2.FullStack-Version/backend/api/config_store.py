"""Validated, atomic configuration updates for the monitoring-job API."""

import json
import os
import tempfile
from pathlib import Path

from config.loader import ConfigurationError, load_config, validate_config
from utils.paths import CONFIG_PATH, resolve_project_path
from api.schemas.monitoring import (
    FolderValidationResponse, MonitoringJobResponse,
    MonitoringJobsResponse, ScheduleResponse,
)
from api.service import APIError


MUTATION_BLOCKED = "Stop the engine before modifying monitoring configuration."


def _trigger_json(trigger):
    if trigger.type == "daily":
        return {"type": "daily", "hour": trigger.hour, "minute": trigger.minute}
    if trigger.type == "interval":
        return {"type": "interval", "every_minutes": trigger.every_minutes}
    return {"type": "file_event", "path": str(trigger.path), "events": list(trigger.events)}


def _config_json(config):
    return {
        "schema_version": 2,
        "email": dict(config.email),
        "tasks": [{
            "id": task.id,
            "name": task.name,
            "type": task.type,
            "enabled": task.enabled,
            "trigger": _trigger_json(task.trigger),
            "parameters": {key: str(value) for key, value in task.parameters.items()},
        } for task in config.tasks],
        "notifications": {
            "enabled": config.notifications.enabled,
            "notify_on_success": config.notifications.notify_on_success,
            "notify_on_failure": config.notifications.notify_on_failure,
        },
    }


def _job(task):
    trigger = task.trigger
    schedule = (ScheduleResponse(type="daily", hour=trigger.hour, minute=trigger.minute)
                if trigger.type == "daily"
                else ScheduleResponse(type="interval", every_minutes=trigger.every_minutes))
    return MonitoringJobResponse(
        id=task.id, folder_path=str(task.parameters["path"]),
        enabled=task.enabled, schedule=schedule,
    )


class MonitoringConfigStore:
    def __init__(self, manager, operation_lock, config_path=None, replace=None):
        self.manager = manager
        self.operation_lock = operation_lock
        self.config_path = Path(config_path) if config_path is not None else CONFIG_PATH
        self.replace = replace if replace is not None else os.replace

    def _load(self):
        try:
            return load_config(self.config_path)
        except (ConfigurationError, OSError, ValueError):
            raise APIError(503, "Workflow configuration is unavailable or invalid.") from None

    def list_jobs(self):
        jobs = [_job(task) for task in self._load().tasks if task.type == "folder_report"]
        return MonitoringJobsResponse(count=len(jobs), jobs=jobs)

    def get_job(self, job_id):
        task = next((item for item in self._load().tasks
                     if item.type == "folder_report" and item.id == job_id), None)
        if task is None:
            raise APIError(404, "Monitoring job was not found.")
        return _job(task)

    @staticmethod
    def as_json(config):
        return _config_json(config)

    def validate_folder(self, supplied):
        value = supplied.strip()
        if not value or "\x00" in value:
            return FolderValidationResponse(valid=False, exists=False, is_directory=False, accessible=False)
        try:
            path = resolve_project_path(value)
            exists = path.exists()
            is_directory = path.is_dir()
            accessible = False
            if is_directory:
                with os.scandir(path):
                    accessible = True
            return FolderValidationResponse(
                valid=exists and is_directory and accessible,
                exists=exists, is_directory=is_directory, accessible=accessible,
            )
        except (OSError, ValueError):
            return FolderValidationResponse(valid=False, exists=False, is_directory=False, accessible=False)

    @staticmethod
    def _schedule(schedule):
        if schedule.type == "daily":
            return {"type": "daily", "hour": schedule.hour, "minute": schedule.minute}
        multiplier = 60 if schedule.type == "hours" else 1
        return {"type": "interval", "every_minutes": schedule.every * multiplier}

    def _require_folder(self, value):
        result = self.validate_folder(value)
        if not result.valid:
            raise APIError(400, "Folder path must identify an existing accessible directory.")
        return str(resolve_project_path(value.strip()))

    def _write(self, data):
        try:
            validate_config(data)
        except ConfigurationError:
            raise APIError(400, "Monitoring configuration is invalid.") from None
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.config_path.parent,
                prefix=f"{self.config_path.name}.", suffix=".tmp", delete=False,
            ) as destination:
                temporary = Path(destination.name)
                json.dump(data, destination, indent=2)
                destination.write("\n")
                destination.flush()
                os.fsync(destination.fileno())
            self.replace(temporary, self.config_path)
        except OSError:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            raise APIError(503, "Configuration could not be saved; the previous configuration was preserved.") from None

    def _mutate(self, change):
        with self.operation_lock:
            if self.manager.inspect_status().state != "STOPPED":
                raise APIError(409, MUTATION_BLOCKED)
            config = self._load()
            data = _config_json(config)
            try:
                result = change(data)
            except ConfigurationError:
                raise APIError(400, "Monitoring configuration is invalid.") from None
            self._write(data)
            return result

    def create(self, request):
        def change(data):
            folder = self._require_folder(request.folder_path)
            if any(task["id"] == request.id for task in data["tasks"]):
                raise APIError(409, "A task or monitoring job already uses this ID.")
            task = {
                "id": request.id,
                "name": f"Monitoring job {request.id}",
                "type": "folder_report",
                "enabled": request.enabled,
                "trigger": self._schedule(request.schedule),
                "parameters": {"path": folder},
            }
            data["tasks"].append(task)
            validate_config(data)
            return _job(validate_config({"schema_version": 2, "email": data["email"],
                                         "tasks": [task]}).tasks[0])

        return self._mutate(change)

    def update(self, job_id, request):
        def change(data):
            folder = self._require_folder(request.folder_path)
            task = next((item for item in data["tasks"]
                         if item["type"] == "folder_report" and item["id"] == job_id), None)
            if task is None:
                raise APIError(404, "Monitoring job was not found.")
            task.update(enabled=request.enabled, trigger=self._schedule(request.schedule),
                        parameters={"path": folder})
            validated = validate_config(data)
            return _job(next(item for item in validated.tasks if item.id == job_id))

        return self._mutate(change)

    def set_enabled(self, job_id, enabled):
        def change(data):
            task = next((item for item in data["tasks"]
                         if item["type"] == "folder_report" and item["id"] == job_id), None)
            if task is None:
                raise APIError(404, "Monitoring job was not found.")
            task["enabled"] = enabled
            validated = validate_config(data)
            return _job(next(item for item in validated.tasks if item.id == job_id))

        return self._mutate(change)

    def delete(self, job_id):
        def change(data):
            index = next((index for index, item in enumerate(data["tasks"])
                          if item["type"] == "folder_report" and item["id"] == job_id), None)
            if index is None:
                raise APIError(404, "Monitoring job was not found.")
            data["tasks"].pop(index)
            return job_id

        return self._mutate(change)
