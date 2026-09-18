"""Only explicitly imported built-in handlers can be selected by task JSON."""

from types import MappingProxyType

from tasks.email_action import EmailHandler
from tasks.report_task import FolderReportHandler


class UnknownTaskType(LookupError):
    """Safe lookup failure that does not echo an untrusted type value."""


class TaskRegistry:
    def __init__(self, email_settings, monitors):
        self._handlers = MappingProxyType({
            "folder_report": FolderReportHandler(email_settings, monitors),
            "email": EmailHandler(email_settings),
        })

    def resolve(self, task_type):
        try:
            return self._handlers[task_type]
        except (KeyError, TypeError):
            raise UnknownTaskType("Unsupported task type; only folder_report and email are available.") from None
