"""The folder-report handler alone owns its collected batch's disposition."""

from pathlib import Path
from threading import local
from types import MappingProxyType

from tasks.email_task import send_folder_report_email
from tasks.models import TaskExecutionResult
from utils.logger import get_logger

_single_report = local()
logger = get_logger()


def send_report_email(config, changes):
    """Compatibility seam for the established single-report tests and callers."""
    return send_folder_report_email(config, ({"task_id": _single_report.task.id,
                                               "folder": _single_report.path,
                                               "changes": tuple(changes)},))


class FolderReportHandler:
    def __init__(self, email_settings, monitors):
        self._email = MappingProxyType(dict(email_settings))
        self._monitors = MappingProxyType({Path(path).resolve(): monitor for path, monitor in monitors.items()})

    def execute(self, task):
        return self.execute_many((task,))[0]

    def execute_many(self, tasks):
        tasks = tuple(tasks)
        prepared = []
        results = {}
        for task in tasks:
            path = Path(task.parameters["path"]).resolve()
            monitor = self._monitors.get(path)
            if monitor is None:
                logger.error("Folder report preparation failed | id=%r", task.id)
                results[task.id] = TaskExecutionResult(
                    task.id, "FAILED", error="No monitor is available for the configured folder.")
                continue
            try:
                batch = monitor.get_and_clear_changes()
            except Exception:
                logger.error("Folder report preparation failed | id=%r", task.id)
                results[task.id] = TaskExecutionResult(
                    task.id, "FAILED", error="The report event batch could not be collected.")
                continue
            prepared.append((task, path, monitor, batch))
        accepted = False
        send_error = None
        restoration_failed = False
        try:
            try:
                if len(prepared) == 1:
                    task, path, _monitor, batch = prepared[0]
                    _single_report.task, _single_report.path = task, path
                    try:
                        accepted = send_report_email({"email": dict(self._email)}, batch) is True
                    finally:
                        del _single_report.task, _single_report.path
                elif prepared:
                    reports = tuple({"task_id": task.id, "folder": path, "changes": tuple(batch)}
                                    for task, path, _monitor, batch in prepared)
                    logger.info("Consolidated folder report started | jobs=%d", len(reports))
                    accepted = send_folder_report_email({"email": dict(self._email)}, reports) is True
                    if accepted:
                        logger.info("Consolidated folder report email accepted | jobs=%d", len(reports))
            except Exception as error:
                send_error = error
        finally:
            if not accepted:
                for task, _path, monitor, batch in prepared:
                    restored = False
                    try:
                        monitor.restore_changes(batch)
                        restored = True
                    except Exception:
                        restoration_failed = True
                    details = {"event_count": len(batch), "events_restored": restored}
                    error = ("Report acceptance was not confirmed; events retained in memory."
                             if restored else
                             "Report acceptance was not confirmed and batch restoration could not be confirmed.")
                    results[task.id] = TaskExecutionResult(
                        task.id, "FAILED", error=error, notification_accepted=False, details=details)
        if accepted:
            for task, _path, _monitor, batch in prepared:
                results[task.id] = TaskExecutionResult(
                    task.id, "SUCCESS", notification_accepted=True,
                    details={"event_count": len(batch), "events_restored": False})
            if len(prepared) > 1:
                logger.info("Consolidated folder report completed | jobs=%d", len(prepared))
        if send_error is not None and len(tasks) == 1 and not restoration_failed:
            raise send_error
        return tuple(results[task.id] for task in tasks)
