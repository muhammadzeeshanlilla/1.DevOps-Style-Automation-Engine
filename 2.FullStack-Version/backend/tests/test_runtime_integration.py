"""Fake-clock scheduling and isolated runtime wiring; no real SMTP."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import main
import cli.handler as cli
from config.loader import validate_config, ConfigurationError
from scheduler.job_scheduler import JobScheduler
from tasks.models import DailyTrigger, IntervalTrigger, FileEventTrigger, TaskDefinition, WorkflowConfiguration, TaskExecutionResult
from tasks.registry import TaskRegistry
from tasks.runner import TaskRunner
from utils.process_manager import ProcessManager
from utils.paths import PROJECT_ROOT

EMAIL = {"sender": "sender@example.com", "receiver": "receiver@example.com", "smtp_server": "smtp.example.com", "smtp_port": 587}


def email_task(identifier, trigger, enabled=True, task_type="email"):
    return TaskDefinition(identifier, identifier, task_type, enabled, trigger, {"subject": "Subject", "body": "Body"})


class Clock:
    def __init__(self, now=None):
        self.now = now or datetime(2026, 1, 1, 12, 35)
        self.tick = 100.0
    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.tick += seconds


class ConfiguredSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.runner = Mock()
        self.runner.run.side_effect = lambda task: TaskExecutionResult(task.id, "SUCCESS")
        guard = patch("smtplib.SMTP", side_effect=AssertionError("Real SMTP forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def scheduler(self, *tasks, runner=None):
        scheduler = JobScheduler(tasks, runner or self.runner, lambda: self.clock.now, lambda: self.clock.tick)
        scheduler._initialize_schedule()
        self.addCleanup(scheduler.stop, 2)
        return scheduler

    def test_multiple_tasks_due_together_follow_configuration_order(self):
        tasks = [email_task("a", DailyTrigger(12, 35)), email_task("b", DailyTrigger(12, 35))]
        scheduler = self.scheduler(*tasks)
        scheduler._run_due_tasks()
        self.assertEqual([call.args[0].id for call in self.runner.run.call_args_list], ["a", "b"])

    def test_start_during_matching_minute_runs(self):
        self.clock.now = datetime(2026, 1, 1, 12, 35, 59)
        scheduler = self.scheduler(email_task("a", DailyTrigger(12, 35)))
        scheduler._run_due_tasks()
        self.runner.run.assert_called_once()

    def test_start_after_matching_minute_waits_until_next_day(self):
        self.clock.now = datetime(2026, 1, 1, 12, 36)
        scheduler = self.scheduler(email_task("a", DailyTrigger(12, 35)))
        scheduler._run_due_tasks()
        self.runner.run.assert_not_called()
        self.clock.advance(23 * 3600 + 59 * 60)
        scheduler._run_due_tasks()
        self.runner.run.assert_called_once()

    def test_daily_claim_prevents_duplicates_even_after_failure(self):
        self.runner.run.side_effect = lambda task: TaskExecutionResult(task.id, "FAILED")
        scheduler = self.scheduler(email_task("a", DailyTrigger(12, 35)))
        scheduler._run_due_tasks()
        scheduler._run_due_tasks()
        self.clock.advance(120)
        scheduler._run_due_tasks()
        self.runner.run.assert_called_once()
        self.clock.advance(24 * 3600)
        scheduler._run_due_tasks()
        self.assertEqual(self.runner.run.call_count, 2)

    def test_delayed_daily_task_runs_later_same_day(self):
        scheduler = self.scheduler(email_task("a", DailyTrigger(12, 35)), email_task("b", DailyTrigger(12, 36)))
        def execute(task):
            if task.id == "a":
                self.clock.advance(180)
            return TaskExecutionResult(task.id, "SUCCESS")
        self.runner.run.side_effect = execute
        scheduler._run_due_tasks()
        self.assertEqual(self.runner.run.call_count, 2)

    def test_historical_days_are_not_replayed(self):
        scheduler = self.scheduler(email_task("a", DailyTrigger(12, 35)))
        self.clock.advance(3 * 86400)
        scheduler._run_due_tasks()
        scheduler._run_due_tasks()
        self.runner.run.assert_called_once()
        self.assertEqual(scheduler._daily_claims["a"], self.clock.now.date())

    def test_wall_clock_backwards_does_not_repeat_daily_occurrence(self):
        scheduler = self.scheduler(email_task("a", DailyTrigger(12, 35)))
        scheduler._run_due_tasks()
        self.clock.now -= timedelta(hours=1)
        scheduler._run_due_tasks()
        self.clock.now += timedelta(hours=1)
        scheduler._run_due_tasks()
        self.runner.run.assert_called_once()

    def test_interval_first_deadline_is_after_full_interval(self):
        scheduler = self.scheduler(email_task("a", IntervalTrigger(2)))
        scheduler._run_due_tasks()
        self.clock.advance(119)
        scheduler._run_due_tasks()
        self.runner.run.assert_not_called()
        self.clock.advance(1)
        scheduler._run_due_tasks()
        self.runner.run.assert_called_once()
        self.assertEqual(scheduler._interval_due["a"], 340.0)

    def test_interval_missed_boundaries_coalesce_without_burst(self):
        scheduler = self.scheduler(email_task("a", IntervalTrigger(1)))
        self.clock.advance(305)
        scheduler._run_due_tasks()
        scheduler._run_due_tasks()
        self.runner.run.assert_called_once()
        self.assertEqual(scheduler._interval_due["a"], 460.0)

    def test_failed_interval_advances_to_future_boundary(self):
        self.runner.run.side_effect = lambda task: TaskExecutionResult(task.id, "FAILED")
        scheduler = self.scheduler(email_task("a", IntervalTrigger(1)))
        self.clock.advance(60)
        scheduler._run_due_tasks()
        self.assertEqual(scheduler._interval_due["a"], 220.0)
        scheduler._run_due_tasks()
        self.runner.run.assert_called_once()

    def test_slow_interval_execution_advances_after_completion(self):
        scheduler = self.scheduler(email_task("a", IntervalTrigger(1)))
        self.clock.advance(60)
        self.runner.run.side_effect = lambda task: self.clock.advance(185)
        scheduler._run_due_tasks()
        self.assertEqual(scheduler._interval_due["a"], 400.0)
        scheduler._run_due_tasks()
        self.runner.run.assert_called_once()

    def test_intervals_ignore_wall_clock_adjustment(self):
        scheduler = self.scheduler(email_task("a", IntervalTrigger(1)))
        self.clock.now += timedelta(days=10)
        scheduler._run_due_tasks()
        self.runner.run.assert_not_called()
        self.clock.tick += 60
        scheduler._run_due_tasks()
        self.runner.run.assert_called_once()

    def test_restart_retains_daily_claim_and_resets_interval_deadline(self):
        scheduler = self.scheduler(email_task("daily", DailyTrigger(12, 35)), email_task("interval", IntervalTrigger(1)))
        scheduler._run_due_tasks()
        scheduler.stop()
        self.clock.tick += 30
        scheduler._initialize_schedule()
        scheduler._stop_event.clear()
        scheduler._run_due_tasks()
        self.assertEqual(self.runner.run.call_count, 1)
        self.assertEqual(scheduler._interval_due["interval"], 190.0)

    def test_disabled_tasks_skip_once_per_start_without_handler(self):
        registry = Mock()
        runner = TaskRunner(registry)
        scheduler = self.scheduler(email_task("a", DailyTrigger(12, 35), False), email_task("event", FileEventTrigger(Path("unused"), ("new",)), False), runner=runner)
        results = []
        original = runner.run
        def run(task):
            result = original(task)
            results.append(result)
            return result
        def wait(timeout):
            scheduler._run_due_tasks()
            scheduler.request_stop()
        with patch.object(runner, "run", side_effect=run), patch.object(scheduler._stop_event, "wait", side_effect=wait):
            scheduler._scheduler_loop()
        self.assertEqual([result.status for result in results], ["SKIPPED", "SKIPPED"])
        registry.resolve.assert_not_called()

    def test_unknown_type_and_handler_exception_do_not_stop_later_tasks(self):
        runner = TaskRunner(TaskRegistry(EMAIL, {}))
        tasks = [email_task("unknown", DailyTrigger(12, 35), task_type="forged"), email_task("failed", DailyTrigger(12, 35)), email_task("good", DailyTrigger(12, 35))]
        scheduler = self.scheduler(*tasks, runner=runner)
        results = []
        original = runner.run
        with patch("tasks.email_action.send_email", side_effect=[RuntimeError("sensitive diagnostic"), True]) as sender, patch.object(runner, "run", side_effect=lambda task: results.append(original(task)) or results[-1]):
            scheduler._run_due_tasks()
        self.assertEqual([result.status for result in results], ["FAILED", "FAILED", "SUCCESS"])
        self.assertEqual(sender.call_count, 2)

    def test_failed_handler_result_does_not_stop_later_task(self):
        runner = TaskRunner(TaskRegistry(EMAIL, {}))
        scheduler = self.scheduler(email_task("a", DailyTrigger(12, 35)), email_task("b", DailyTrigger(12, 35)), runner=runner)
        with patch("tasks.email_action.send_email", side_effect=[False, True]) as sender:
            scheduler._run_due_tasks()
        self.assertEqual(sender.call_count, 2)

    def test_shutdown_during_execution_prevents_next_due_task(self):
        scheduler = self.scheduler(email_task("a", DailyTrigger(12, 35)), email_task("b", DailyTrigger(12, 35)))
        self.runner.run.side_effect = lambda task: scheduler.request_stop()
        scheduler._run_due_tasks()
        self.runner.run.assert_called_once()

    def test_wait_uses_nearest_deadline_and_is_capped(self):
        scheduler = self.scheduler(email_task("a", IntervalTrigger(1)))
        self.assertEqual(scheduler._next_wait(), 60)
        self.clock.advance(55)
        self.assertEqual(scheduler._next_wait(), 5)


class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="runtime-integration-")
        self.root = Path(temporary.name).resolve()
        self.addCleanup(temporary.cleanup)
        self.manager = ProcessManager(self.root)
        self.addCleanup(self.manager.release)
        for replacement in (patch("smtplib.SMTP", side_effect=AssertionError("Real SMTP forbidden")), patch("os.kill", side_effect=AssertionError("Process signals forbidden"))):
            replacement.start()
            self.addCleanup(replacement.stop)

    def legacy(self, path=None):
        return validate_config({"watch_folder": str(path or self.root), "email": EMAIL, "schedule": {"hour": 12, "minute": 35}})

    def test_email_only_constructs_no_monitors(self):
        with patch.object(main, "FileMonitor") as monitor:
            engine = main.Engine(WorkflowConfiguration(EMAIL, (email_task("a", DailyTrigger(12, 35)),)), self.manager)
        monitor.assert_not_called()
        self.assertEqual(engine.file_monitors, {})
        self.assertIs(engine.job_scheduler.runner, engine.task_runner)

    def test_multiple_report_folders_have_distinct_monitors(self):
        other = self.root / "other"
        other.mkdir()
        tasks = tuple(TaskDefinition(str(index), str(index), "folder_report", True, DailyTrigger(12, 35), {"path": str(folder)}) for index, folder in enumerate((self.root, other)))
        with patch.object(main, "FileMonitor") as monitor:
            engine = main.Engine(WorkflowConfiguration(EMAIL, tasks), self.manager)
        self.assertEqual(monitor.call_count, 2)
        self.assertEqual(set(engine.file_monitors), {self.root, other})

    def test_disabled_report_creates_no_monitor(self):
        task = TaskDefinition("report", "report", "folder_report", False, DailyTrigger(12, 35), {"path": str(self.root)})
        with patch.object(main, "FileMonitor") as monitor:
            engine = main.Engine(WorkflowConfiguration(EMAIL, (task,)), self.manager)
        monitor.assert_not_called()
        self.assertEqual(engine.file_monitors, {})

    def test_legacy_and_version_two_reach_same_runner_path(self):
        legacy = self.legacy()
        task = legacy.tasks[0]
        modern = validate_config({"schema_version": 2, "email": EMAIL, "tasks": [{"id": task.id, "name": task.name, "type": "folder_report", "enabled": True, "trigger": {"type": "daily", "hour": 12, "minute": 35}, "parameters": {"path": str(self.root)}}]})
        self.assertEqual(legacy, modern)
        for config in (legacy, modern):
            engine = main.Engine(config, self.manager)
            monitor = engine.file_monitors[self.root]
            monitor._record_change("A")
            engine.job_scheduler._wall_clock = lambda: datetime(2026, 1, 1, 12, 35)
            engine.job_scheduler._initialize_schedule()
            with patch("tasks.report_task.send_report_email", return_value=True) as sender, patch.object(engine.task_runner, "run", wraps=engine.task_runner.run) as runner:
                engine.job_scheduler._run_due_tasks()
            runner.assert_called_once_with(task)
            sender.assert_called_once_with({"email": EMAIL}, ["A"])
            self.assertEqual(monitor.get_and_clear_changes(), [])

    def test_integrated_failed_report_restores_once_before_new_events(self):
        engine = main.Engine(self.legacy(), self.manager)
        monitor = engine.file_monitors[self.root]
        monitor._record_change("A")
        monitor._record_change("A")
        scheduler = engine.job_scheduler
        scheduler._wall_clock = lambda: datetime(2026, 1, 1, 12, 35)
        scheduler._initialize_schedule()
        def send(config, batch):
            monitor._record_change("B")
            return False
        with patch("tasks.report_task.send_report_email", side_effect=send) as sender, patch.object(monitor, "restore_changes", wraps=monitor.restore_changes) as restore:
            scheduler._run_due_tasks()
            scheduler._run_due_tasks()
        sender.assert_called_once()
        restore.assert_called_once()
        self.assertEqual(monitor.get_and_clear_changes(), ["A", "A", "B"])

    def test_enabled_file_event_constructs_dependencies_before_workers(self):
        task = email_task("event", FileEventTrigger(self.root, ("new",)))
        with patch.object(main, "FileMonitor") as monitor:
            engine = main.Engine(WorkflowConfiguration(EMAIL, (task,)), self.manager)
        self.assertEqual(len(engine.file_monitors), 1)
        monitor.return_value.start.assert_not_called()
        self.assertIsNotNone(engine.event_dispatcher)
        self.assertFalse(self.manager.owns_instance)

    def test_enabled_file_event_cli_start_uses_cleanup(self):
        config = WorkflowConfiguration(EMAIL, (email_task("event", FileEventTrigger(self.root, ("new",))),))
        with patch.object(main, "load_config", return_value=config), patch.object(main.Engine, "start", return_value=0), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.run_cli(["start"], self.manager), 0)

    def test_scheduler_accepts_enabled_file_event_with_dispatcher(self):
        from tasks.event_dispatcher import EventDispatcher
        tasks = (email_task("event", FileEventTrigger(self.root, ("new",))),)
        scheduler = JobScheduler(tasks, Mock(), event_dispatcher=EventDispatcher(tasks))
        self.assertIsNotNone(scheduler.event_dispatcher)

    def test_all_monitors_start_before_scheduler_with_ownership(self):
        other = self.root / "other"
        other.mkdir()
        tasks = tuple(TaskDefinition(str(index), str(index), "folder_report", True, DailyTrigger(12, 35), {"path": str(folder)}) for index, folder in enumerate((self.root, other)))
        workers = [Mock(), Mock(), Mock()]
        order = []
        for index, worker in enumerate(workers):
            worker.is_running.return_value = False
            def start(index=index):
                self.assertTrue(self.manager.owns_instance)
                order.append(index)
            worker.start.side_effect = start
        with patch.object(main, "FileMonitor", side_effect=workers[:2]), patch.object(main, "JobScheduler", return_value=workers[2]), patch.object(self.manager, "stop_requested", return_value=True), contextlib.redirect_stdout(io.StringIO()):
            engine = main.Engine(WorkflowConfiguration(EMAIL, tasks), self.manager)
            self.assertEqual(engine.start(), 0)
        self.assertEqual(order, [0, 1, 2])
        for worker in workers:
            worker.request_stop.assert_called_once()
            worker.stop.assert_called_once()
        self.assertFalse(self.manager.owns_instance)

    def test_second_monitor_failure_cleans_attempted_workers_only(self):
        other = self.root / "other"
        other.mkdir()
        tasks = tuple(TaskDefinition(str(index), str(index), "folder_report", True, DailyTrigger(12, 35), {"path": str(folder)}) for index, folder in enumerate((self.root, other)))
        workers = [Mock(), Mock(), Mock()]
        for worker in workers:
            worker.is_running.return_value = False
        workers[1].start.side_effect = RuntimeError("controlled failure")
        with patch.object(main, "FileMonitor", side_effect=workers[:2]), patch.object(main, "JobScheduler", return_value=workers[2]), contextlib.redirect_stdout(io.StringIO()):
            engine = main.Engine(WorkflowConfiguration(EMAIL, tasks), self.manager)
            self.assertEqual(engine.start(), 1)
        workers[0].stop.assert_called_once()
        workers[1].stop.assert_called_once()
        workers[2].start.assert_not_called()
        workers[2].stop.assert_not_called()
        self.assertFalse(self.manager.owns_instance)

    def test_loading_from_another_current_directory_uses_shared_config_path(self):
        path = self.root / "settings.json"
        path.write_text(json.dumps({"watch_folder": "watched_folder", "email": EMAIL, "schedule": {"hour": 12, "minute": 35}}))
        original = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.object(main, "CONFIG_PATH", path):
                config = main.load_config()
            self.assertEqual(Path(config.tasks[0].parameters["path"]), PROJECT_ROOT / "watched_folder")
        finally:
            os.chdir(original)

    def test_task_example_matches_loader_schema(self):
        from config.loader import load_config
        config = load_config(PROJECT_ROOT / "config" / "settings.tasks.example.json")
        self.assertTrue(any(isinstance(task.trigger, DailyTrigger) for task in config.tasks))
        self.assertTrue(any(isinstance(task.trigger, IntervalTrigger) for task in config.tasks))
        self.assertTrue(any(not task.enabled and isinstance(task.trigger, FileEventTrigger) for task in config.tasks))

    def test_version_two_status_is_disk_configuration_not_live_tasks(self):
        path = self.root / "settings.json"
        path.write_text(json.dumps({"schema_version": 2, "tasks": [{"id": "disk-task", "enabled": False, "trigger": {"type": "interval", "every_minutes": 5}}]}))
        self.manager.acquire()
        self.manager.publish_state("RUNNING")
        with patch.object(cli, "CONFIG_PATH", path), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main.run_cli(["status"], self.manager), 0)
        self.assertIn("Engine status: RUNNING", output.getvalue())
        self.assertIn("not live task/worker status", output.getvalue())
        self.assertIn("disk-task", output.getvalue())

    def test_malformed_optional_task_display_does_not_break_status(self):
        path = self.root / "settings.json"
        self.manager.acquire()
        for data in ({"schema_version": 2, "tasks": [None]}, {"schema_version": 2, "tasks": [{"id": "a", "enabled": True, "trigger": None}]}, {"schema_version": 2, "tasks": [{"id": "a", "enabled": True, "trigger": {"type": {"password": "never-display"}}}]}, []):
            path.write_text(json.dumps(data))
            with patch.object(cli, "CONFIG_PATH", path), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main.run_cli(["status"], self.manager), 0)
            self.assertIn("unavailable or invalid", output.getvalue())
            self.assertNotIn("never-display", output.getvalue())

    def test_runtime_scheduler_has_no_direct_report_email_or_batch_path(self):
        import inspect
        source = inspect.getsource(JobScheduler)
        for forbidden in ("send_email", "send_report_email", "get_and_clear_changes", "restore_changes"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
