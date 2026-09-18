"""The folder-report handler alone owns its collected batch's disposition."""

from pathlib import Path
from types import MappingProxyType

from tasks.email_task import send_report_email
from tasks.models import TaskExecutionResult


class FolderReportHandler:
    def __init__(self, email_settings, monitors):
        self._email = MappingProxyType(dict(email_settings))
        self._monitors = MappingProxyType({Path(path).resolve(): monitor for path, monitor in monitors.items()})

    def execute(self, task):
        monitor = self._monitors.get(Path(task.parameters["path"]).resolve())
        if monitor is None:
            return TaskExecutionResult(task.id, "FAILED", error="No monitor is available for the configured folder.")
        batch = monitor.get_and_clear_changes()
        accepted = False
        restored = False
        send_error = None
        restoration_failed = False
        try:
            try:
                accepted = send_report_email({"email": dict(self._email)}, batch) is True
            except Exception as error:
                send_error = error
        finally:
            # Exactly one restoration path. Lifecycle interrupts still propagate.
            if not accepted:
                try:
                    monitor.restore_changes(batch)
                    restored = True
                except Exception:
                    restoration_failed = True
        details = {"event_count": len(batch), "events_restored": restored}
        if restoration_failed:
            return TaskExecutionResult(task.id, "FAILED",
                error="Report acceptance was not confirmed and batch restoration could not be confirmed.",
                notification_accepted=False, details=details)
        if send_error is not None:
            # The runner contains this error after the batch has been restored.
            raise send_error
        return TaskExecutionResult(task.id, "SUCCESS" if accepted else "FAILED",
            error=None if accepted else "Report acceptance was not confirmed; events retained in memory.",
            notification_accepted=accepted, details=details)
