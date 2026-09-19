"""Thin process/configuration adapters, never a second automation engine."""

import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from config.loader import ConfigurationError, load_config
from utils.paths import CONFIG_PATH, ENGINE_STATUS_PATH, LOG_FILE, PROJECT_ROOT
from utils.process_manager import ProcessManager
from utils.runtime_status import IDENTITY, read_runtime_snapshot
from api.schemas.responses import (
    ConfiguredTask, LogEntry, LogsResponse, OperationResponse, RuntimeDetails,
    RuntimeObservation, StatusResponse, TasksResponse, TaskTrigger,
)

MAX_LOG_BYTES = 64 * 1024
ENGINE_PYTHON = getattr(sys, "_base_executable", sys.executable) if os.name == "nt" else sys.executable
LOG_HEADER = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\] (.*)$"
)
FIXED_LOG_MESSAGES = {
    "=== Automation Engine STARTING ===": "Engine starting.",
    "=== Automation Engine STARTED ===": "Engine started.",
    "=== Automation Engine STOPPING ===": "Engine stopping.",
    "Engine cleanup completed. All attempted workers have exited.": "Engine cleanup completed; workers exited.",
    "CLI stop request received.": "Cooperative stop request received.",
    "Ctrl+C received; cleaning up engine.": "Cooperative shutdown started.",
    "Scheduler started with configured daily/interval tasks.": "Scheduler started.",
    "Scheduler worker exiting.": "Scheduler worker exiting.",
    "Scheduler stopped.": "Scheduler stopped.",
    "File monitor worker exiting.": "File monitor worker exiting.",
    "File monitor stopped.": "File monitor stopped.",
    "Lifecycle notification SMTP acceptance confirmed.": "Lifecycle notification SMTP acceptance confirmed.",
}
LOG_PREFIXES = (
    ("Task started | ", "Task started."),
    ("Task completed | ", "Task completed."),
    ("Task failed | ", "Task failed."),
    ("Task skipped | ", "Task skipped."),
    ("File monitor started. Watching: ", "File monitor started."),
    ("NEW file detected: ", "New file detected."),
    ("MODIFIED file: ", "Modified file detected."),
    ("DELETED file: ", "Deleted file detected."),
    ("SMTP accepted email to ", "SMTP acceptance confirmed; inbox delivery is not verified."),
    ("Failed to send email; acceptance not confirmed: ", "Email SMTP acceptance not confirmed."),
    ("Cannot send email: ", "Email configuration or credentials unavailable."),
    ("Error reading folder: ", "Folder inspection failed."),
    ("Engine startup or runtime failed: ", "Engine startup or runtime failed."),
    ("Lifecycle notification failed", "Lifecycle notification failed."),
)


class APIError(Exception):
    """Only fixed, public-safe messages may be supplied by this adapter."""

    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def public_id(value):
    """Accept core-validated identities, including already-redacted snapshots."""
    if not isinstance(value, str) or len(value) > 256:
        return "[UNAVAILABLE]"
    if value != "[UNAVAILABLE]" and IDENTITY.fullmatch(value) is None:
        return "[UNAVAILABLE]"
    password = os.environ.get("SMTP_PASSWORD")
    return value.replace(password, "[REDACTED]") if password else value


class EngineAPIService:
    def __init__(self, manager=None, config_loader=None, log_path=None,
                 launcher=None, start_timeout=2.0, stop_timeout=5.0):
        # Overrides support isolated API tests; HTTP callers cannot select paths.
        self.manager = manager if manager is not None else ProcessManager()
        self.config_loader = config_loader if config_loader is not None else lambda: load_config(CONFIG_PATH)
        self.log_path = Path(log_path) if log_path is not None else LOG_FILE
        self.launcher = launcher if launcher is not None else subprocess.Popen
        self.start_timeout = start_timeout
        self.stop_timeout = stop_timeout
        self._operation_lock = threading.Lock()
        self._child = None

    def status(self):
        status = self.manager.inspect_status()
        observation = RuntimeObservation()
        if status.metadata is not None:
            owner = status.metadata
            read = read_runtime_snapshot(self.manager.runtime_dir / ENGINE_STATUS_PATH.name, owner)
            current = self.manager.inspect_status()
            # Exactly the CLI's ownership recheck: never attach a foreign snapshot.
            if (current.metadata is None
                    or current.metadata["instance_id"] != owner["instance_id"]
                    or current.metadata["pid"] != owner["pid"]):
                observation = RuntimeObservation(reason="Engine ownership changed during reading.")
            elif read.snapshot is None:
                observation = RuntimeObservation(reason=read.reason, age_seconds=read.age)
            else:
                snapshot = read.snapshot
                tasks = [{**task, "id": public_id(task["id"])} for task in snapshot["tasks"]]
                execution = snapshot["current"]
                result = snapshot["last_result"]
                details = RuntimeDetails(
                    published_at=snapshot["published_at"], lifecycle=snapshot["lifecycle"],
                    tasks=tasks, configured_count=snapshot["configured_count"],
                    enabled_count=snapshot["enabled_count"],
                    current={**execution, "id": public_id(execution["id"])} if execution else None,
                    last_result={**result, "id": public_id(result["id"])} if result else None,
                    scheduler=snapshot["scheduler"], monitors=snapshot["monitors"],
                    queued_events=snapshot["queued_events"],
                )
                observation = RuntimeObservation(available=True, reason="", age_seconds=read.age, snapshot=details)
            status = current
        # Never return ProcessStatus.detail or arbitrary metadata fields.
        return StatusResponse(state=status.state,
                              pid=status.metadata["pid"] if status.metadata else None,
                              runtime=observation)

    def tasks(self):
        try:
            config = self.config_loader()
        except (ConfigurationError, OSError, ValueError):
            raise APIError(503, "Workflow configuration is unavailable or invalid.") from None
        tasks = []
        for task in config.tasks:
            trigger = task.trigger
            fields = {"type": trigger.type}
            if trigger.type == "daily":
                fields.update(hour=trigger.hour, minute=trigger.minute)
            elif trigger.type == "interval":
                fields["every_minutes"] = trigger.every_minutes
            else:
                fields["events"] = list(trigger.events)
            # Names, parameters, watched paths and SMTP fields are deliberately private.
            tasks.append(ConfiguredTask(id=public_id(task.id), type=task.type,
                                        enabled=task.enabled, trigger=TaskTrigger(**fields)))
        return TasksResponse(count=len(tasks), tasks=tasks)

    def start(self):
        with self._operation_lock:
            before = self.manager.inspect_status()
            if before.state != "STOPPED":
                raise APIError(409, "Engine already owns the runtime, or safe ownership cannot be established.")
            if self._child is not None and self._child.poll() is None:
                raise APIError(409, "An engine start is already pending; check status before retrying.")
            try:
                self.config_loader()  # Reuse validation; do not construct workers here.
            except (ConfigurationError, OSError, ValueError):
                raise APIError(503, "Workflow configuration is unavailable or invalid.") from None
            options = dict(cwd=str(PROJECT_ROOT), stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            else:
                options["start_new_session"] = True
            try:
                # The standard-library CLI needs no API packages. On Windows use
                # the underlying interpreter, not the venv redirector whose PID
                # differs from os.getpid() in the engine ownership metadata.
                self._child = self.launcher([ENGINE_PYTHON, "-B", str(PROJECT_ROOT / "main.py"), "start"], **options)
            except OSError:
                raise APIError(503, "Unable to launch the CLI engine process.") from None
            deadline = time.monotonic() + self.start_timeout
            while True:
                observed = self.manager.inspect_status()
                exited = self._child.poll() is not None
                if not exited and observed.metadata is not None and observed.metadata["pid"] == self._child.pid:
                    return OperationResponse(state=observed.state, detail="CLI engine owns the runtime; check status for worker details.")
                if observed.metadata is not None and observed.metadata["pid"] != self._child.pid:
                    raise APIError(409, "A competing engine instance acquired the runtime; no second engine was started.")
                if exited:
                    raise APIError(503, "CLI engine exited before startup was confirmed; review local engine logs.")
                if time.monotonic() >= deadline:
                    return OperationResponse(state="START_REQUESTED", detail="CLI engine launch requested; startup is not yet confirmed.")
                time.sleep(0.05)

    def stop(self):
        with self._operation_lock:
            if self._child is not None and self._child.poll() is None:
                if self.manager.inspect_status().state == "STOPPED":
                    raise APIError(409, "Engine startup or exit is pending; retry when ownership is observable.")
            result = self.manager.request_stop(timeout=self.stop_timeout)
            if result.state == "STOPPED":
                return 200, OperationResponse(state="STOPPED", detail="Engine runtime ownership is released.")
            if result.state == "REQUESTED":
                return 202, OperationResponse(state="REQUESTED", detail="Cooperative stop requested; worker exit is not yet confirmed. No process was forcibly terminated.")
            raise APIError(409, "Cannot safely target engine ownership for cooperative shutdown.")

    def logs(self, limit):
        try:
            with self.log_path.open("rb") as source:
                source.seek(0, os.SEEK_END)
                size = source.tell()
                start = max(0, size - MAX_LOG_BYTES)
                source.seek(start)
                content = source.read(MAX_LOG_BYTES)
        except FileNotFoundError:
            return LogsResponse(count=0, truncated=False, entries=[])
        except OSError:
            raise APIError(503, "Engine log is unavailable.") from None
        lines = content.decode("utf-8", errors="replace").splitlines()
        if start and lines:
            lines = lines[1:]  # The first bounded-tail line may be incomplete.
        if content and not content.endswith(b"\n") and lines:
            lines = lines[:-1]  # Do not publish a record still being written.
        entries = []
        for line in lines:
            match = LOG_HEADER.fullmatch(line)
            if match is None:
                continue  # Tracebacks and continuation lines are never exposed.
            timestamp, level, raw_message = match.groups()
            try:
                datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            message = FIXED_LOG_MESSAGES.get(raw_message)
            if message is None:
                message = next((summary for prefix, summary in LOG_PREFIXES if raw_message.startswith(prefix)),
                               "Additional log details withheld for privacy.")
            entries.append(LogEntry(timestamp=timestamp, level=level, message=message))
        truncated = bool(start) or len(entries) > limit
        entries = entries[-limit:]
        return LogsResponse(count=len(entries), truncated=truncated, entries=entries)
