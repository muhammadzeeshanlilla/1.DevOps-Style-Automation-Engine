"""Serialized native directory selection isolated from the API process."""

import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass

from utils.paths import PROJECT_ROOT

PICKER_PYTHON = getattr(sys, "_base_executable", sys.executable) if os.name == "nt" else sys.executable


@dataclass(frozen=True)
class PickerResult:
    selected: bool = False
    path: str | None = None
    available: bool = True
    message: str = ""


class NativeFolderPicker:
    def __init__(self, runner=None):
        self._runner = runner or subprocess.run
        self._lock = threading.Lock()

    def select(self):
        if not self._lock.acquire(blocking=False):
            return PickerResult(message="Another folder picker is already open.")
        try:
            options = {
                "cwd": str(PROJECT_ROOT),
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "check": False,
            }
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            try:
                completed = self._runner(
                    [PICKER_PYTHON, "-B", "-m", "api.folder_picker_helper"],
                    **options,
                )
                data = json.loads(completed.stdout.strip())
                if data.get("unavailable"):
                    return self._unavailable()
                if data.get("selected") is False and data.get("path") is None:
                    return PickerResult()
                path = data.get("path")
                if data.get("selected") is not True or not isinstance(path, str) or not path:
                    return self._unavailable()
                return PickerResult(selected=True, path=path)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return self._unavailable()
        finally:
            self._lock.release()

    @staticmethod
    def _unavailable():
        return PickerResult(
            available=False,
            message="Folder picker is unavailable. Enter the folder path manually.",
        )
