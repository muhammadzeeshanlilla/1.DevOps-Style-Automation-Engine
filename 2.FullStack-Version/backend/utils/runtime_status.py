"""Safe observations only; process ownership belongs to ProcessManager."""
import copy
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from utils.paths import ENGINE_STATUS_PATH
from utils.process_manager import _retry_file_operation, INSTANCE_ID

MAX_SNAPSHOT_BYTES = 1024 * 1024
FRESHNESS_SECONDS = 5.0
WORKER_STATES = {"alive", "stopped", "unavailable"}
SOURCES = {"daily", "interval", "file_event"}
TASK_TYPES = {"email", "folder_report", "unavailable"}
IDENTITY = re.compile(r"(?:[a-z0-9_-]|\[REDACTED\])+\Z")


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def safe_id(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9_-]+", value) is None:
        return "[UNAVAILABLE]"
    password = os.environ.get("SMTP_PASSWORD")
    return value.replace(password, "[REDACTED]") if password else value


def worker_state(worker):
    try:
        alive = worker.is_running()
        return "alive" if alive is True else "stopped" if alive is False else "unavailable"
    except Exception:
        return "unavailable"


def _fields(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError("Invalid snapshot structure")


def _time(value):
    if not isinstance(value, str):
        raise ValueError("Invalid timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Timestamp requires timezone")
    return parsed


def _id(value):
    if not isinstance(value, str) or (value != "[UNAVAILABLE]" and IDENTITY.fullmatch(value) is None):
        raise ValueError("Invalid task identity")


def validate_snapshot(data):
    _fields(data, ("version", "instance_id", "pid", "published_at", "lifecycle", "tasks",
                   "configured_count", "enabled_count", "current", "last_result", "scheduler", "monitors", "queued_events"))
    if type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("Unsupported snapshot version")
    if not isinstance(data["instance_id"], str) or INSTANCE_ID.fullmatch(data["instance_id"]) is None:
        raise ValueError("Invalid instance")
    if type(data["pid"]) is not int or data["pid"] <= 0:
        raise ValueError("Invalid PID")
    _time(data["published_at"])
    if data["lifecycle"] not in ("STARTING", "RUNNING", "STOPPING"):
        raise ValueError("Invalid lifecycle")
    if not isinstance(data["tasks"], list):
        raise ValueError("Invalid tasks")
    for task in data["tasks"]:
        _fields(task, ("id", "type", "trigger", "enabled", "schedule"))
        _id(task["id"])
        if task["type"] not in TASK_TYPES or task["trigger"] not in SOURCES or type(task["enabled"]) is not bool:
            raise ValueError("Invalid task fields")
        schedule = task["schedule"]
        if not isinstance(schedule, str):
            raise ValueError("Invalid schedule")
        if task["trigger"] == "daily":
            if re.fullmatch(r"daily (?:[01][0-9]|2[0-3]):[0-5][0-9]", schedule) is None:
                raise ValueError("Invalid daily description")
        elif task["trigger"] == "interval":
            if re.fullmatch(r"every [1-9][0-9]* minutes", schedule) is None:
                raise ValueError("Invalid interval description")
        elif schedule != "file_event":
            raise ValueError("Invalid event description")
    for key, expected in (("configured_count", len(data["tasks"])), ("enabled_count", sum(t["enabled"] for t in data["tasks"]))):
        if type(data[key]) is not int or data[key] != expected:
            raise ValueError("Invalid task count")
    current = data["current"]
    if current is not None:
        _fields(current, ("id", "type", "source", "started_at", "phase"))
        _id(current["id"]); _time(current["started_at"])
        if current["type"] not in TASK_TYPES or current["source"] not in SOURCES or current["phase"] not in ("action", "lifecycle notification"):
            raise ValueError("Invalid execution")
    last = data["last_result"]
    if last is not None:
        _fields(last, ("id", "status", "finished_at"))
        _id(last["id"]); _time(last["finished_at"])
        if last["status"] not in ("SUCCESS", "FAILED", "SKIPPED"):
            raise ValueError("Invalid result")
    if data["scheduler"] not in WORKER_STATES or not isinstance(data["monitors"], list):
        raise ValueError("Invalid workers")
    for index, monitor in enumerate(data["monitors"], 1):
        _fields(monitor, ("label", "state"))
        if monitor["label"] != f"monitor-{index}" or monitor["state"] not in WORKER_STATES:
            raise ValueError("Invalid monitor")
    if type(data["queued_events"]) is not int or data["queued_events"] < 0:
        raise ValueError("Invalid queue count")
    return data


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate snapshot field")
        value[key] = item
    return value


def _read(path):
    def read():
        with Path(path).open("rb") as file:
            return file.read(MAX_SNAPSHOT_BYTES + 1)
    content = _retry_file_operation(read, deadline=time.monotonic() + 0.2)
    if len(content) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Oversized snapshot")
    return validate_snapshot(json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object))


@dataclass(frozen=True)
class SnapshotRead:
    snapshot: object = None
    reason: str = ""
    age: object = None


def read_runtime_snapshot(path, metadata, now=None):
    try:
        data = _read(path)
        if data["instance_id"] != metadata["instance_id"] or data["pid"] != metadata["pid"]:
            return SnapshotRead(reason="Snapshot belongs to another instance")
        age = ((now or datetime.now(timezone.utc)) - _time(data["published_at"])).total_seconds()
        if age < -1:
            return SnapshotRead(reason="Snapshot timestamp is in the future")
        if age > FRESHNESS_SECONDS:
            return SnapshotRead(reason="Runtime details stale", age=age)
        return SnapshotRead(data, age=max(0.0, age))
    except FileNotFoundError:
        return SnapshotRead(reason="Snapshot missing")
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError):
        return SnapshotRead(reason="Snapshot unreadable or invalid")


class RuntimeStatus:
    def __init__(self, tasks, runtime_dir=None):
        self.path = Path(runtime_dir) / ENGINE_STATUS_PATH.name if runtime_dir is not None else ENGINE_STATUS_PATH
        self._tasks = []
        for task in tasks:
            trigger = task.trigger
            description = (f"daily {trigger.hour:02d}:{trigger.minute:02d}" if trigger.type == "daily"
                           else f"every {trigger.every_minutes} minutes" if trigger.type == "interval" else "file_event")
            self._tasks.append({"id": safe_id(task.id), "type": task.type if task.type in TASK_TYPES else "unavailable",
                                "trigger": trigger.type, "enabled": task.enabled, "schedule": description})
        self._lock = threading.Lock()
        self._publication_lock = threading.Lock()
        self._identity = None
        self._current = None
        self._last = None

    def bind(self, manager):
        if not manager.owns_instance:
            raise ValueError("Status requires engine ownership")
        with self._publication_lock, self._lock:
            self._identity = (manager.instance_id, os.getpid())
            self._current = None
            self._last = None

    def execution_started(self, task, context=None):
        with self._lock:
            self._current = {"id": safe_id(task.id), "type": task.type if task.type in TASK_TYPES else "unavailable",
                             "source": "file_event" if context is not None else task.trigger.type,
                             "started_at": timestamp(), "phase": "action"}

    def result_finalized(self, result):
        with self._lock:
            self._last = {"id": safe_id(result.task_id), "status": result.status, "finished_at": timestamp()}

    def notification_started(self):
        with self._lock:
            if self._current is not None:
                self._current["phase"] = "lifecycle notification"

    def execution_finished(self):
        with self._lock:
            self._current = None

    def snapshot(self, lifecycle, scheduler="unavailable", monitors=(), queued_events=0):
        with self._lock:
            if self._identity is None:
                raise ValueError("Status is not bound")
            data = {"version": 1, "instance_id": self._identity[0], "pid": self._identity[1],
                    "published_at": timestamp(), "lifecycle": lifecycle,
                    "tasks": copy.deepcopy(self._tasks), "configured_count": len(self._tasks),
                    "enabled_count": sum(t["enabled"] for t in self._tasks),
                    "current": copy.deepcopy(self._current), "last_result": copy.deepcopy(self._last),
                    "scheduler": scheduler, "monitors": list(monitors), "queued_events": queued_events}
        # Redact also at publication in case the environment changed since startup.
        password = os.environ.get("SMTP_PASSWORD")
        if password:
            for task in data["tasks"]: task["id"] = task["id"].replace(password, "[REDACTED]")
            for record in (data["current"], data["last_result"]):
                if record is not None: record["id"] = record["id"].replace(password, "[REDACTED]")
        return validate_snapshot(data)

    def _owned(self, manager):
        return manager.owns_instance and self._identity is not None and manager.instance_id == self._identity[0]

    def publish(self, manager, lifecycle, scheduler="unavailable", monitors=(), queued_events=0):
        with self._publication_lock:
            if not self._owned(manager):
                return False
            data = self.snapshot(lifecycle, scheduler, monitors, queued_events)
            serialized = json.dumps(data, allow_nan=False)
            if len(serialized.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
                raise ValueError("Snapshot exceeds size limit")
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                        prefix="engine.status.", suffix=".tmp", delete=False) as file:
                    temporary = Path(file.name)
                    file.write(serialized)
                    file.flush()
                _retry_file_operation(lambda: os.replace(temporary, self.path))
            finally:
                if temporary is not None:
                    _retry_file_operation(lambda: temporary.unlink(missing_ok=True))
            return True

    def cleanup(self, manager):
        with self._publication_lock:
            if not self._owned(manager):
                return False
            try:
                data = _read(self.path)
            except FileNotFoundError:
                return True
            if data["instance_id"] != self._identity[0] or data["pid"] != self._identity[1]:
                return False
            _retry_file_operation(lambda: self.path.unlink(missing_ok=True))
            return True
