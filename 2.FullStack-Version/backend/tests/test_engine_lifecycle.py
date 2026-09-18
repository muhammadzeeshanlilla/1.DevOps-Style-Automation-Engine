import contextlib
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main
from datetime import datetime
from config.loader import validate_config
from tasks.registry import TaskRegistry
from tasks.runner import TaskRunner
from tasks.file_monitor import FileMonitor
from scheduler.job_scheduler import JobScheduler
from utils.process_manager import AlreadyRunning, ProcessManagementError, ProcessManager


class EngineLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="engine-lifecycle-test-")
        self.root = Path(temp.name).resolve()
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temp.cleanup)
        self.manager = ProcessManager(self.root)
        self.addCleanup(self.manager.release)
        self.monitor = Mock()
        self.scheduler = Mock()
        for component in (self.monitor, self.scheduler):
            component.is_running.return_value = False
            component.stop.return_value = True
            component.join.return_value = True
        for replacement in (
            patch.object(main, "FileMonitor", return_value=self.monitor),
            patch.object(main, "JobScheduler", return_value=self.scheduler),
            patch.object(main, "logger"),
            patch("os.kill", side_effect=AssertionError("Process signals are forbidden")),
            patch("smtplib.SMTP", side_effect=AssertionError("Real email is forbidden")),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.config = validate_config({"watch_folder": str(self.root), "schedule": {"hour": 12, "minute": 35},
            "email": {"sender": "sender@example.com", "receiver": "receiver@example.com",
                      "smtp_server": "smtp.example.com", "smtp_port": 587}})
        self.engine = main.Engine(self.config, self.manager)

    def test_cli_request_reaches_cleanup_and_stop_is_idempotent(self):
        original_start = self.scheduler.start
        def start_and_request():
            original_start()
            ProcessManager(self.root).request_stop(timeout=0)
        self.scheduler.start = Mock(side_effect=start_and_request)
        with patch.object(self.manager, "publish_state", wraps=self.manager.publish_state) as states:
            self.assertEqual(self.engine.start(), 0)
        self.assertEqual([call.args[0] for call in states.call_args_list], ["RUNNING", "STOPPING"])
        self.monitor.stop.assert_called_once()
        self.scheduler.stop.assert_called_once()
        self.assertFalse(self.manager.pid_path.exists())
        self.assertFalse(self.engine.runtime_status.path.exists())
        self.assertFalse(list(self.root.glob("engine.stop.*")))
        self.assertFalse(self.engine.is_running())
        self.assertTrue(self.engine.stop())
        self.monitor.stop.assert_called_once()

    def test_halfway_startup_failure_cleans_both_attempted_components(self):
        self.scheduler.start.side_effect = RuntimeError("scheduler start failed")
        self.assertEqual(self.engine.start(), 1)
        self.monitor.stop.assert_called_once()
        self.scheduler.stop.assert_called_once()
        self.assertFalse(self.manager.owns_instance)
        self.assertFalse(self.manager.pid_path.exists())
        self.assertFalse(self.engine.runtime_status.path.exists())
        self.assertFalse(self.engine.is_running())
        self.manager.acquire()

    def test_first_component_failure_does_not_start_second(self):
        self.monitor.start.side_effect = RuntimeError("monitor start failed")
        self.assertEqual(self.engine.start(), 1)
        self.monitor.stop.assert_called_once()
        self.scheduler.start.assert_not_called()
        self.scheduler.stop.assert_not_called()
        self.assertFalse(self.manager.owns_instance)

    def test_one_stop_error_does_not_skip_other_stop_or_ownership_cleanup(self):
        self.monitor.stop.side_effect = RuntimeError("monitor stop failed")
        with patch.object(self.manager, "stop_requested", return_value=True):
            self.assertEqual(self.engine.start(), 1)
        self.scheduler.stop.assert_called_once()
        self.assertFalse(self.manager.owns_instance)
        self.assertFalse(self.manager.pid_path.exists())

    def test_ctrl_c_during_startup_uses_same_cleanup(self):
        self.scheduler.start.side_effect = KeyboardInterrupt
        self.assertEqual(self.engine.start(), 0)
        self.monitor.stop.assert_called_once()
        self.scheduler.stop.assert_called_once()
        self.assertFalse(self.manager.owns_instance)

    def test_ctrl_c_in_main_loop_uses_same_cleanup(self):
        with patch.object(self.manager, "stop_requested", side_effect=KeyboardInterrupt):
            self.assertEqual(self.engine.start(), 0)
        self.monitor.stop.assert_called_once()
        self.scheduler.stop.assert_called_once()
        self.assertFalse(self.manager.owns_instance)

    def test_interruption_immediately_after_acquiring_ownership_is_cleaned_up(self):
        acquire = self.manager.acquire
        def interrupted_acquire():
            acquire()
            raise KeyboardInterrupt
        with patch.object(self.manager, "acquire", side_effect=interrupted_acquire):
            self.assertEqual(self.engine.start(), 0)
        self.monitor.start.assert_not_called()
        self.assertFalse(self.manager.owns_instance)
        self.assertFalse(self.manager.pid_path.exists())

    def test_state_publication_failure_does_not_skip_component_cleanup(self):
        publish = self.manager.publish_state
        def fail_stopping(state):
            if state == "STOPPING":
                raise ProcessManagementError("simulated state failure")
            return publish(state)
        with patch.object(self.manager, "publish_state", side_effect=fail_stopping), patch.object(self.manager, "stop_requested", return_value=True):
            self.assertEqual(self.engine.start(), 1)
        self.monitor.stop.assert_called_once()
        self.scheduler.stop.assert_called_once()
        self.assertFalse(self.manager.owns_instance)

    def test_duplicate_start_does_not_clean_up_existing_owner(self):
        owner = ProcessManager(self.root)
        owner.acquire()
        try:
            metadata = owner.pid_path.read_bytes()
            self.assertEqual(self.engine.start(), 1)
            self.monitor.start.assert_not_called()
            self.monitor.stop.assert_not_called()
            self.assertEqual(owner.pid_path.read_bytes(), metadata)
            self.assertTrue(owner.owns_instance)
        finally:
            owner.release()

    def test_ctrl_c_during_stopping_state_publication_does_not_skip_workers(self):
        publish = self.manager.publish_state
        def interrupted_publish(state):
            if state == "STOPPING":
                raise KeyboardInterrupt
            publish(state)
        with patch.object(self.manager, "publish_state", side_effect=interrupted_publish), patch.object(self.manager, "stop_requested", return_value=True):
            self.assertEqual(self.engine.start(), 1)
        self.monitor.request_stop.assert_called_once()
        self.scheduler.request_stop.assert_called_once()
        self.monitor.stop.assert_called_once()
        self.scheduler.stop.assert_called_once()
        self.assertFalse(self.manager.owns_instance)

    def test_all_components_are_signalled_before_any_stop_wait(self):
        order = []
        for name, component in (("monitor", self.monitor), ("scheduler", self.scheduler)):
            component.request_stop.side_effect = lambda name=name: order.append(name + " signal")
            component.stop.side_effect = lambda timeout, name=name: order.append(name + " stop")
        with patch.object(self.manager, "stop_requested", return_value=True):
            self.assertEqual(self.engine.start(), 0)
        self.assertEqual(order, ["monitor signal", "scheduler signal", "monitor stop", "scheduler stop"])

    def test_one_signal_error_does_not_skip_other_component(self):
        self.monitor.request_stop.side_effect = RuntimeError("signal failed")
        with patch.object(self.manager, "stop_requested", return_value=True):
            self.assertEqual(self.engine.start(), 1)
        self.scheduler.request_stop.assert_called_once()
        self.monitor.stop.assert_called_once()
        self.scheduler.stop.assert_called_once()
        self.assertFalse(self.manager.owns_instance)

    def test_interrupted_component_stop_does_not_skip_other_component(self):
        self.monitor.stop.side_effect = KeyboardInterrupt
        with patch.object(self.manager, "stop_requested", return_value=True):
            self.assertEqual(self.engine.start(), 1)
        self.scheduler.stop.assert_called_once()
        self.assertFalse(self.manager.owns_instance)

    def _start_controlled_engine(self, release):
        results = []
        thread = threading.Thread(target=lambda: results.append(self.engine.start()))
        def finish():
            release.set()
            self.engine._running = False
            self.engine._shutdown_event.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "Controlled engine failed to finish")
        self.addCleanup(finish)
        thread.start()
        return thread, results

    def test_blocked_email_retains_ownership_until_actual_worker_exit(self):
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
        config = {"watch_folder": str(self.root), "schedule": {"hour": 12, "minute": 35}}
        monitor = FileMonitor(str(self.root))
        scheduler = JobScheduler(self.config.tasks, TaskRunner(TaskRegistry(self.config.email, {self.root: monitor})), wall_clock=lambda: datetime(2026, 1, 1, 12, 35))
        self.engine.file_monitors = {self.root: monitor}
        self.engine.job_scheduler = scheduler
        self.engine.WORKER_JOIN_TIMEOUT = 0.02
        main.logger.warning.side_effect = lambda *args: waiting.set()
        def send(config, changes):
            entered.set()
            release.wait(timeout=5)
            return True
        with patch("scheduler.job_scheduler.datetime") as clock, patch("tasks.report_task.send_report_email", side_effect=send):
            clock.now.return_value.hour = 12
            clock.now.return_value.minute = 35
            thread, results = self._start_controlled_engine(release)
            try:
                self.assertTrue(entered.wait(timeout=2))
                controller = ProcessManager(self.root)
                self.assertEqual(controller.request_stop(timeout=0.05).state, "REQUESTED")
                self.assertTrue(waiting.wait(timeout=2))
                self.assertEqual(controller.inspect_status().state, "STOPPING", str(main.logger.mock_calls))
                self.assertTrue(self.manager.owns_instance)
                self.assertFalse(self.engine._cleanup_done)
                self.assertTrue(scheduler.is_running())
                self.assertFalse(monitor.is_running())
                with self.assertRaises(AlreadyRunning):
                    controller.acquire()
                self.assertEqual(controller.request_stop(timeout=0.02).state, "REQUESTED")
                release.set()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results, [0], str(main.logger.mock_calls))
                self.assertFalse(scheduler.is_running())
                self.assertFalse(self.manager.owns_instance)
                self.assertFalse(self.manager.pid_path.exists())
                self.assertEqual(controller.inspect_status().state, "STOPPED")
            finally:
                release.set()
                thread.join(timeout=5)

    def test_simulated_smtp_timeout_restores_events_before_worker_exit_and_ownership_release(self):
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
        config = {"watch_folder": str(self.root), "schedule": {"hour": 12, "minute": 35},
                  "email": {"sender": "sender@example.com", "receiver": "receiver@example.com",
                            "smtp_server": "smtp.example.com", "smtp_port": 587}}
        monitor = FileMonitor(str(self.root))
        monitor._record_change("A")
        scheduler = JobScheduler(self.config.tasks, TaskRunner(TaskRegistry(self.config.email, {self.root: monitor})), wall_clock=lambda: datetime(2026, 1, 1, 12, 35))
        self.engine.file_monitors = {self.root: monitor}
        self.engine.job_scheduler = scheduler
        self.engine.WORKER_JOIN_TIMEOUT = 0.02
        main.logger.warning.side_effect = lambda *args: waiting.set()
        def timeout_login(*args):
            entered.set()
            release.wait(timeout=5)
            raise TimeoutError("simulated SMTP timeout")
        original_release = self.manager.release
        def release_after_workers():
            self.assertFalse(scheduler.is_running())
            self.assertFalse(monitor.is_running())
            self.assertTrue(self.manager.owns_instance)
            original_release()
        with patch.dict(os.environ, {"SMTP_PASSWORD": "dummy-test-only"}), patch("tasks.email_task.smtplib.SMTP") as smtp, patch("tasks.email_task.logger"), patch("scheduler.job_scheduler.logger"), patch("scheduler.job_scheduler.datetime") as clock, patch.object(monitor, "restore_changes", wraps=monitor.restore_changes) as restore, patch.object(self.manager, "release", side_effect=release_after_workers) as ownership_release:
            clock.now.return_value.hour = 12
            clock.now.return_value.minute = 35
            smtp.return_value.__enter__.return_value.login.side_effect = timeout_login
            thread, results = self._start_controlled_engine(release)
            try:
                self.assertTrue(entered.wait(timeout=2))
                controller = ProcessManager(self.root)
                self.assertEqual(controller.request_stop(timeout=0.05).state, "REQUESTED")
                self.assertTrue(waiting.wait(timeout=2))
                self.assertEqual(controller.inspect_status().state, "STOPPING")
                self.assertTrue(self.manager.owns_instance)
                self.assertTrue(scheduler.is_running())
                ownership_release.assert_not_called()
                monitor._record_change("C")
                release.set()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results, [0])
                restore.assert_called_once()
                smtp.return_value.__enter__.return_value.sendmail.assert_not_called()
                events = monitor.get_and_clear_changes()
                self.assertEqual(events.count("A"), 1)
                self.assertEqual(events.count("C"), 1)
                self.assertLess(events.index("A"), events.index("C"))
                ownership_release.assert_called_once()
                self.assertFalse(self.manager.owns_instance)
                self.assertEqual(controller.inspect_status().state, "STOPPED")
            finally:
                release.set()
                thread.join(timeout=5)

    def test_partial_start_failure_waits_for_blocked_monitor(self):
        entered, release, waiting = threading.Event(), threading.Event(), threading.Event()
        monitor = FileMonitor(str(self.root))
        self.engine.file_monitors = {self.root: monitor}
        self.engine.WORKER_JOIN_TIMEOUT = 0.02
        main.logger.warning.side_effect = lambda *args: waiting.set()
        def scan():
            entered.set()
            release.wait(timeout=5)
        def fail_start():
            if not entered.wait(timeout=2):
                raise RuntimeError("Monitor failed to enter controlled scan")
            raise RuntimeError("scheduler startup failed")
        self.scheduler.start.side_effect = fail_start
        with patch.object(monitor, "_check_changes", side_effect=scan):
            thread, results = self._start_controlled_engine(release)
            try:
                self.assertTrue(waiting.wait(timeout=2))
                self.assertEqual(ProcessManager(self.root).inspect_status().state, "STOPPING")
                self.assertTrue(self.manager.owns_instance)
                self.scheduler.request_stop.assert_called_once()
                self.scheduler.stop.assert_called_once()
                release.set()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results, [1])
                self.assertFalse(monitor.is_running())
                self.assertFalse(self.manager.owns_instance)
            finally:
                release.set()
                thread.join(timeout=5)

    def test_stop_error_with_live_worker_is_retried_without_releasing_ownership(self):
        alive = [True]
        self.monitor.is_running.side_effect = lambda: alive[0]
        self.monitor.stop.side_effect = RuntimeError("stop failed")
        def join(timeout):
            self.assertTrue(self.manager.owns_instance)
            self.assertEqual(ProcessManager(self.root).inspect_status().state, "STOPPING")
            self.scheduler.request_stop.assert_called()
            alive[0] = False
            return True
        self.monitor.join.side_effect = join
        with patch.object(self.manager, "stop_requested", return_value=True):
            self.assertEqual(self.engine.start(), 1)
        self.monitor.join.assert_called_once()
        self.assertFalse(self.manager.owns_instance)


if __name__ == "__main__":
    unittest.main()
