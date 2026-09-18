"""Read-only configuration data; these models do not execute tasks.

Construct validated instances through config.loader. Validation belongs there,
so the models stay small and independent of files, clocks and worker threads.
"""

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Union


@dataclass(frozen=True)
class DailyTrigger:
    hour: int
    minute: int
    type: str = field(default="daily", init=False)


@dataclass(frozen=True)
class IntervalTrigger:
    every_minutes: int
    type: str = field(default="interval", init=False)


@dataclass(frozen=True)
class FileEventTrigger:
    path: Path
    events: tuple[str, ...]
    type: str = field(default="file_event", init=False)

    def __post_init__(self):
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "events", tuple(self.events))


Trigger = Union[DailyTrigger, IntervalTrigger, FileEventTrigger]


@dataclass(frozen=True)
class TaskDefinition:
    id: str
    name: str
    type: str
    enabled: bool
    trigger: Trigger
    parameters: Mapping[str, Union[str, Path]] = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True)
class NotificationSettings:
    enabled: bool = False
    notify_on_success: bool = True
    notify_on_failure: bool = True

    def __post_init__(self):
        if any(type(value) is not bool for value in
               (self.enabled, self.notify_on_success, self.notify_on_failure)):
            raise TypeError("Notification settings must be booleans")


@dataclass(frozen=True)
class WorkflowConfiguration:
    email: Mapping[str, Union[str, int]] = field(repr=False)
    tasks: tuple[TaskDefinition, ...]
    warnings: tuple[str, ...] = ()
    schema_version: int = field(default=2, init=False)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)

    def __post_init__(self):
        object.__setattr__(self, "email", MappingProxyType(dict(self.email)))
        object.__setattr__(self, "tasks", tuple(self.tasks))
        object.__setattr__(self, "warnings", tuple(self.warnings))


@dataclass(frozen=True)
class TaskExecutionResult:
    """One attempt's outcome; SMTP acceptance is not proof of inbox delivery."""

    task_id: str
    status: str
    error: Union[str, None] = field(default=None, repr=False)
    notification_accepted: Union[bool, None] = None
    details: Mapping[str, Union[str, int, bool, None]] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("Result task_id must be a nonempty string")
        if self.status not in ("SUCCESS", "FAILED", "SKIPPED"):
            raise ValueError("Result status must be SUCCESS, FAILED or SKIPPED")
        if self.error is not None and not isinstance(self.error, str):
            raise ValueError("Result error must be a string or None")
        if self.notification_accepted is not None and not isinstance(self.notification_accepted, bool):
            raise ValueError("Result notification_accepted must be a boolean or None")
        if self.status == "SUCCESS" and self.error is not None:
            raise ValueError("Successful results must not contain an error")
        if self.status == "SKIPPED" and self.notification_accepted is not None:
            raise ValueError("Skipped results must not contain a notification outcome")
        details = dict(self.details)
        if any(not isinstance(key, str) or (value is not None and not isinstance(value, (str, int, bool)))
               for key, value in details.items()):
            raise ValueError("Result details must contain string keys and immutable scalar values")
        object.__setattr__(self, "details", MappingProxyType(details))

    @property
    def success(self):
        return self.status == "SUCCESS"
