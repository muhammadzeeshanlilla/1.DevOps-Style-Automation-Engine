import threading
from datetime import datetime, timedelta
import time

from tasks.models import DailyTrigger, IntervalTrigger, FileEventTrigger
from utils.logger import get_logger

logger = get_logger()


class JobScheduler:
    """Sequential time-based dispatch; handlers own all task-specific work."""

    def __init__(self, tasks, runner, wall_clock=None, monotonic_clock=None, event_dispatcher=None):
        self.tasks = tuple(tasks)
        if event_dispatcher is None and any(task.enabled and isinstance(task.trigger, FileEventTrigger) for task in self.tasks):
            raise ValueError("Enabled file-event tasks require an event dispatcher")
        self.event_dispatcher = event_dispatcher
        self.runner = runner
        self._wall_clock = wall_clock or (lambda: datetime.now())
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._daily_claims = {}  # Retained across this component's restarts.
        self._daily_due = {}
        self._interval_due = {}
        self._stop_event = threading.Event()
        self._thread = None
        self._lifecycle_lock = threading.Lock()

    def _initialize_schedule(self):
        now = self._wall_clock()
        tick = self._monotonic_clock()
        self._daily_due = {}
        self._interval_due = {}
        for task in self.tasks:
            if not task.enabled:
                continue
            trigger = task.trigger
            if isinstance(trigger, DailyTrigger):
                due = now.replace(hour=trigger.hour, minute=trigger.minute, second=0, microsecond=0)
                claimed = self._daily_claims.get(task.id)
                if now >= due + timedelta(minutes=1) or (claimed is not None and claimed >= now.date()):
                    due += timedelta(days=1)
                self._daily_due[task.id] = due
            elif isinstance(trigger, IntervalTrigger):
                self._interval_due[task.id] = tick + trigger.every_minutes * 60

    def start(self, reopen_dispatch=True):
        """
        Starts the scheduler in a background thread.
        """
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._initialize_schedule()
            if self.event_dispatcher is not None and reopen_dispatch:
                self.event_dispatcher.reopen()
            self._stop_event.clear()
            try:
                self._thread = threading.Thread(
                    target=self._run_worker, name="JobScheduler", daemon=False
                )
                self._thread.start()
            except BaseException:
                self._stop_event.set()
                if self.event_dispatcher is not None:
                    self.event_dispatcher.close()
                raise
        logger.info("Scheduler started with configured daily/interval tasks.")
        return True

    def is_running(self):
        with self._lifecycle_lock:
            return self._thread is not None and self._thread.is_alive()

    def _run_worker(self):
        try:
            self._scheduler_loop()
        except Exception:
            logger.exception("Scheduler worker failed.")
        finally:
            if self.event_dispatcher is not None:
                self.event_dispatcher.close()
            logger.info("Scheduler worker exiting.")

    def _run_due_tasks(self):
        for task in self.tasks:
            if self._stop_event.is_set():
                break
            if not task.enabled:
                continue
            now = self._wall_clock()
            trigger = task.trigger
            if isinstance(trigger, DailyTrigger):
                due = self._daily_due[task.id]
                # Skip historical days, but preserve today's delayed occurrence.
                if due.date() < now.date():
                    due = now.replace(hour=trigger.hour, minute=trigger.minute, second=0, microsecond=0)
                    self._daily_due[task.id] = due
                if now < due:
                    continue
                claimed = self._daily_claims.get(task.id)
                if claimed is not None and claimed >= due.date():
                    self._daily_due[task.id] = due + timedelta(days=1)
                    continue
                self._daily_claims[task.id] = due.date()
                self._daily_due[task.id] = due + timedelta(days=1)
                self._execute(task)
            elif isinstance(trigger, IntervalTrigger):
                due = self._interval_due[task.id]
                if self._monotonic_clock() < due:
                    continue
                try:
                    self._execute(task)
                finally:
                    period = trigger.every_minutes * 60
                    finished = self._monotonic_clock()
                    steps = max(1, int((finished - due) // period) + 1)
                    self._interval_due[task.id] = due + steps * period

    def _execute(self, task, context=None):
        if self.event_dispatcher is None:
            if not self._stop_event.is_set():
                return self.runner.run(task)
            return None
        if not self.event_dispatcher.claim():
            return None
        try:
            return self.runner.run(task) if context is None else self.runner.run(task, context=context)
        finally:
            self.event_dispatcher.finish()

    def _dispatch_event(self, event):
        for task in self.event_dispatcher.matching_tasks(event):
            if self._stop_event.is_set():
                break
            self._execute(task, context=event)

    def _next_wait(self):
        now = self._wall_clock()
        tick = self._monotonic_clock()
        delays = [60.0]
        delays.extend((due - now).total_seconds() for due in self._daily_due.values())
        delays.extend(due - tick for due in self._interval_due.values())
        return max(0.0, min(delays))

    def _scheduler_loop(self):
        # Disabled definitions pass through the result contract once per start.
        for task in self.tasks:
            if self._stop_event.is_set():
                return
            if not task.enabled:
                self._execute(task)
        while not self._stop_event.is_set():
            self._run_due_tasks()
            if self.event_dispatcher is None:
                self._stop_event.wait(timeout=self._next_wait())
            else:
                event = self.event_dispatcher.next_event(timeout=min(0.1, self._next_wait()))
                if event is not None and not self._stop_event.is_set():
                    self._dispatch_event(event)
                # One observed event per turn keeps scheduled checks responsive.

    def request_stop(self):
        """Signal shutdown without waiting for a current email operation."""
        with self._lifecycle_lock:
            if self.event_dispatcher is not None:
                self.event_dispatcher.close()
            self._stop_event.set()

    def join(self, timeout=1.0):
        """Wait at most timeout seconds; a timeout never terminates the worker."""
        with self._lifecycle_lock:
            thread = self._thread
        if thread is not None and thread.ident is not None:
            if thread is threading.current_thread():
                return False
            thread.join(timeout=timeout)
        stopped = not self.is_running()
        if stopped:
            logger.info("Scheduler stopped.")
        return stopped

    def stop(self, timeout=1.0):
        self.request_stop()
        return self.join(timeout)
