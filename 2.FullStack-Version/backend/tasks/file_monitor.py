# This file watches a folder continuously.
# Every 5 seconds it checks if any file was added, deleted, or modified.
# It stores the changes in a list so the scheduler can send them in the email.

import os       # To read folder contents
import threading  # To run the monitor in the background without freezing the engine

from datetime import datetime
from pathlib import Path
from tasks.events import FileEvent
from utils.logger import get_logger

logger = get_logger()

class FileMonitor:
    """
    FileMonitor watches one folder.
    It runs in a background thread so it does not block anything else.
    """

    def __init__(self, folder_path, event_submit=None, report_job_id=None):
        # The path of the folder to watch (comes from config)
        self.folder_path = folder_path
        self._event_submit = event_submit
        self._event_folder = Path(folder_path).resolve() if event_submit is not None else None
        self._report_job_id = report_job_id

        # This dictionary stores filename -> last modified time
        # We use it to detect changes
        self.known_files = {}

        # This list collects all change messages during the day
        # The scheduler reads this list when it is time to send the email
        self.changes = []
        self._changes_lock = threading.Lock()

        self._thread = None
        self._lifecycle_lock = threading.Lock()

        # A threading Event that lets us stop the monitor cleanly
        self._stop_event = threading.Event()

    def _take_snapshot(self):
        """
        Reads the folder and returns a dictionary of
        filename -> last modified time for all files currently in the folder.
        """
        snapshot = {}
        try:
            for filename in os.listdir(self.folder_path):
                filepath = os.path.join(self.folder_path, filename)
                # os.path.getmtime gives the last modified time as a number
                snapshot[filename] = os.path.getmtime(filepath)
        except Exception as e:
            logger.error(f"Error reading folder: {e}")
        return snapshot

    def _check_changes(self):
        """
        Compares the current folder state with what we saw last time.
        Logs and records any new, deleted, or modified files.
        """
        current = self._take_snapshot()

        # Check for NEW files (in current but not in known_files)
        for filename in current:
            if filename not in self.known_files:
                msg = f"NEW file detected: {filename}"
                self._log_change("NEW")
                self._record_change(msg)
                self._submit_event("new", filename)

        # Check for DELETED files (in known_files but not in current)
        for filename in self.known_files:
            if filename not in current:
                msg = f"DELETED file: {filename}"
                self._log_change("DELETED")
                self._record_change(msg)
                self._submit_event("deleted", filename)

        # Check for MODIFIED files (same name but different modified time)
        for filename in current:
            if filename in self.known_files:
                if current[filename] != self.known_files[filename]:
                    msg = f"MODIFIED file: {filename}"
                    self._log_change("MODIFIED")
                    self._record_change(msg)
                    self._submit_event("modified", filename)

        # Update our known state to the current state
        self.known_files = current

    def start(self):
        """
        Starts the file monitor in a background thread.
        The thread runs _monitor_loop() continuously.
        """
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            # Establish a fresh baseline only for a genuinely new worker.
            try:
                self.known_files = self._take_snapshot()
                self._thread = threading.Thread(
                    target=self._run_worker, name="FileMonitor", daemon=False
                )
                self._thread.start()
            except BaseException:
                self._stop_event.set()
                raise
        logger.info(f"File monitor started. Watching: {self.folder_path}")
        return True

    @property
    def running(self):
        """Compatibility flag backed by actual worker liveness."""
        return self.is_running()

    def is_running(self):
        with self._lifecycle_lock:
            return self._thread is not None and self._thread.is_alive()

    def _run_worker(self):
        try:
            self._monitor_loop()
        except Exception:
            logger.exception("File monitor worker failed.")
        finally:
            logger.info("File monitor worker exiting.")

    def _monitor_loop(self):
        """
        This loop runs forever in the background.
        Every 5 seconds it checks for changes.
        It stops when self._stop_event is set.
        """
        while not self._stop_event.is_set():
            self._check_changes()
            # Wait 5 seconds before checking again
            # We use _stop_event.wait() instead of time.sleep()
            # so we can interrupt the wait immediately when stopping
            self._stop_event.wait(timeout=5)

    def request_stop(self):
        """Signal shutdown without waiting, so all engine workers can be signalled."""
        with self._lifecycle_lock:
            self._stop_event.set()

    def join(self, timeout=1.0):
        """Wait at most timeout seconds; return True only if no worker is alive."""
        with self._lifecycle_lock:
            thread = self._thread
        # A failed Thread.start() can leave an unstarted thread reference.
        if thread is not None and thread.ident is not None:
            if thread is threading.current_thread():
                return False
            # Never hold the lifecycle lock while waiting for another thread.
            thread.join(timeout=timeout)
        stopped = not self.is_running()
        if stopped:
            logger.info("File monitor stopped.")
        return stopped

    def stop(self, timeout=1.0):
        """Request shutdown and wait for a bounded period; False means still alive."""
        self.request_stop()
        return self.join(timeout)

    def _record_change(self, message):
        with self._changes_lock:
            self.changes.append(message)

    def _log_change(self, event_type):
        if self._report_job_id:
            logger.info("%s file detected | job=%r", event_type, self._report_job_id)
        else:
            logger.info("%s file detected", event_type)

    def get_and_clear_changes(self):
        """
        Returns all changes collected since last call, then clears the list.
        The scheduler calls this when it is time to send the email report.
        """
        with self._changes_lock:
            collected = self.changes.copy()
            self.changes.clear()
        return collected

    def restore_changes(self, batch):
        """Return an unsent older batch before newer events, without deduplication."""
        with self._changes_lock:
            self.changes[:0] = batch

    def _submit_event(self, event_type, filename):
        if self._event_submit is not None:
            self._event_submit(FileEvent(event_type, self._event_folder,
                                         self._event_folder / filename, datetime.now()))
