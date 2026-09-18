import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tasks.file_monitor import FileMonitor
from tasks.models import DailyTrigger, TaskDefinition
from tasks.registry import TaskRegistry
from tasks.report_task import FolderReportHandler
from tasks.runner import TaskRunner


class ReportTaskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="report-handler-test-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name).resolve()
        self.settings = {"sender": "sender@example.com", "receiver": "receiver@example.com",
                         "smtp_server": "smtp.example.com", "smtp_port": 587}
        self.monitor = FileMonitor(str(self.path))
        self.handler = FolderReportHandler(self.settings, {self.path: self.monitor})
        self.task = TaskDefinition("report", "Folder report", "folder_report", True,
                                   DailyTrigger(12, 35), {"path": self.path})
        for replacement in (patch("smtplib.SMTP", side_effect=AssertionError("Real SMTP forbidden")),
                            patch("threading.Thread.start", side_effect=AssertionError("Workers forbidden")),
                            patch("tasks.runner.logger")):
            replacement.start()
            self.addCleanup(replacement.stop)

    def record(self, *events):
        for event in events:
            self.monitor._record_change(event)

    def test_missing_monitor_fails_without_collecting_or_sending(self):
        handler = FolderReportHandler(self.settings, {})
        with patch("tasks.report_task.send_report_email") as sender:
            result = handler.execute(self.task)
        self.assertEqual(result.status, "FAILED")
        self.assertIsNone(result.notification_accepted)
        sender.assert_not_called()

    def test_success_collects_batch_without_restoring(self):
        self.record("A", "B")
        with patch("tasks.report_task.send_report_email", return_value=True) as sender, patch.object(self.monitor, "restore_changes", wraps=self.monitor.restore_changes) as restore:
            result = self.handler.execute(self.task)
        sender.assert_called_once_with({"email": self.settings}, ["A", "B"])
        restore.assert_not_called()
        self.assertTrue(result.success)
        self.assertTrue(result.notification_accepted)
        self.assertEqual(result.details, {"event_count": 2, "events_restored": False})
        self.assertEqual(self.monitor.get_and_clear_changes(), [])

    def test_failed_report_restores_exact_batch_once_without_retry(self):
        self.record("A", "B")
        with patch("tasks.report_task.send_report_email", return_value=False) as sender, patch.object(self.monitor, "restore_changes", wraps=self.monitor.restore_changes) as restore:
            result = self.handler.execute(self.task)
        sender.assert_called_once()
        restore.assert_called_once_with(["A", "B"])
        self.assertIs(restore.call_args.args[0], sender.call_args.args[1])
        self.assertFalse(result.success)
        self.assertFalse(result.notification_accepted)
        self.assertTrue(result.details["events_restored"])
        self.assertEqual(self.monitor.get_and_clear_changes(), ["A", "B"])

    def test_unexpected_send_exception_restores_once_before_propagating(self):
        self.record("A")
        with patch("tasks.report_task.send_report_email", side_effect=RuntimeError("dummy error")), patch.object(self.monitor, "restore_changes", wraps=self.monitor.restore_changes) as restore:
            with self.assertRaises(RuntimeError):
                self.handler.execute(self.task)
        restore.assert_called_once_with(["A"])
        self.assertEqual(self.monitor.get_and_clear_changes(), ["A"])

    def test_runner_contains_report_exception_after_restoration(self):
        self.record("A")
        runner = TaskRunner(TaskRegistry(self.settings, {self.path: self.monitor}))
        with patch("tasks.report_task.send_report_email", side_effect=RuntimeError("dummy sensitive value")), patch.object(self.monitor, "restore_changes", wraps=self.monitor.restore_changes) as restore:
            result = runner.run(self.task)
        restore.assert_called_once()
        self.assertEqual(result.status, "FAILED")
        self.assertNotIn("dummy sensitive value", result.error)
        self.assertEqual(self.monitor.get_and_clear_changes(), ["A"])

    def test_empty_report_still_sends(self):
        with patch("tasks.report_task.send_report_email", return_value=True) as sender:
            result = self.handler.execute(self.task)
        sender.assert_called_once_with({"email": self.settings}, [])
        self.assertTrue(result.success)
        self.assertEqual(result.details["event_count"], 0)

    def test_failed_batch_precedes_new_events_detected_during_send(self):
        self.record("A", "B")
        def send(config, batch):
            self.record("C")
            return False
        with patch("tasks.report_task.send_report_email", side_effect=send):
            self.handler.execute(self.task)
        self.assertEqual(self.monitor.get_and_clear_changes(), ["A", "B", "C"])

    def test_success_keeps_new_events_for_next_report(self):
        self.record("A")
        def send(config, batch):
            self.record("B")
            return True
        with patch("tasks.report_task.send_report_email", side_effect=send):
            self.handler.execute(self.task)
        self.assertEqual(self.monitor.get_and_clear_changes(), ["B"])

    def test_identical_legitimate_events_are_not_deduplicated(self):
        self.record("same", "same")
        def send(config, batch):
            self.record("same")
            return False
        with patch("tasks.report_task.send_report_email", side_effect=send):
            self.handler.execute(self.task)
        self.assertEqual(self.monitor.get_and_clear_changes(), ["same"] * 3)

    def test_restoration_failure_does_not_claim_events_retained(self):
        self.record("A")
        with patch("tasks.report_task.send_report_email", return_value=False), patch.object(self.monitor, "restore_changes", side_effect=OSError("dummy sensitive failure")) as restore:
            result = self.handler.execute(self.task)
        restore.assert_called_once()
        self.assertEqual(result.status, "FAILED")
        self.assertFalse(result.details["events_restored"])
        self.assertIn("could not be confirmed", result.error)
        self.assertNotIn("events retained", result.error)
        self.assertNotIn("dummy sensitive failure", result.error)

    def test_both_send_and_restoration_failure_return_safe_failed_result(self):
        self.record("A")
        with patch("tasks.report_task.send_report_email", side_effect=RuntimeError("dummy send")), patch.object(self.monitor, "restore_changes", side_effect=OSError("dummy restoration")) as restore:
            result = self.handler.execute(self.task)
        restore.assert_called_once()
        self.assertFalse(result.success)
        self.assertFalse(result.details["events_restored"])

    def test_interrupts_restore_once_and_are_not_swallowed(self):
        for error in (KeyboardInterrupt(), SystemExit(2)):
            self.record("A")
            with patch("tasks.report_task.send_report_email", side_effect=error), patch.object(self.monitor, "restore_changes", wraps=self.monitor.restore_changes) as restore:
                with self.assertRaises(type(error)):
                    self.handler.execute(self.task)
            restore.assert_called_once_with(["A"])
            self.assertEqual(self.monitor.get_and_clear_changes(), ["A"])

    def test_interrupt_during_send_still_propagates_if_restoration_fails(self):
        self.record("A")
        with patch("tasks.report_task.send_report_email", side_effect=KeyboardInterrupt()), patch.object(self.monitor, "restore_changes", side_effect=OSError("dummy restoration")):
            with self.assertRaises(KeyboardInterrupt):
                self.handler.execute(self.task)

    def test_canonical_folder_alias_resolves_same_monitor(self):
        task = TaskDefinition("report", "Report", "folder_report", True, DailyTrigger(12, 35),
                              {"path": self.path / ".." / self.path.name})
        with patch("tasks.report_task.send_report_email", return_value=True):
            self.assertTrue(self.handler.execute(task).success)

    def test_dependency_mapping_is_snapshotted(self):
        monitors = {self.path: self.monitor}
        handler = FolderReportHandler(self.settings, monitors)
        monitors.clear()
        with patch("tasks.report_task.send_report_email", return_value=True):
            self.assertTrue(handler.execute(self.task).success)

    def test_existing_report_text_is_reused(self):
        from tasks.email_task import send_report_email
        self.record("A", "B")
        with patch("tasks.report_task.send_report_email", side_effect=send_report_email), patch("tasks.email_task.send_email", return_value=True) as sender:
            self.assertTrue(self.handler.execute(self.task).success)
        sender.assert_called_once_with({"email": self.settings}, "Automation Engine — Daily Folder Report",
            "The following changes were detected in the watched folder:\n\nA\nB\n\nThis report was generated automatically.")

    def test_mocked_smtp_cleanup_failure_does_not_restore_accepted_report(self):
        self.record("A")
        with patch.dict(os.environ, {"SMTP_PASSWORD": "dummy-test-only"}), patch("tasks.email_task.smtplib.SMTP") as smtp, patch("tasks.email_task.logger"), patch.object(self.monitor, "restore_changes", wraps=self.monitor.restore_changes) as restore:
            smtp.return_value.__enter__.return_value.sendmail.return_value = {}
            smtp.return_value.__exit__.side_effect = TimeoutError("dummy cleanup")
            result = self.handler.execute(self.task)
        self.assertTrue(result.success)
        restore.assert_not_called()
