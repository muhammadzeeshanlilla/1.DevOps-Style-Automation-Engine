"""Cooperative CLI process control. PIDs are never used to send signals."""

import errno
import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils.paths import ENGINE_LOCK_PATH, ENGINE_PID_PATH, STOP_REQUEST_DIR, STOP_REQUEST_PREFIX

WINDOWS = os.name == "nt"
if WINDOWS:
    import msvcrt
else:
    import fcntl

STATES = {"STARTING", "RUNNING", "STOPPING"}
INSTANCE_ID = re.compile(r"[0-9a-f]{32}\Z")
LEGACY_MESSAGE = (
    "Legacy PID file found. Stop the old engine with Ctrl+C, then remove "
    "engine.pid only after confirming that the old engine has stopped."
)
FILE_RETRY_TIMEOUT = 1.0
FILE_RETRY_INTERVAL = 0.01
STATUS_RECHECK_TIMEOUT = 0.1
OWNERSHIP_RECHECK_TIMEOUT = 0.1
WINDOWS_CONTENTION_ERRORS = {5, 32, 33}


def _retry_file_operation(operation, deadline=None):
    """Retry only potentially transient Windows errors, retaining the first error."""
    limit = time.monotonic() + FILE_RETRY_TIMEOUT
    if deadline is not None:
        limit = min(limit, deadline)
    first_error = None
    while True:
        try:
            return operation()
        except OSError as exc:
            code = getattr(exc, "winerror", None)
            # CRT-backed open() reports EACCES without retaining a Win32 error code.
            contention = code in WINDOWS_CONTENTION_ERRORS or (
                code is None and exc.errno == errno.EACCES
            )
            if not WINDOWS or not contention:
                raise
            if first_error is None:
                first_error = exc
            remaining = limit - time.monotonic()
            if remaining <= 0:
                raise first_error
            time.sleep(min(FILE_RETRY_INTERVAL, remaining))


class ProcessManagementError(Exception):
    """Runtime state could not be safely managed."""


class AlreadyRunning(ProcessManagementError):
    """Another instance owns the project lock."""


class LegacyPIDError(ProcessManagementError):
    """A numeric PID file cannot establish safe instance ownership."""


@dataclass(frozen=True)
class ProcessStatus:
    state: str
    metadata: Optional[dict] = None
    detail: str = ""


class _InstanceLock:
    """Hold one byte on Windows, or an advisory file lock on Unix."""

    def __init__(self, path):
        self.path = path
        self.file = None

    def acquire(self):
        # Never truncate or unlink this file: all instances must lock the same file.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self.file = os.fdopen(fd, "r+b")
        try:
            self.file.seek(0)
            if WINDOWS:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
        return True

    def release(self):
        if self.file is None:
            return
        try:
            self.file.seek(0)
            if WINDOWS:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None


class ProcessManager:
    def __init__(self, runtime_dir=None):
        # An alternate directory is used by isolated tests, never by workflow config.
        self.runtime_dir = Path(runtime_dir) if runtime_dir is not None else STOP_REQUEST_DIR
        self.lock_path = self.runtime_dir / ENGINE_LOCK_PATH.name
        self.pid_path = self.runtime_dir / ENGINE_PID_PATH.name
        self._lock = None
        self._metadata = None

    @property
    def owns_instance(self):
        return self._lock is not None

    @property
    def instance_id(self):
        return self._metadata["instance_id"] if self._metadata else None

    def _stop_path(self, instance_id):
        if not isinstance(instance_id, str) or not INSTANCE_ID.fullmatch(instance_id):
            raise ProcessManagementError("Invalid engine instance identifier.")
        return self.runtime_dir / (STOP_REQUEST_PREFIX + instance_id)

    def _read_metadata(self, deadline=None):
        def read():
            with self.pid_path.open("r", encoding="utf-8") as file:
                return file.read(16385)
        try:
            text = _retry_file_operation(read, deadline)
        except FileNotFoundError:
            return "missing", None
        except UnicodeError:
            return "malformed", None
        if len(text) > 16384:
            return "malformed", None
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            return "malformed", None
        if type(data) is int:
            return "legacy", None
        if not isinstance(data, dict):
            return "malformed", None
        if (
            type(data.get("version")) is not int or data["version"] != 1
            or type(data.get("pid")) is not int or data["pid"] <= 0
            or not isinstance(data.get("instance_id"), str)
            or not INSTANCE_ID.fullmatch(data["instance_id"])
            or not isinstance(data.get("state"), str) or data["state"] not in STATES
            or not isinstance(data.get("started_at"), str)
        ):
            return "malformed", None
        return "valid", data

    def _write_metadata(self, metadata):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.runtime_dir,
                prefix="engine.pid.", suffix=".tmp", delete=False,
            ) as file:
                temporary = Path(file.name)
                json.dump(metadata, file, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            # Retry the same completed file, rather than generating new temporary files.
            _retry_file_operation(lambda: os.replace(temporary, self.pid_path))
        except BaseException as original:
            if temporary is not None:
                try:
                    _retry_file_operation(lambda: temporary.unlink(missing_ok=True))
                except OSError as cleanup_error:
                    # The publication failure remains primary; retain cleanup diagnostics too.
                    raise original from cleanup_error
            raise
        else:
            if temporary is not None:
                _retry_file_operation(lambda: temporary.unlink(missing_ok=True))

    def acquire(self):
        if self.owns_instance:
            raise AlreadyRunning("This instance already owns the engine lock.")
        lock = _InstanceLock(self.lock_path)
        try:
            # A status probe can briefly own this lock while confirming STOPPED.
            # Retry that brief contention; ownership still requires the same exclusive lock.
            deadline = time.monotonic() + OWNERSHIP_RECHECK_TIMEOUT
            while not lock.acquire():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AlreadyRunning("Engine is already running or starting.")
                time.sleep(min(FILE_RETRY_INTERVAL, remaining))
            self._lock = lock
            kind, _ = self._read_metadata()
            if kind == "legacy":
                raise LegacyPIDError(LEGACY_MESSAGE)
            self._metadata = {
                "version": 1,
                "pid": os.getpid(),
                "instance_id": uuid.uuid4().hex,
                "state": "STARTING",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write_metadata(self._metadata)
        except BaseException as exc:
            try:
                lock.release()
            finally:
                self._lock = None
                self._metadata = None
            if isinstance(exc, OSError):
                raise ProcessManagementError("Unable to acquire or publish engine state: " + str(exc)) from exc
            raise

    def publish_state(self, state):
        if not self.owns_instance or state not in STATES:
            raise ProcessManagementError("Cannot publish an unowned or invalid engine state.")
        updated = dict(self._metadata, state=state)
        try:
            self._write_metadata(updated)
        except OSError as exc:
            raise ProcessManagementError("Unable to publish engine state: " + str(exc)) from exc
        self._metadata = updated

    def inspect_status(self, *, deadline=None):
        probe = _InstanceLock(self.lock_path)
        recheck_limit = time.monotonic() + STATUS_RECHECK_TIMEOUT
        if deadline is not None:
            recheck_limit = min(recheck_limit, deadline)
        try:
            while True:
                available = probe.acquire()
                kind, metadata = self._read_metadata(deadline=deadline)
                if not available and kind == "missing":
                    # Always take one fresh snapshot, even when no waiting budget remains.
                    available = probe.acquire()
                    if available:
                        kind, metadata = self._read_metadata(deadline=deadline)
                if kind == "legacy":
                    return ProcessStatus("LEGACY", detail=LEGACY_MESSAGE)
                if available:
                    detail = ""
                    if kind != "missing":
                        detail = "Stale or malformed runtime metadata; no instance holds the engine lock."
                    return ProcessStatus("STOPPED", detail=detail)
                if kind == "valid":
                    return ProcessStatus(metadata["state"], metadata)
                remaining = recheck_limit - time.monotonic()
                if kind != "missing" or remaining <= 0:
                    return ProcessStatus("UNKNOWN", detail="Engine lock is held, but runtime metadata is unavailable or malformed.")
                # A failed probe owns nothing; re-probe rather than assuming disappearance = exit.
                time.sleep(min(FILE_RETRY_INTERVAL, remaining))
        except OSError as exc:
            return ProcessStatus("UNKNOWN", detail="Unable to inspect engine state: " + str(exc))
        finally:
            probe.release()

    def stop_requested(self):
        if not self.owns_instance:
            return False
        try:
            _retry_file_operation(lambda: self._stop_path(self.instance_id).stat())
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ProcessManagementError("Unable to inspect stop request: " + str(exc)) from exc

    def request_stop(self, timeout=5.0, poll_interval=0.1):
        # Initial validation, filesystem retries and polling share the same budget.
        deadline = time.monotonic() + max(0, timeout)
        status = self.inspect_status(deadline=deadline)
        if status.state == "STOPPED":
            return ProcessStatus("STOPPED", detail="Engine is not running.")
        if status.state in {"UNKNOWN", "LEGACY"}:
            return status
        instance_id = status.metadata["instance_id"]
        try:
            fd = _retry_file_operation(
                lambda: os.open(self._stop_path(instance_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                deadline,
            )
            os.close(fd)
        except FileExistsError:
            pass  # Repeated requests for this instance are harmless.
        except OSError as exc:
            return ProcessStatus("UNKNOWN", detail="Unable to write stop request: " + str(exc))
        uncertainty = ""
        while True:
            current = self.inspect_status(deadline=deadline)
            if current.state == "STOPPED" or (
                current.metadata is not None and current.metadata["instance_id"] != instance_id
            ):
                return ProcessStatus("STOPPED", detail="Requested engine instance has released ownership.")
            if current.state == "LEGACY":
                return current
            if current.state == "UNKNOWN":
                uncertainty = current.detail
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = "Stop requested; shutdown was not confirmed before the timeout. No process was forcibly terminated."
                if uncertainty:
                    detail += " Last uncertainty: " + uncertainty
                return ProcessStatus("REQUESTED", status.metadata, detail)
            time.sleep(min(poll_interval, remaining))

    def release(self):
        if not self.owns_instance:
            return
        errors = []
        try:
            try:
                kind, metadata = self._read_metadata()
                if kind == "valid" and metadata["instance_id"] == self.instance_id:
                    _retry_file_operation(lambda: self.pid_path.unlink(missing_ok=True))
            except OSError as exc:
                errors.append(str(exc))
            try:
                if self.instance_id is not None:
                    _retry_file_operation(lambda: self._stop_path(self.instance_id).unlink(missing_ok=True))
            except OSError as exc:
                errors.append(str(exc))
        finally:
            try:
                self._lock.release()
            finally:
                self._lock = None
                self._metadata = None
        if errors:
            raise ProcessManagementError("Ownership released, but runtime cleanup failed: " + "; ".join(errors))
