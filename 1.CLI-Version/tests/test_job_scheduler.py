import threading
import unittest
import tempfile
from datetime import datetime
from unittest.mock import Mock, patch

from scheduler.job_scheduler import JobScheduler
from tasks.file_monitor import FileMonitor
from tasks.models import TaskDefinition, DailyTrigger
from tasks.registry import TaskRegistry
from tasks.runner import TaskRunner
from pathlib import Path


class JobSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.config = {"schedule": {"hour": 12, "minute": 35}, "email": {
            "sender": "sender@example.com", "receiver": "receiver@example.com",
            "smtp_server": "smtp.example.com", "smtp_port": 587}}
        self.monitor = Mock()
        self.monitor.get_and_clear_changes.return_value = ["NEW file detected: test.txt"]
        self.task = TaskDefinition("report", "Daily report", "folder_report", True, DailyTrigger(12, 35), {"path": str(Path.cwd())})
        self.scheduler = JobScheduler((self.task,), TaskRunner(TaskRegistry(self.config["email"], {Path.cwd(): self.monitor})))
        self.clock = Mock()
        self.clock.now.return_value = datetime(2026, 1, 1, 0, 0)
        for replacement in (
            patch("scheduler.job_scheduler.datetime", self.clock),
            patch("scheduler.job_scheduler.logger"),
            patch("tasks.report_task.send_report_email", return_value=True),
            patch("smtplib.SMTP", side_effect=AssertionError("Real email is forbidden")),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.addCleanup(self.scheduler.stop, 3)

    def test_normal_start_stop_and_repeated_stop(self):
        self.assertTrue(self.scheduler.start())
        self.assertTrue(self.scheduler.is_running())
        self.assertFalse(self.scheduler._thread.daemon)
        self.assertTrue(self.scheduler.stop())
        self.assertFalse(self.scheduler.is_running())
        self.assertTrue(self.scheduler.stop())

    def test_stop_before_start(self):
        self.assertTrue(self.scheduler.stop())
        self.assertTrue(self.scheduler.start())
        self.assertFalse(self.scheduler._stop_event.is_set())

    def test_restart_clears_stop_event_and_creates_new_thread(self):
        self.scheduler.start()
        first = self.scheduler._thread
        self.scheduler.stop()
        self.assertTrue(self.scheduler._stop_event.is_set())
        self.assertTrue(self.scheduler.start())
        self.assertIsNot(self.scheduler._thread, first)
        self.assertFalse(self.scheduler._stop_event.is_set())
        self.assertTrue(self.scheduler.is_running())

    def test_sequential_duplicate_start_keeps_same_worker(self):
        self.scheduler.start()
        first = self.scheduler._thread
        with patch.object(self.scheduler._stop_event, "clear", wraps=self.scheduler._stop_event.clear) as clear:
            self.assertFalse(self.scheduler.start())
            clear.assert_not_called()
        self.assertIs(self.scheduler._thread, first)

    def test_simultaneous_duplicate_starts_create_only_one_worker(self):
        barrier = threading.Barrier(3)
        results = []
        def start():
            barrier.wait(timeout=3)
            results.append(self.scheduler.start())
        callers = [threading.Thread(target=start) for _ in range(2)]
        for caller in callers:
            caller.start()
        barrier.wait(timeout=3)
        for caller in callers:
            caller.join(timeout=3)
            self.assertFalse(caller.is_alive())
        self.assertEqual(sorted(results), [False, True])

    def test_matching_daily_time_sends_same_batch_and_waits_for_next_check(self):
        self.clock.now.return_value = datetime(2026, 1, 1, 12, 35)
        waited = threading.Event()
        intervals = []
        real_wait = self.scheduler._stop_event.wait
        def wait(timeout):
            intervals.append(timeout)
            waited.set()
            return real_wait(timeout)
        with patch("tasks.report_task.send_report_email", return_value=True) as sender, patch.object(self.scheduler._stop_event, "wait", side_effect=wait):
            self.scheduler.start()
            self.assertTrue(waited.wait(timeout=2))
            self.scheduler.stop()
            sender.assert_called_once_with({"email": self.config["email"]}, ["NEW file detected: test.txt"])
        self.monitor.get_and_clear_changes.assert_called_once_with()
        self.monitor.restore_changes.assert_not_called()
        self.assertEqual(intervals, [60.0])

    def test_other_time_does_not_send_and_waits_60_seconds(self):
        waited = threading.Event()
        intervals = []
        real_wait = self.scheduler._stop_event.wait
        def wait(timeout):
            intervals.append(timeout)
            waited.set()
            return real_wait(timeout)
        with patch("tasks.report_task.send_report_email") as sender, patch.object(self.scheduler._stop_event, "wait", side_effect=wait):
            self.scheduler.start()
            self.assertTrue(waited.wait(timeout=2))
            self.scheduler.stop()
            sender.assert_not_called()
        self.monitor.get_and_clear_changes.assert_not_called()
        self.assertEqual(intervals, [60])

    def test_blocked_fake_email_delays_shutdown_and_prevents_restart(self):
        self.clock.now.return_value = datetime(2026, 1, 1, 12, 35)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocked_email(config, changes):
            entered.set()
            release.wait(timeout=3)
            return True
        with patch("tasks.report_task.send_report_email", side_effect=blocked_email), patch("scheduler.job_scheduler.logger") as logger:
            self.scheduler.start()
            self.assertTrue(entered.wait(timeout=2))
            first = self.scheduler._thread
            self.assertFalse(self.scheduler.stop(timeout=0.01))
            self.assertTrue(self.scheduler.is_running())
            self.assertNotIn("Scheduler stopped.", [call.args[0] for call in logger.info.call_args_list])
            self.assertFalse(self.scheduler.start())
            self.assertIs(self.scheduler._thread, first)
            self.assertTrue(self.scheduler._stop_event.is_set())
            release.set()
            self.assertTrue(self.scheduler.join(timeout=2))

    def test_failed_thread_start_can_be_cleaned_up_and_restarted(self):
        with patch("threading.Thread.start", side_effect=RuntimeError("start failed")):
            with self.assertRaises(RuntimeError):
                self.scheduler.start()
        self.assertTrue(self.scheduler.stop())
        self.assertTrue(self.scheduler.start())

    def test_unexpected_worker_failure_is_logged(self):
        with patch.object(self.scheduler, "_scheduler_loop", side_effect=RuntimeError("worker failed")), patch("scheduler.job_scheduler.logger") as logger:
            self.scheduler.start()
            self.assertTrue(self.scheduler.join(timeout=2))
            logger.exception.assert_called_once_with("Scheduler worker failed.")

    def test_failed_report_restores_exact_batch_once_and_waits_without_retry(self):
        self.clock.now.return_value = datetime(2026, 1, 1, 12, 35)
        batch = self.monitor.get_and_clear_changes.return_value
        def stop_wait(timeout):
            self.assertEqual(timeout, 60.0)
            self.scheduler.request_stop()
        with patch("tasks.report_task.send_report_email", return_value=False) as sender, patch.object(self.scheduler._stop_event, "wait", side_effect=stop_wait) as wait:
            self.scheduler._initialize_schedule()
            self.scheduler._scheduler_loop()
        sender.assert_called_once_with({"email": self.config["email"]}, batch)
        wait.assert_called_once_with(timeout=60.0)
        self.monitor.restore_changes.assert_called_once_with(batch)
        self.assertIs(self.monitor.restore_changes.call_args.args[0], batch)

    def test_unexpected_report_exception_restores_and_scheduler_continues(self):
        self.clock.now.return_value = datetime(2026, 1, 1, 12, 35)
        with patch("tasks.report_task.send_report_email", side_effect=RuntimeError("simulated error")) as sender, patch.object(self.scheduler._stop_event, "wait", side_effect=lambda timeout: self.scheduler.request_stop()):
            self.scheduler._initialize_schedule()
            self.scheduler._run_worker()
        sender.assert_called_once()
        self.monitor.restore_changes.assert_called_once()

    def test_restored_events_precede_events_recorded_during_failed_send(self):
        with tempfile.TemporaryDirectory(prefix="report-order-test-") as folder:
            monitor = FileMonitor(folder)
            monitor._record_change("A")
            monitor._record_change("B")
            self.scheduler.runner = TaskRunner(TaskRegistry(self.config["email"], {Path.cwd(): monitor}))
            self.clock.now.return_value = datetime(2026, 1, 1, 12, 35)
            def fail(config, batch):
                self.assertEqual(batch, ["A", "B"])
                monitor._record_change("C")
                return False
            with patch("tasks.report_task.send_report_email", side_effect=fail) as sender, patch.object(self.scheduler._stop_event, "wait", side_effect=lambda timeout: self.scheduler.request_stop()):
                self.scheduler._initialize_schedule()
                self.scheduler._scheduler_loop()
            sender.assert_called_once()
            self.assertEqual(monitor.get_and_clear_changes(), ["A", "B", "C"])

    def test_non_boolean_result_is_not_treated_as_confirmed_acceptance(self):
        self.clock.now.return_value = datetime(2026, 1, 1, 12, 35)
        with patch("tasks.report_task.send_report_email", return_value=None), patch.object(self.scheduler._stop_event, "wait", side_effect=lambda timeout: self.scheduler.request_stop()):
            self.scheduler._initialize_schedule()
            self.scheduler._scheduler_loop()
        self.monitor.restore_changes.assert_called_once()

    def test_successful_acceptance_with_cleanup_failure_does_not_restore(self):
        from tasks.email_task import send_report_email
        import os
        self.config["email"] = {"sender": "sender@example.com", "receiver": "receiver@example.com",
                                "smtp_server": "smtp.example.com", "smtp_port": 587}
        self.clock.now.return_value = datetime(2026, 1, 1, 12, 35)
        with patch.dict(os.environ, {"SMTP_PASSWORD": "dummy-only"}), patch("tasks.email_task.smtplib.SMTP") as smtp, patch("tasks.email_task.logger"), patch("tasks.report_task.send_report_email", side_effect=send_report_email), patch.object(self.scheduler._stop_event, "wait", side_effect=lambda timeout: self.scheduler.request_stop()):
            smtp.return_value.__enter__.return_value.sendmail.return_value = {}
            smtp.return_value.__exit__.side_effect = TimeoutError("simulated cleanup timeout")
            self.scheduler._initialize_schedule()
            self.scheduler._scheduler_loop()
        self.monitor.restore_changes.assert_not_called()


if __name__ == "__main__":
    unittest.main()
