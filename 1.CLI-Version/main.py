import sys
import threading

from utils.runtime_status import RuntimeStatus, worker_state
from utils.logger import setup_logger, get_logger
from tasks.file_monitor import FileMonitor
from scheduler.job_scheduler import JobScheduler
from cli.handler import handle_command
from utils.paths import CONFIG_PATH
from config.loader import load_config as load_workflow_config
from tasks.models import FileEventTrigger
from tasks.registry import TaskRegistry
from tasks.event_dispatcher import EventDispatcher
from tasks.runner import TaskRunner
from tasks.notifications import LifecycleNotificationService
from pathlib import Path
from utils.process_manager import AlreadyRunning, ProcessManager

logger = get_logger()


def load_config():
    return load_workflow_config(CONFIG_PATH)


class Engine:
    WORKER_JOIN_TIMEOUT = 1.0

    def __init__(self, config, process_manager=None):
        self.config = config
        self._running = False
        self._shutdown_event = threading.Event()
        self._attempted_components = []
        self._cleanup_done = False
        self._cleanup_ok = True
        self._cleanup_lock = threading.Lock()
        self.process_manager = process_manager if process_manager is not None else ProcessManager()
        for warning in config.warnings:
            logger.warning("%s", warning)
        event_tasks = any(task.enabled and isinstance(task.trigger, FileEventTrigger) for task in config.tasks)
        self.event_dispatcher = EventDispatcher(config.tasks) if event_tasks else None
        self.file_monitors = {}
        for task in config.tasks:
            if not task.enabled:
                continue
            folder = None
            if task.type == "folder_report":
                folder = Path(task.parameters["path"]).resolve()
            elif isinstance(task.trigger, FileEventTrigger):
                folder = task.trigger.path.resolve()
            if folder is not None and folder not in self.file_monitors:
                if self.event_dispatcher is None:
                    self.file_monitors[folder] = FileMonitor(str(folder))
                else:
                    self.file_monitors[folder] = FileMonitor(str(folder), event_submit=self.event_dispatcher.submit)
        self.task_registry = TaskRegistry(config.email, self.file_monitors)
        self.notification_service = LifecycleNotificationService(config.email, config.notifications)
        self.runtime_status = RuntimeStatus(config.tasks, self.process_manager.runtime_dir)
        self._status_lifecycle = "STARTING"
        self.task_runner = TaskRunner(self.task_registry, notification_service=self.notification_service,
                                      status_observer=self.runtime_status)
        if self.event_dispatcher is None:
            self.job_scheduler = JobScheduler(config.tasks, self.task_runner)
        else:
            self.job_scheduler = JobScheduler(config.tasks, self.task_runner, event_dispatcher=self.event_dispatcher)

    def start(self):
        acquired = False
        previously_owned = self.process_manager.owns_instance
        result = 0
        try:
            self.process_manager.acquire()
            acquired = True
            try:
                self.runtime_status.bind(self.process_manager)
            except Exception:
                logger.warning("Runtime status initialization unavailable.")
            self._running = True
            self._shutdown_event.clear()
            self._attempted_components = []
            self._cleanup_done = False
            self._cleanup_ok = True
            if self.event_dispatcher is not None:
                self.event_dispatcher.reopen()
            self._status_lifecycle = "STARTING"
            self._publish_runtime_status()
            logger.info("=== Automation Engine STARTING ===")
            for component in (*self.file_monitors.values(), self.job_scheduler):
                # Even a partially successful start needs a stop attempt.
                self._attempted_components.append(component)
                if component is self.job_scheduler and self.event_dispatcher is not None:
                    component.start(reopen_dispatch=False)
                else:
                    component.start()
                self._publish_runtime_status()
            self.process_manager.publish_state("RUNNING")
            self._status_lifecycle = "RUNNING"
            self._publish_runtime_status()
            logger.info("=== Automation Engine STARTED ===")
            print("\nEngine is running. Press Ctrl+C to stop.\n")
            while self._running:
                if self.process_manager.stop_requested():
                    logger.info("CLI stop request received.")
                    break
                self._publish_runtime_status()
                self._shutdown_event.wait(timeout=1)
        except AlreadyRunning as exc:
            print(str(exc))
            result = 1
        except KeyboardInterrupt:
            logger.info("Ctrl+C received; cleaning up engine.")
        except Exception as exc:
            logger.exception("Engine startup or runtime failed: %s", exc)
            print("Engine failed: " + str(exc))
            result = 1
        finally:
            if acquired or (not previously_owned and self.process_manager.owns_instance):
                try:
                    if not self.stop():
                        result = 1
                finally:
                    # A stop request or join timeout is not proof of worker exit.
                    if self._cleanup_done:
                        try:
                            self.runtime_status.cleanup(self.process_manager)
                        except Exception:
                            logger.warning("Runtime status cleanup unavailable; continuing ownership cleanup.")
                        try:
                            self.process_manager.release()
                        except Exception:
                            logger.exception("Engine ownership cleanup failed.")
                            result = 1
        return result

    def stop(self):
        if self.event_dispatcher is not None:
            self.event_dispatcher.close()
        self._running = False
        self._shutdown_event.set()
        # Repeated/concurrent cleanup calls share one completion result.
        with self._cleanup_lock:
            return self._stop_workers()

    def _stop_workers(self):
        if self._cleanup_done:
            return self._cleanup_ok
        self._status_lifecycle = "STOPPING"
        self._publish_runtime_status()
        logger.info("=== Automation Engine STOPPING ===")
        if self.process_manager.owns_instance:
            try:
                self.process_manager.publish_state("STOPPING")
            except (Exception, KeyboardInterrupt):
                logger.exception("Unable to publish STOPPING state; continuing cleanup.")
                self._cleanup_ok = False
        for component in self._attempted_components:
            try:
                component.request_stop()
            except (Exception, KeyboardInterrupt):
                logger.exception("Component shutdown signal failed; continuing cleanup.")
                self._cleanup_ok = False
        # Every attempted component has received a signal before any join.
        pending = list(self._attempted_components)
        first_pass = True
        warned = set()
        while pending:
            unfinished = []
            for component in pending:
                try:
                    if first_pass:
                        component.stop(timeout=self.WORKER_JOIN_TIMEOUT)
                    else:
                        # Retry signalling too, in case an earlier stop operation failed.
                        component.request_stop()
                        component.join(timeout=self.WORKER_JOIN_TIMEOUT)
                except (Exception, KeyboardInterrupt):
                    logger.exception("Component stop/join failed; continuing cleanup.")
                    self._cleanup_ok = False
                try:
                    alive = component.is_running()
                except (Exception, KeyboardInterrupt):
                    logger.exception("Unable to confirm worker exit; retaining ownership.")
                    self._cleanup_ok = False
                    alive = True
                if alive:
                    unfinished.append(component)
                    if id(component) not in warned:
                        logger.warning(
                            "%s worker still alive; engine remains STOPPING and retains ownership.",
                            type(component).__name__,
                        )
                        warned.add(id(component))
                self._publish_runtime_status()
            pending = unfinished
            first_pass = False
            if pending:
                # Prevent a busy retry loop if a component operation keeps raising.
                try:
                    threading.Event().wait(timeout=0.1)
                except KeyboardInterrupt:
                    logger.warning("Shutdown is still waiting for workers; ownership retained.")
        self._cleanup_done = True
        logger.info("Engine cleanup completed. All attempted workers have exited.")
        print("Engine cleanup completed." if self._cleanup_ok else "Engine cleanup completed with errors; check logs.")
        return self._cleanup_ok

    def _publish_runtime_status(self):
        if not self.process_manager.owns_instance:
            return
        try:
            monitors = [{"label": f"monitor-{index}", "state": worker_state(worker)}
                        for index, worker in enumerate(self.file_monitors.values(), 1)]
            queued = self.event_dispatcher.queued_count() if self.event_dispatcher is not None else 0
            self.runtime_status.publish(self.process_manager, self._status_lifecycle,
                                        worker_state(self.job_scheduler), monitors, queued)
        except Exception:
            logger.warning("Runtime status publication unavailable.")

    def is_running(self):
        return self._running


def run_cli(argv=None, process_manager=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("Usage: python main.py start | stop | status")
        return 2
    command = arguments[0].lower()
    manager = process_manager if process_manager is not None else ProcessManager()
    engine = None
    if command == "start":
        try:
            engine = Engine(load_config(), process_manager=manager)
        except SystemExit as exc:
            return exc.code
        except Exception as exc:
            logger.exception("Unable to prepare engine: %s", exc)
            print("Unable to prepare engine: " + str(exc))
            return 1
    return handle_command(command, engine, process_manager=manager)


if __name__ == "__main__":
    setup_logger()
    sys.exit(run_cli())
