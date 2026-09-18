"""Thread-safe handoff and shutdown claims; no execution worker or report access."""
import os
import queue
import threading
from pathlib import Path
from tasks.events import FileEvent
from tasks.models import FileEventTrigger
from utils.logger import get_logger

logger = get_logger()


def folder_key(path):
    return os.path.normcase(str(Path(path).resolve()))


class EventDispatcher:
    def __init__(self, tasks):
        self._tasks = tuple((task, folder_key(task.trigger.path)) for task in tasks
                            if task.enabled and isinstance(task.trigger, FileEventTrigger))
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._accepting = False
        self._active = False

    def reopen(self):
        with self._lock:
            if self._active:
                raise RuntimeError("Cannot reopen dispatch while execution is active")
            if not self._accepting:
                self._discard_locked()
            self._accepting = True

    def submit(self, event):
        if not isinstance(event, FileEvent):
            raise TypeError("Event dispatch requires a FileEvent")
        with self._lock:
            if not self._accepting:
                return False
            self._queue.put_nowait(event)
            return True

    def next_event(self, timeout=0):
        try:
            event = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        self._queue.task_done()
        return event

    def matching_tasks(self, event):
        if not isinstance(event, FileEvent):
            raise TypeError("Event dispatch requires a FileEvent")
        key = folder_key(event.watched_folder)
        return tuple(task for task, watched in self._tasks
                     if watched == key and event.event_type in task.trigger.events)

    def claim(self):
        # A claim preceding close is active work; close cannot cancel it.
        with self._lock:
            if not self._accepting or self._active:
                return False
            self._active = True
            return True

    def finish(self):
        with self._lock:
            self._active = False

    def _discard_locked(self):
        count = 0
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            self._queue.task_done()
            count += 1
        return count

    def close(self):
        with self._lock:
            self._accepting = False
            count = self._discard_locked()
        if count:
            logger.info("Discarded queued file events during shutdown: %d", count)
        return count

    def queued_count(self):
        """Approximate observations waiting; excludes the active event."""
        return self._queue.qsize()
