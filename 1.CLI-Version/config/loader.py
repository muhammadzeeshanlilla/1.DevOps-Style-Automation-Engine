"""Validate legacy/task JSON without starting workers or rewriting settings.

Both supported configuration formats normalize into runtime configuration.
"""

import json
import os
import re
from pathlib import Path, PureWindowsPath

from tasks.models import (
    DailyTrigger, FileEventTrigger, IntervalTrigger, TaskDefinition,
    WorkflowConfiguration, NotificationSettings,
)
from utils.paths import CONFIG_PATH, resolve_project_path


TASK_TRIGGERS = {
    "folder_report": {"daily", "interval"},
    "email": {"daily", "interval", "file_event"},
}
EVENT_TYPES = {"new", "modified", "deleted"}
TASK_ID = re.compile(r"[a-z0-9_-]+\Z")


class ConfigurationError(ValueError):
    """A safe, field-specific error containing no supplied configuration values."""


def _fail(field, message):
    raise ConfigurationError(f"{field}: {message}")


def _object(value, fields, field):
    """Every declared field is required; additional fields are rejected."""
    if not isinstance(value, dict):
        _fail(field, "must be an object")
    for key in sorted(fields):
        if key not in value:
            _fail(f"{field}.{key}", "is required")
    if set(value) - fields:
        # Do not echo unknown keys: even a malicious key can contain a secret.
        _fail(field, "contains unexpected fields; only the declared schema fields are allowed")
    return value


def _string(value, field, allow_empty=False, header=False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        _fail(field, "must be a nonempty string" if not allow_empty else "must be a string")
    if header and ("\r" in value or "\n" in value):
        _fail(field, "must not contain header line breaks")
    return value


def _integer(value, field, minimum, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(field, "must be an integer, not a boolean, string or float")
    if value < minimum or (maximum is not None and value > maximum):
        _fail(field, f"must be in range {minimum} to {maximum}" if maximum is not None
              else f"must be at least {minimum}")
    return value


def _folder(value, field, enabled):
    value = _string(value, field)
    if "\x00" in value:
        _fail(field, "contains an invalid path character")
    if os.name == "nt":
        windows = PureWindowsPath(value)
        parts = windows.parts[1:] if windows.anchor else windows.parts
        is_reserved = getattr(os.path, "isreserved", None)
        reserved = is_reserved(value) if is_reserved else any(
            PureWindowsPath(part).is_reserved() for part in parts
        )
        if reserved or any(
            any(character in part for character in '<>:"|?*')
            or (part not in {".", ".."} and part.endswith((".", " ")))
            for part in parts
        ):
            _fail(field, "contains invalid Windows path syntax")
    try:
        path = resolve_project_path(value)
        if enabled and not path.is_dir():
            _fail(field, "must identify an existing watched directory for an enabled task")
        return path
    except (OSError, ValueError) as error:
        if isinstance(error, ConfigurationError):
            raise
        raise ConfigurationError(f"{field}: cannot resolve or inspect the watched directory") from None


def _email(value):
    email = _object(value, {"sender", "receiver", "smtp_server", "smtp_port"}, "email")
    result = {field: _string(email[field], f"email.{field}", header=True)
              for field in ("sender", "receiver", "smtp_server")}
    result["smtp_port"] = _integer(email["smtp_port"], "email.smtp_port", 1, 65535)
    return result


def _trigger(value, field, enabled):
    if not isinstance(value, dict):
        _fail(field, "must be an object")
    kind = _string(value.get("type"), f"{field}.type")
    if kind == "daily":
        _object(value, {"type", "hour", "minute"}, field)
        return DailyTrigger(_integer(value["hour"], f"{field}.hour", 0, 23),
                            _integer(value["minute"], f"{field}.minute", 0, 59))
    if kind == "interval":
        _object(value, {"type", "every_minutes"}, field)
        return IntervalTrigger(_integer(value["every_minutes"], f"{field}.every_minutes", 1))
    if kind == "file_event":
        _object(value, {"type", "path", "events"}, field)
        events = value["events"]
        if not isinstance(events, list) or not events:
            _fail(f"{field}.events", "must be a nonempty list")
        for index, event in enumerate(events):
            if not isinstance(event, str) or event not in EVENT_TYPES:
                _fail(f"{field}.events[{index}]", "must be new, modified or deleted")
        if len(set(events)) != len(events):
            _fail(f"{field}.events", "must not contain duplicate event names")
        return FileEventTrigger(_folder(value["path"], f"{field}.path", enabled), tuple(events))
    _fail(f"{field}.type", "is not a supported trigger type")


def _task(value, index):
    field = f"tasks[{index}]"
    task = _object(value, {"id", "name", "type", "enabled", "trigger", "parameters"}, field)
    identity = _string(task["id"], f"{field}.id")
    if TASK_ID.fullmatch(identity) is None:
        _fail(f"{field}.id", "may contain only lowercase letters, numbers, hyphens and underscores")
    name = _string(task["name"], f"{field}.name")
    kind = _string(task["type"], f"{field}.type")
    if kind not in TASK_TRIGGERS:
        _fail(f"{field}.type", "is not a supported task type")
    if not isinstance(task["enabled"], bool):
        _fail(f"{field}.enabled", "must be a boolean")
    enabled = task["enabled"]
    trigger = _trigger(task["trigger"], f"{field}.trigger", enabled)
    if trigger.type not in TASK_TRIGGERS[kind]:
        _fail(f"{field}.trigger.type", "is not supported for this task type")
    if kind == "folder_report":
        parameters = _object(task["parameters"], {"path"}, f"{field}.parameters")
        parameters = {"path": _folder(parameters["path"], f"{field}.parameters.path", enabled)}
    else:
        parameters = _object(task["parameters"], {"subject", "body"}, f"{field}.parameters")
        parameters = {
            "subject": _string(parameters["subject"], f"{field}.parameters.subject", header=True),
            "body": _string(parameters["body"], f"{field}.parameters.body"),
        }
    return TaskDefinition(identity, name, kind, enabled, trigger, parameters)


def validate_config(data):
    """Normalize a JSON-compatible dictionary into read-only version-2 data."""
    if not isinstance(data, dict):
        _fail("configuration", "must be an object")
    new_format = "schema_version" in data or "tasks" in data
    if new_format and ("watch_folder" in data or "schedule" in data):
        _fail("configuration", "cannot mix legacy workflow fields with versioned task fields")
    if new_format:
        version = _integer(data.get("schema_version"), "schema_version", 0)
        if version != 2:
            _fail("schema_version", "is unsupported; expected version 2")
        _object(data, {"schema_version", "email", "tasks"} | ({"notifications"} if "notifications" in data else set()), "configuration")
        raw_tasks = data["tasks"]
        if not isinstance(raw_tasks, list):
            _fail("tasks", "must be a list")
    else:
        _object(data, {"watch_folder", "email", "schedule"} | ({"notifications"} if "notifications" in data else set()), "configuration")
        schedule = _object(data["schedule"], {"hour", "minute"}, "schedule")
        hour = _integer(schedule["hour"], "schedule.hour", 0, 23)
        minute = _integer(schedule["minute"], "schedule.minute", 0, 59)
        path = _folder(data["watch_folder"], "watch_folder", True)
        raw_tasks = [{"id": "daily-folder-report", "name": "Daily folder report",
                      "type": "folder_report", "enabled": True,
                      "trigger": {"type": "daily", "hour": hour, "minute": minute},
                      "parameters": {"path": str(path)}}]
    email = _email(data["email"])
    tasks = tuple(_task(value, index) for index, value in enumerate(raw_tasks))
    identities = set()
    report_folders = set()
    for index, task in enumerate(tasks):
        if task.id in identities:
            _fail(f"tasks[{index}].id", "duplicates another task ID, including disabled definitions")
        identities.add(task.id)
        if task.enabled and task.type == "folder_report":
            try:
                folder = os.path.normcase(str(task.parameters["path"].resolve()))
            except (OSError, ValueError):
                raise ConfigurationError(f"tasks[{index}].parameters.path: cannot compare watched directories") from None
            if folder in report_folders:
                _fail(f"tasks[{index}].parameters.path", "already has an enabled folder_report consumer")
            report_folders.add(folder)
    warnings = []
    if not any(task.enabled for task in tasks):
        warnings.append("No enabled tasks are defined; the configuration represents an idle engine.")
    elif not os.environ.get("SMTP_PASSWORD"):
        warnings.append("SMTP_PASSWORD is missing or empty; email attempts will fail safely.")
    notifications = NotificationSettings()
    if "notifications" in data:
        values = _object(data["notifications"], {"enabled", "notify_on_success", "notify_on_failure"}, "notifications")
        for field, value in values.items():
            if type(value) is not bool:
                _fail(f"notifications.{field}", "must be a boolean")
        notifications = NotificationSettings(**values)
    return WorkflowConfiguration(email, tasks, tuple(warnings), notifications=notifications)


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("configuration", "contains a duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value):
    _fail("configuration", "contains a nonstandard JSON numeric constant")


def load_config(path=CONFIG_PATH):
    """Read local JSON by default; never modify it or initialize the engine."""
    try:
        with Path(path).open("r", encoding="utf-8-sig") as source:
            data = json.load(source, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
    except json.JSONDecodeError as error:
        raise ConfigurationError(
            f"configuration: invalid JSON at line {error.lineno}, column {error.colno}"
        ) from None
    except (OSError, UnicodeError, TypeError, ValueError) as error:
        if isinstance(error, ConfigurationError):
            raise
        raise ConfigurationError("configuration: cannot read the JSON configuration file") from None
    return validate_config(data)
