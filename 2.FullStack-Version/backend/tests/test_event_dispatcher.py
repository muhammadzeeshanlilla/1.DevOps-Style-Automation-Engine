import contextlib
import io
import os
import threading
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock, patch
import main
from tasks.events import FileEvent
from tasks.event_dispatcher import EventDispatcher, folder_key
from tasks.models import FileEventTrigger, DailyTrigger, IntervalTrigger, TaskDefinition, WorkflowConfiguration, TaskExecutionResult
from tasks.registry import TaskRegistry
from tasks.runner import TaskRunner
from tasks.file_monitor import FileMonitor
from scheduler.job_scheduler import JobScheduler
from utils.process_manager import ProcessManager, AlreadyRunning

EMAIL = {"sender":"sender@example.com","receiver":"receiver@example.com","smtp_server":"smtp.example.com","smtp_port":587}

class EventDispatcherTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="event-dispatch-")
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name).resolve()
        self.tasks = (self.task("one"), self.task("two"))
        self.dispatcher = EventDispatcher(self.tasks)
        self.dispatcher.reopen()
        self.addCleanup(self.dispatcher.close)
        guard = patch("smtplib.SMTP", side_effect=AssertionError("Real SMTP forbidden"))
        guard.start(); self.addCleanup(guard.stop)
    def task(self, identity, events=("new","modified","deleted"), enabled=True, folder=None, kind="email"):
        return TaskDefinition(identity, identity, kind, enabled, FileEventTrigger(folder or self.folder, events), {"subject":"Subject","body":"Body"})
    def event(self, kind="new", folder=None):
        folder = folder or self.folder
        return FileEvent(kind, folder, folder / "a.txt", datetime.now())
    def scheduler(self, tasks=None):
        tasks = tasks or self.tasks
        runner = TaskRunner(TaskRegistry(EMAIL, {}))
        scheduler = JobScheduler(tasks, runner, event_dispatcher=self.dispatcher)
        self.addCleanup(scheduler.stop, 2)
        return scheduler
    def test_new_modified_deleted_matching(self):
        for kind in ("new","modified","deleted"):
            self.assertEqual(self.dispatcher.matching_tasks(self.event(kind)), self.tasks)
    def test_type_filter(self):
        dispatcher = EventDispatcher((self.task("new-only", ("new",)),))
        self.assertEqual(dispatcher.matching_tasks(self.event("deleted")), ())
    def test_folder_filter_and_no_match(self):
        other = self.folder / "other"; other.mkdir()
        self.assertEqual(self.dispatcher.matching_tasks(self.event(folder=other)), ())
    def test_disabled_task_never_matches(self):
        dispatcher = EventDispatcher((self.task("disabled", enabled=False),))
        self.assertEqual(dispatcher.matching_tasks(self.event()), ())
    def test_canonical_path_alias(self):
        self.assertEqual(folder_key(self.folder / "."), folder_key(self.folder))
    @unittest.skipUnless(os.name == "nt", "Windows path case semantics")
    def test_windows_case_normalization(self):
        self.assertEqual(folder_key(str(self.folder).upper()), folder_key(self.folder))
    def test_multiple_matches_ordered(self):
        scheduler = self.scheduler()
        with patch("tasks.email_action.send_email", return_value=True), patch.object(scheduler.runner,"run", wraps=scheduler.runner.run) as run:
            scheduler._dispatch_event(self.event())
        self.assertEqual([call.args[0].id for call in run.call_args_list], ["one","two"])
    def test_rapid_repeated_events_preserved(self):
        events = [self.event() for _ in range(100)]
        for event in events: self.assertTrue(self.dispatcher.submit(event))
        self.assertEqual(self.dispatcher.queued_count(), len(events))
        self.assertEqual([self.dispatcher.next_event() for _ in events], events)
        self.assertEqual(self.dispatcher.queued_count(), 0)
        self.assertIsNone(self.dispatcher.next_event())
    def test_concurrent_submission_no_loss(self):
        threads = [threading.Thread(target=lambda: [self.dispatcher.submit(self.event()) for _ in range(50)]) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(2); self.assertFalse(thread.is_alive())
        count = 0
        while self.dispatcher.next_event() is not None: count += 1
        self.assertEqual(count,200)
    def test_handler_failure_and_exception_isolation(self):
        scheduler = self.scheduler()
        for first in (False, RuntimeError("private error")):
            with patch("tasks.email_action.send_email", side_effect=[first, True]) as sender:
                scheduler._dispatch_event(self.event())
                self.assertEqual(sender.call_count,2)
    def test_unknown_forged_type_isolated(self):
        tasks = (self.task("unknown",kind="forged"), self.task("good"))
        dispatcher = EventDispatcher(tasks); dispatcher.reopen()
        scheduler = JobScheduler(tasks,TaskRunner(TaskRegistry(EMAIL,{})),event_dispatcher=dispatcher)
        with patch("tasks.email_action.send_email",return_value=True) as sender: scheduler._dispatch_event(self.event())
        sender.assert_called_once()
        dispatcher.close()
    def test_invalid_submission_context_protected(self):
        with self.assertRaises(TypeError): self.dispatcher.submit("unsafe")
        runner = TaskRunner(TaskRegistry(EMAIL,{}))
        with patch("tasks.email_action.send_email") as sender:
            result = runner.run(self.tasks[0],context={"password":"unsafe"})
        self.assertEqual(result.status,"FAILED"); sender.assert_not_called()
    def test_context_preserves_email_content_and_definition(self):
        task = self.tasks[0]
        with patch("tasks.email_action.send_email", return_value=True) as sender:
            result = TaskRunner(TaskRegistry(EMAIL,{})).run(task,context=self.event())
        self.assertTrue(result.success)
        sender.assert_called_once_with({"email":EMAIL},"Subject","Body")
        self.assertEqual(dict(task.parameters),{"subject":"Subject","body":"Body"})
    def test_context_interrupts_propagate(self):
        for error in (KeyboardInterrupt,SystemExit):
            with patch("tasks.email_action.send_email",side_effect=error):
                with self.assertRaises(error): TaskRunner(TaskRegistry(EMAIL,{})).run(self.tasks[0],context=self.event())
    def test_monitor_emits_all_types_and_keeps_report_list(self):
        monitor = FileMonitor(str(self.folder),event_submit=self.dispatcher.submit)
        monitor.known_files = {"old":1,"edited":1}
        with patch.object(monitor,"_take_snapshot",return_value={"new":1,"edited":2}): monitor._check_changes()
        self.assertEqual([self.dispatcher.next_event().event_type for _ in range(3)],["new","deleted","modified"])
        self.assertEqual(monitor.get_and_clear_changes(),["NEW file detected: new","DELETED file: old","MODIFIED file: edited"])
    def test_report_batch_not_touched_by_dispatch(self):
        monitor = FileMonitor(str(self.folder)); monitor._record_change("A")
        scheduler = self.scheduler()
        with patch.object(monitor,"get_and_clear_changes",side_effect=AssertionError("Report drain forbidden")), patch.object(monitor,"restore_changes",side_effect=AssertionError("Report restore forbidden")), patch("tasks.email_action.send_email",return_value=True): scheduler._dispatch_event(self.event())
        self.assertEqual(monitor.get_and_clear_changes(),["A"])
    def test_close_prevents_submission_and_claims_discards_count(self):
        for _ in range(3): self.dispatcher.submit(self.event())
        with patch("tasks.event_dispatcher.logger") as logger: self.assertEqual(self.dispatcher.close(),3)
        logger.info.assert_called_once_with("Discarded queued file events during shutdown: %d",3)
        self.assertFalse(self.dispatcher.submit(self.event())); self.assertFalse(self.dispatcher.claim())
    def test_active_claim_can_finish_but_reopen_is_rejected(self):
        self.assertTrue(self.dispatcher.claim()); self.dispatcher.close()
        with self.assertRaises(RuntimeError): self.dispatcher.reopen()
        self.dispatcher.finish(); self.dispatcher.reopen(); self.assertTrue(self.dispatcher.claim()); self.dispatcher.finish()
    def test_restart_does_not_replay_abandoned_events(self):
        self.dispatcher.submit(self.event()); self.dispatcher.close(); self.dispatcher.reopen()
        self.assertIsNone(self.dispatcher.next_event())
    def test_close_during_first_match_prevents_second(self):
        scheduler = self.scheduler()
        with patch("tasks.email_action.send_email",side_effect=lambda *args: self.dispatcher.close() or True) as sender: scheduler._dispatch_event(self.event())
        sender.assert_called_once()
    def test_monitor_thread_only_submits_scheduler_executes(self):
        scheduler = self.scheduler()
        entered = threading.Event(); executing = []
        monitor = FileMonitor(str(self.folder),event_submit=self.dispatcher.submit)
        def send(*args): executing.append(threading.current_thread().name); entered.set(); return True
        with patch("tasks.email_action.send_email",side_effect=send), patch.object(monitor,"_take_snapshot",side_effect=[{}, {"a":1}, {"a":1}]):
            scheduler.start(); monitor.start()
            try: self.assertTrue(entered.wait(2))
            finally: monitor.stop(); scheduler.stop()
        self.assertTrue(executing); self.assertEqual(set(executing),{"JobScheduler"})
    def test_busy_queue_returns_to_daily_and_interval_checks(self):
        now = [datetime(2026,1,1,12,35)]; tick = [0.0]
        daily = TaskDefinition("daily","daily","email",True,DailyTrigger(12,35),{"subject":"s","body":"b"})
        interval = TaskDefinition("interval","interval","email",True,IntervalTrigger(1),{"subject":"s","body":"b"})
        tasks = (*self.tasks,daily,interval); calls=[]
        runner = Mock()
        scheduler = JobScheduler(tasks,runner,lambda:now[0],lambda:tick[0],event_dispatcher=self.dispatcher)
        scheduler._initialize_schedule()
        for _ in range(20): self.dispatcher.submit(self.event())
        def run(task,context=None):
            calls.append(task.id)
            if context is not None: tick[0] += 60
            if task.id == "interval": scheduler.request_stop()
        runner.run.side_effect=run
        scheduler._scheduler_loop()
        self.assertIn("daily",calls); self.assertIn("interval",calls)
        self.assertLess(len(calls),10)
    def test_worker_restart_and_duplicate_start(self):
        scheduler = self.scheduler(); scheduler.start(); first=scheduler._thread
        self.assertFalse(scheduler.start()); self.assertTrue(scheduler.stop())
        self.assertTrue(scheduler.start()); self.assertIsNot(first,scheduler._thread); self.assertTrue(scheduler.stop())
    def test_engine_reuses_report_event_monitor(self):
        report=TaskDefinition("report","report","folder_report",True,DailyTrigger(12,35),{"path":self.folder})
        with patch.object(main,"FileMonitor") as constructor:
            engine=main.Engine(WorkflowConfiguration(EMAIL,(report,*self.tasks)))
        constructor.assert_called_once(); self.assertEqual(len(engine.file_monitors),1)
    def test_event_handler_blocked_retains_engine_ownership(self):
        manager=ProcessManager(self.folder); self.addCleanup(manager.release)
        engine=main.Engine(WorkflowConfiguration(EMAIL,self.tasks),manager); engine.WORKER_JOIN_TIMEOUT=.01
        entered=threading.Event(); release=threading.Event(); stopping=threading.Event(); results=[]
        def send(*args): entered.set(); release.wait(5); return True
        original_start=engine.job_scheduler.start
        def start(**kwargs):
            result=original_start(**kwargs); engine.event_dispatcher.submit(self.event()); return result
        with patch.object(engine.job_scheduler,"start",side_effect=start), patch("tasks.email_action.send_email",side_effect=send) as sender, patch.object(main,"logger") as logger, contextlib.redirect_stdout(io.StringIO()):
            logger.warning.side_effect=lambda *args: stopping.set()
            thread=threading.Thread(target=lambda:results.append(engine.start())); thread.start()
            try:
                self.assertTrue(entered.wait(2)); engine.event_dispatcher.submit(self.event())
                controller=ProcessManager(self.folder); controller.request_stop(timeout=0)
                self.assertTrue(stopping.wait(2)); self.assertEqual(controller.inspect_status().state,"STOPPING")
                with self.assertRaises(AlreadyRunning): controller.acquire()
                self.assertTrue(manager.owns_instance); release.set(); thread.join(3)
                self.assertFalse(thread.is_alive()); self.assertEqual(results,[0]); self.assertFalse(manager.owns_instance)
                sender.assert_called_once(); self.assertIsNone(engine.event_dispatcher.next_event())
            finally:
                release.set(); engine._running=False; engine._shutdown_event.set(); thread.join(5)

    def test_scheduler_requires_dispatcher_for_enabled_events(self):
        with self.assertRaisesRegex(ValueError,"event dispatcher"):
            JobScheduler(self.tasks,Mock())
    def test_scheduler_start_preserves_fresh_monitor_submissions(self):
        scheduler=self.scheduler(); self.dispatcher.submit(self.event())
        executed=threading.Event()
        with patch("tasks.email_action.send_email",side_effect=lambda *args: executed.set() or True):
            scheduler.start()
            try: self.assertTrue(executed.wait(2))
            finally: scheduler.stop()
    def test_thread_start_failure_closes_dispatch_and_allows_restart(self):
        scheduler=self.scheduler(); self.dispatcher.submit(self.event())
        with patch("threading.Thread.start",side_effect=RuntimeError("controlled")):
            with self.assertRaises(RuntimeError): scheduler.start()
        self.assertFalse(self.dispatcher.submit(self.event())); self.assertIsNone(self.dispatcher.next_event())
        self.assertTrue(scheduler.stop()); self.assertTrue(scheduler.start()); self.assertTrue(scheduler.stop())
    def test_shutdown_gate_serializes_with_concurrent_claims(self):
        barrier=threading.Barrier(2); claimed=[]
        def claim(): barrier.wait(2); claimed.append(self.dispatcher.claim())
        thread=threading.Thread(target=claim); thread.start(); barrier.wait(2); self.dispatcher.close(); thread.join(2)
        self.assertFalse(thread.is_alive()); self.assertFalse(self.dispatcher.claim())
        if claimed == [True]: self.dispatcher.finish()
        self.assertIn(claimed,([True],[False]))

    def test_no_matching_event_executes_no_handler(self):
        scheduler=self.scheduler(); other=self.folder/"other"; other.mkdir()
        with patch("tasks.email_action.send_email") as sender: scheduler._dispatch_event(self.event(folder=other))
        sender.assert_not_called()
    def test_repeated_events_each_execute_each_matching_task_once(self):
        scheduler=self.scheduler()
        for _ in range(4): self.dispatcher.submit(self.event())
        with patch("tasks.email_action.send_email",return_value=True) as sender:
            for _ in range(4): scheduler._dispatch_event(self.dispatcher.next_event())
        self.assertEqual(sender.call_count,8)
    def test_engine_startup_failure_closes_dispatch_and_releases_ownership(self):
        manager=ProcessManager(self.folder); self.addCleanup(manager.release)
        monitor=Mock(); monitor.is_running.return_value=False
        scheduler=Mock(); scheduler.is_running.return_value=False; scheduler.start.side_effect=RuntimeError("controlled failure")
        with patch.object(main,"FileMonitor",return_value=monitor), patch.object(main,"JobScheduler",return_value=scheduler), patch.object(main,"logger"), contextlib.redirect_stdout(io.StringIO()):
            engine=main.Engine(WorkflowConfiguration(EMAIL,self.tasks),manager)
            self.assertEqual(engine.start(),1)
        self.assertFalse(manager.owns_instance); self.assertFalse(manager.pid_path.exists())
        self.assertFalse(engine.event_dispatcher.submit(self.event())); self.assertFalse(engine.event_dispatcher.claim())
        monitor.request_stop.assert_called_once(); scheduler.request_stop.assert_called_once()

    def test_prepared_engine_dispatch_cannot_reopen_after_shutdown(self):
        scheduler=self.scheduler(); self.dispatcher.close()
        scheduler.start(reopen_dispatch=False)
        try:
            self.assertFalse(self.dispatcher.submit(self.event()))
            self.assertFalse(self.dispatcher.claim())
        finally: scheduler.stop()
