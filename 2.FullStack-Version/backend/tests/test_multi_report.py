import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from scheduler.job_scheduler import JobScheduler
from tasks.email_task import send_folder_report_email
from tasks.file_monitor import FileMonitor
from tasks.models import DailyTrigger, TaskDefinition
from tasks.registry import TaskRegistry
from tasks.runner import TaskRunner


EMAIL = {"sender": "sender@example.com", "receiver": "receiver@example.com",
         "smtp_server": "smtp.example.com", "smtp_port": 587}


class MultiReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="multi-report-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patch("smtplib.SMTP", side_effect=AssertionError("Real SMTP forbidden")).start()
        self.addCleanup(patch.stopall)

    def make(self, count, times=None):
        tasks, monitors = [], {}
        for index in range(count):
            folder = self.root / f"folder-{index}"
            folder.mkdir()
            hour, minute = (times[index] if times else (12, 35))
            task = TaskDefinition(f"job-{index}", f"Job {index}", "folder_report", True,
                                  DailyTrigger(hour, minute), {"path": folder})
            monitor = FileMonitor(folder, report_job_id=task.id)
            monitor._record_change(f"NEW file detected: file-{index}.txt")
            tasks.append(task)
            monitors[folder.resolve()] = monitor
        runner = TaskRunner(TaskRegistry(EMAIL, monitors))
        scheduler = JobScheduler(tasks, runner, wall_clock=lambda: datetime(2026, 1, 2, 12, 35))
        scheduler._initialize_schedule()
        return tasks, monitors, runner, scheduler

    def test_three_same_time_jobs_send_once_with_owned_sections(self):
        tasks, monitors, _runner, scheduler = self.make(3)
        with patch("tasks.report_task.send_folder_report_email", return_value=True) as sender:
            scheduler._run_due_tasks()
        sender.assert_called_once()
        reports = sender.call_args.args[1]
        self.assertEqual([report["task_id"] for report in reports], [task.id for task in tasks])
        for index, report in enumerate(reports):
            self.assertEqual(report["changes"], (f"NEW file detected: file-{index}.txt",))
            self.assertEqual(monitors[(self.root / f"folder-{index}").resolve()].get_and_clear_changes(), [])

    def test_failed_consolidated_send_restores_every_batch_once(self):
        _tasks, monitors, _runner, scheduler = self.make(3)
        restores = [patch.object(monitor, "restore_changes", wraps=monitor.restore_changes).start()
                    for monitor in monitors.values()]
        with patch("tasks.report_task.send_folder_report_email", return_value=False):
            scheduler._run_due_tasks()
        self.assertTrue(all(restore.call_count == 1 for restore in restores))
        for index, monitor in enumerate(monitors.values()):
            self.assertEqual(monitor.get_and_clear_changes(),
                             [f"NEW file detected: file-{index}.txt"])

    def test_different_daily_times_send_separately(self):
        tasks, _monitors, _runner, scheduler = self.make(2, ((12, 35), (12, 36)))
        now = [datetime(2026, 1, 2, 12, 35)]
        scheduler._wall_clock = lambda: now[0]
        with patch("tasks.report_task.send_report_email", return_value=True) as single:
            scheduler._run_due_tasks()
            now[0] = datetime(2026, 1, 2, 12, 36)
            scheduler._run_due_tasks()
        self.assertEqual(single.call_count, 2)
        self.assertEqual([call.args[1] for call in single.call_args_list],
                         [["NEW file detected: file-0.txt"], ["NEW file detected: file-1.txt"]])

    def test_thirty_jobs_are_one_linear_batch(self):
        tasks, _monitors, _runner, scheduler = self.make(30)
        with patch("tasks.report_task.send_folder_report_email", return_value=True) as sender:
            scheduler._run_due_tasks()
        sender.assert_called_once()
        self.assertEqual(len(sender.call_args.args[1]), len(tasks))

    def test_ten_jobs_are_one_linear_batch(self):
        tasks, _monitors, _runner, scheduler = self.make(10)
        with patch("tasks.report_task.send_folder_report_email", return_value=True) as sender:
            scheduler._run_due_tasks()
        sender.assert_called_once()
        self.assertEqual(len(sender.call_args.args[1]), len(tasks))

    def test_plain_text_message_has_identity_counts_and_no_change_section(self):
        reports = ({"task_id": "job-a", "folder": self.root / "alpha",
                    "changes": ("NEW file detected: a.txt", "MODIFIED file: b.txt")},
                   {"task_id": "job-b", "folder": self.root / "beta", "changes": ()})
        when = datetime(2026, 1, 2, 20, 35)
        with patch("tasks.email_task.send_email", return_value=True) as sender:
            self.assertTrue(send_folder_report_email({"email": EMAIL}, reports, when))
        config, subject, body = sender.call_args.args
        self.assertEqual(config, {"email": EMAIL})
        self.assertEqual(subject, "DevOps Automation Engine — 2 Monitoring Reports — 20:35")
        for value in ("Monitoring Job: job-a", "Monitoring Job: job-b",
                      "NEW: a.txt", "MODIFIED: b.txt", "No changes detected.",
                      "New files: 1", "Modified files: 1", "Deleted files: 0"):
            self.assertIn(value, body)

    def test_single_message_subject_contains_task_id(self):
        report = ({"task_id": "job-a", "folder": self.root / "alpha", "changes": ()},)
        with patch("tasks.email_task.send_email", return_value=True) as sender:
            send_folder_report_email({"email": EMAIL}, report, datetime(2026, 1, 2, 20, 35))
        self.assertEqual(sender.call_args.args[1], "DevOps Automation Engine — Folder Report — job-a")

    def test_mixed_job_events_have_correct_section_and_overall_counts(self):
        reports = ({"task_id": "one", "folder": self.root / "one",
                    "changes": ("NEW file detected: a", "DELETED file: b")},
                   {"task_id": "two", "folder": self.root / "two",
                    "changes": ("MODIFIED file: c", "NEW file detected: d")})
        with patch("tasks.email_task.send_email", return_value=True) as sender:
            send_folder_report_email({"email": EMAIL}, reports, datetime(2026, 1, 2, 20, 35))
        body = sender.call_args.args[2]
        self.assertIn("New files: 2", body)
        self.assertIn("Modified files: 1", body)
        self.assertIn("Deleted files: 1", body)
        first, second = body.split("Monitoring Job: one", 1)[1].split("Monitoring Job: two", 1)
        self.assertIn("NEW: a", first)
        self.assertNotIn("MODIFIED: c", first)
        self.assertIn("MODIFIED: c", second)

    def test_all_no_change_jobs_still_send_existing_due_report(self):
        reports = tuple({"task_id": f"job-{index}", "folder": self.root / str(index), "changes": ()}
                        for index in range(3))
        with patch("tasks.email_task.send_email", return_value=True) as sender:
            send_folder_report_email({"email": EMAIL}, reports, datetime(2026, 1, 2, 20, 35))
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(sender.call_args.args[2].count("No changes detected."), 3)

    def test_monitor_log_has_job_id_but_not_filename_or_path(self):
        monitor = FileMonitor(self.root, report_job_id="job-safe")
        monitor.known_files = {}
        with patch.object(monitor, "_take_snapshot", return_value={"private.txt": 1}), \
                patch("tasks.file_monitor.logger") as logger:
            monitor._check_changes()
        logger.info.assert_called_once_with("%s file detected | job=%r", "NEW", "job-safe")
        self.assertNotIn("private.txt", str(logger.info.call_args))
        self.assertNotIn(str(self.root), str(logger.info.call_args))


if __name__ == "__main__":
    unittest.main()
