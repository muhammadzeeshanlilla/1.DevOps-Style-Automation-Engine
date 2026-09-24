"""Synchronous execution and safe lifecycle logs."""

import os

from tasks.events import FileEvent
from tasks.models import TaskDefinition, TaskExecutionResult
from tasks.registry import UnknownTaskType
from utils.logger import get_logger

logger = get_logger()


def _log_label(task):
    password = os.environ.get("SMTP_PASSWORD")
    identity, name = task.id, task.name
    if password:
        identity = identity.replace(password, "[REDACTED]")
        name = name.replace(password, "[REDACTED]")
    # repr escapes line breaks/control characters before logging user text.
    return f"id={identity!r} name={name!r}"


class TaskRunner:
    def __init__(self, registry, notification_service=None, status_observer=None):
        self.registry = registry
        self.notification_service = notification_service
        self.status_observer = status_observer

    def _observe(self, method, *args):
        if self.status_observer is not None:
            try:
                getattr(self.status_observer, method)(*args)
            except Exception:
                logger.warning("Runtime status observation unavailable.")

    def run(self, task, context=None):
        if not isinstance(task, TaskDefinition):
            raise TypeError("TaskRunner requires a validated TaskDefinition")
        if task.enabled:
            self._observe("execution_started", task, context)
        try:
            return self._run(task, context)
        finally:
            if task.enabled:
                self._observe("execution_finished")

    def _run(self, task, context=None):
        if not isinstance(task, TaskDefinition):
            raise TypeError("TaskRunner requires a validated TaskDefinition")
        label = _log_label(task)
        if not task.enabled:
            logger.info("Task skipped | %s", label)
            result = TaskExecutionResult(task.id, "SKIPPED")
            self._observe("result_finalized", result)
            return result
        logger.info("Task started | %s", label)
        try:
            if context is not None and not isinstance(context, FileEvent):
                raise TypeError("Invalid execution context")
            handler = self.registry.resolve(task.type)
            result = handler.execute(task) if context is None else handler.execute(task, context=context)
            if (not isinstance(result, TaskExecutionResult) or result.task_id != task.id
                    or result.status not in ("SUCCESS", "FAILED")):
                result = TaskExecutionResult(task.id, "FAILED", error="Handler returned an invalid execution result.")
        except UnknownTaskType:
            result = TaskExecutionResult(task.id, "FAILED", error="No supported handler is available for this task type.")
        except Exception as error:
            # No raw exception strings/tracebacks: these can embed credentials or bodies.
            category = type(error).__name__
            password = os.environ.get("SMTP_PASSWORD")
            if password:
                category = category.replace(password, "[REDACTED]")
            result = TaskExecutionResult(task.id, "FAILED", error=f"Task handler raised {category} during execution.")
        if result.success:
            logger.info("Task completed | %s", label)
        else:
            logger.error("Task failed | %s", label)
        self._observe("result_finalized", result)
        if self.notification_service is not None:
            self._observe("notification_started")
            try:
                self.notification_service.notify(result)
            except Exception:
                logger.error("Lifecycle notification failed; original task result preserved.")
        return result

    def run_reports(self, tasks):
        """Execute one same-window folder-report group with per-task outcomes."""
        tasks = tuple(tasks)
        if not tasks or any(not isinstance(task, TaskDefinition) or task.type != "folder_report"
                            or not task.enabled for task in tasks):
            raise TypeError("TaskRunner requires enabled folder-report definitions")
        for task in tasks:
            logger.info("Task started | %s", _log_label(task))
        self._observe("execution_started", tasks[0], None)
        try:
            try:
                results = self.registry.resolve("folder_report").execute_many(tasks)
            except Exception as error:
                category = type(error).__name__
                results = tuple(TaskExecutionResult(
                    task.id, "FAILED", error=f"Task handler raised {category} during execution.")
                    for task in tasks)
            for task, result in zip(tasks, results):
                if result.success:
                    logger.info("Task completed | %s", _log_label(task))
                else:
                    logger.error("Task failed | %s", _log_label(task))
                self._observe("result_finalized", result)
                if self.notification_service is not None:
                    self._observe("notification_started")
                    try:
                        self.notification_service.notify(result)
                    except Exception:
                        logger.error("Lifecycle notification failed; original task result preserved.")
            return results
        finally:
            self._observe("execution_finished")
