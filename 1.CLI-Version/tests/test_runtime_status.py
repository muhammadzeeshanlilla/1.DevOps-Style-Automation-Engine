"""Isolated observation/serialization tests; no production engine or network."""
import contextlib
import copy
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
import main
import cli.handler as cli
from tasks.models import TaskDefinition, TaskExecutionResult, DailyTrigger, IntervalTrigger, FileEventTrigger, WorkflowConfiguration, NotificationSettings
from tasks.events import FileEvent
from tasks.event_dispatcher import EventDispatcher
from tasks.registry import TaskRegistry
from tasks.runner import TaskRunner
from utils.paths import PROJECT_ROOT
from utils.process_manager import ProcessManager, AlreadyRunning
from utils.runtime_status import RuntimeStatus, read_runtime_snapshot, validate_snapshot, worker_state, MAX_SNAPSHOT_BYTES

EMAIL={"sender":"sender@example.com","receiver":"receiver@example.com","smtp_server":"smtp.example.com","smtp_port":587}

class RuntimeStatusTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix="runtime-status-")
        self.addCleanup(temporary.cleanup); self.root=Path(temporary.name).resolve()
        self.manager=ProcessManager(self.root); self.manager.acquire(); self.addCleanup(self.manager.release)
        self.task=TaskDefinition("task","PRIVATE NAME","email",True,DailyTrigger(12,35),{"subject":"subject","body":"PRIVATE BODY"})
        self.status=RuntimeStatus((self.task,),self.root); self.status.bind(self.manager)
        for guard in (patch("smtplib.SMTP",side_effect=AssertionError("Real SMTP forbidden")),patch("os.kill",side_effect=AssertionError("Signals forbidden"))):
            guard.start(); self.addCleanup(guard.stop)
    def data(self): return self.status.snapshot("RUNNING","alive")
    def metadata(self): return {"pid":os.getpid(),"instance_id":self.manager.instance_id}
    def write(self,data): self.status.path.write_text(json.dumps(data),encoding="utf-8")
    def read(self): return read_runtime_snapshot(self.status.path,self.metadata())
    def output(self,manager=None):
        with patch.object(cli,"CONFIG_PATH",self.root/"broken.json"),contextlib.redirect_stdout(io.StringIO()) as stream:
            code=main.run_cli(["status"],manager or ProcessManager(self.root))
        return code,stream.getvalue()
    def test_unbound_and_nonowner_cannot_publish(self):
        status=RuntimeStatus((self.task,),self.root); owner=ProcessManager(self.root)
        self.assertFalse(status.publish(owner,"RUNNING"))
        with self.assertRaises(ValueError): status.bind(owner)
    def test_duplicate_instance_cannot_overwrite_owner(self):
        self.status.publish(self.manager,"RUNNING"); before=self.status.path.read_bytes()
        other=RuntimeStatus((self.task,),self.root)
        self.assertFalse(other.publish(ProcessManager(self.root),"RUNNING")); self.assertEqual(self.status.path.read_bytes(),before)
    def test_starting_running_stopping_snapshots(self):
        for state in ("STARTING","RUNNING","STOPPING"):
            self.status.publish(self.manager,state); self.assertEqual(self.read().snapshot["lifecycle"],state)
    def test_running_idle_current_none(self): self.assertIsNone(self.data()["current"])
    def test_active_daily_interval_event_sources(self):
        for trigger,source in ((DailyTrigger(12,35),"daily"),(IntervalTrigger(10),"interval"),(FileEventTrigger(self.root,("new",)),"file_event")):
            task=TaskDefinition("task","name","email",True,trigger,self.task.parameters)
            context=FileEvent("new",self.root,self.root/"PRIVATE FILE",datetime.now()) if source=="file_event" else None
            self.status.execution_started(task,context)
            current=self.data()["current"]; self.assertEqual(current["source"],source); self.assertEqual(current["phase"],"action")
    def test_notification_phase_keeps_current_and_finalized_result(self):
        self.status.execution_started(self.task); self.status.result_finalized(TaskExecutionResult("task","SUCCESS")); self.status.notification_started()
        data=self.data(); self.assertEqual(data["current"]["phase"],"lifecycle notification"); self.assertEqual(data["last_result"]["status"],"SUCCESS")
        self.status.execution_finished(); self.assertIsNone(self.data()["current"])
    def test_success_failed_skipped_safe_last_result(self):
        for state in ("SUCCESS","FAILED","SKIPPED"):
            result=TaskExecutionResult("task",state,error="PRIVATE ERROR" if state=="FAILED" else None)
            self.status.result_finalized(result); data=self.data()
            self.assertEqual(data["last_result"]["status"],state); self.assertNotIn("PRIVATE ERROR",json.dumps(data))
    def test_schedule_descriptions_and_counts(self):
        tasks=(self.task,TaskDefinition("interval","name","email",False,IntervalTrigger(10),self.task.parameters),TaskDefinition("event","name","email",True,FileEventTrigger(self.root,("new",)),self.task.parameters))
        status=RuntimeStatus(tasks,self.root); status.bind(self.manager); data=status.snapshot("RUNNING")
        self.assertEqual([t["schedule"] for t in data["tasks"]],["daily 12:35","every 10 minutes","file_event"])
        self.assertEqual((data["configured_count"],data["enabled_count"]),(3,2))
    def test_worker_alive_stopped_unavailable(self):
        worker=Mock()
        for value,expected in ((True,"alive"),(False,"stopped"),(None,"unavailable")):
            worker.is_running.return_value=value; self.assertEqual(worker_state(worker),expected)
        worker.is_running.side_effect=RuntimeError("PRIVATE ERROR"); self.assertEqual(worker_state(worker),"unavailable")
    def test_multiple_monitors_and_queue_summary(self):
        monitors=[{"label":"monitor-1","state":"alive"},{"label":"monitor-2","state":"stopped"}]
        self.status.publish(self.manager,"RUNNING","alive",monitors,3)
        data=self.read().snapshot; self.assertEqual(len(data["monitors"]),2); self.assertEqual(data["queued_events"],3)
        code,output=self.output(); self.assertEqual(code,0); self.assertIn("1/2 alive",output); self.assertIn("approximately 3",output)
    def test_queue_accessor_does_not_drain(self):
        dispatcher=EventDispatcher(()); dispatcher.reopen()
        event=FileEvent("new",self.root,self.root/"a",datetime.now()); dispatcher.submit(event)
        self.assertEqual(dispatcher.queued_count(),1); self.assertIs(dispatcher.next_event(),event); self.assertEqual(dispatcher.queued_count(),0); dispatcher.close()
    def test_missing_snapshot(self): self.assertEqual(self.read().reason,"Snapshot missing")
    def test_malformed_snapshot(self):
        for content in ("not json","[]","null",'{"version":1,"version":2}'):
            self.status.path.write_text(content); self.assertIsNone(self.read().snapshot)
    def test_oversized_snapshot(self):
        self.status.path.write_bytes(b"x"*(MAX_SNAPSHOT_BYTES+1)); self.assertIsNone(self.read().snapshot)
    def test_wrong_pid_instance(self):
        for key,value in (("pid",os.getpid()+1),("instance_id","f"*32)):
            data=self.data(); data[key]=value; self.write(data); self.assertIsNone(self.read().snapshot)
    def test_fresh_and_stale_snapshots(self):
        data=self.data(); self.write(data); self.assertIsNotNone(self.read().snapshot)
        data["published_at"]=(datetime.now(timezone.utc)-timedelta(seconds=6)).isoformat(); self.write(data)
        result=self.read(); self.assertIsNone(result.snapshot); self.assertEqual(result.reason,"Runtime details stale")
    def test_future_timestamp_unavailable(self):
        data=self.data(); data["published_at"]=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat(); self.write(data); self.assertIsNone(self.read().snapshot)
    def test_stale_cli_suppresses_current_and_workers(self):
        self.status.execution_started(self.task); data=self.data(); data["published_at"]=(datetime.now(timezone.utc)-timedelta(seconds=10)).isoformat(); self.write(data)
        _,output=self.output(); self.assertIn("stale",output); self.assertNotIn("Current execution:",output); self.assertNotIn("Scheduler: alive",output)
    def test_unexpected_fields_nested_or_root_rejected(self):
        for nested in (False,True):
            data=self.data()
            if nested: data["tasks"][0]["password"]="PRIVATE SECRET"
            else: data["password"]="PRIVATE SECRET"
            self.write(data); self.assertIsNone(self.read().snapshot)
            _,output=self.output(); self.assertNotIn("PRIVATE SECRET",output)
    def test_invalid_value_types_never_crash_cli(self):
        for key,value in (("version",True),("pid",True),("lifecycle",[]),("scheduler",{}),("published_at","2026-01-01"),("tasks",None),("queued_events",-1),("queued_events",True),("configured_count",99)):
            data=self.data(); data[key]=value; self.write(data); self.assertIsNone(self.read().snapshot); self.assertEqual(self.output()[0],0)
    def test_invalid_execution_result_monitor_schedule_rejected(self):
        self.status.execution_started(self.task); self.status.result_finalized(TaskExecutionResult("task","SUCCESS"))
        for key,value in (("current",{"id":"task","type":"email","source":"shell","started_at":self.data()["published_at"],"phase":"action"}), ("last_result",{"id":"task","status":"UNKNOWN","finished_at":self.data()["published_at"]}), ("monitors",[{"label":"PRIVATE PATH","state":"alive"}])):
            data=self.data(); data[key]=value; self.write(data); self.assertIsNone(self.read().snapshot)
        data=self.data(); data["tasks"][0]["schedule"]="PRIVATE CONFIG"; self.write(data); self.assertIsNone(self.read().snapshot)
    def test_snapshot_copy_isolated(self):
        data=self.data(); data["tasks"][0]["id"]="changed"; self.assertEqual(self.data()["tasks"][0]["id"],"task")
    def test_no_secret_body_name_error_event_path_leakage(self):
        self.status.execution_started(self.task,FileEvent("new",self.root,self.root/"PRIVATE FILE",datetime.now()))
        self.status.result_finalized(TaskExecutionResult("task","FAILED",error="PRIVATE ERROR"))
        with patch.dict(os.environ,{"SMTP_PASSWORD":"task"}): data=self.data()
        text=json.dumps(data)
        for private in ("PRIVATE BODY","PRIVATE NAME","PRIVATE ERROR","PRIVATE FILE",str(self.root),'"task"'): self.assertNotIn(private,text)
    def test_owned_cleanup_and_foreign_snapshot_preserved(self):
        self.status.publish(self.manager,"RUNNING"); self.assertTrue(self.status.cleanup(self.manager)); self.assertFalse(self.status.path.exists())
        data=self.data(); data["instance_id"]="f"*32; self.write(data); self.assertFalse(self.status.cleanup(self.manager)); self.assertTrue(self.status.path.exists())
    def test_nonowner_cleanup_preserves_snapshot(self):
        self.status.publish(self.manager,"RUNNING"); self.assertFalse(self.status.cleanup(ProcessManager(self.root))); self.assertTrue(self.status.path.exists())
    def test_temporary_read_failure_safe(self):
        with patch("utils.runtime_status._read",side_effect=PermissionError("PRIVATE PATH")):
            result=self.read(); self.assertIsNone(result.snapshot); self.assertNotIn("PRIVATE PATH",result.reason)
    def test_atomic_replacement_completed_closed_file(self):
        original=os.replace; observations=[]
        def replace(source,destination):
            data=json.loads(Path(source).read_text()); validate_snapshot(data); observations.append(data)
            # Reopening with write access demonstrates the writer handle has closed.
            with Path(source).open("a"): pass
            original(source,destination)
        with patch("utils.runtime_status.os.replace",side_effect=replace): self.status.publish(self.manager,"RUNNING")
        self.assertEqual(len(observations),1); self.assertEqual(list(self.root.glob("engine.status.*.tmp")),[])
    def test_publication_failure_cleans_temporary_preserves_previous(self):
        self.status.publish(self.manager,"RUNNING"); old=self.status.path.read_bytes()
        with patch("utils.runtime_status.os.replace",side_effect=OSError("replace failed")):
            with self.assertRaises(OSError): self.status.publish(self.manager,"STOPPING")
        self.assertEqual(self.status.path.read_bytes(),old); self.assertEqual(list(self.root.glob("engine.status.*.tmp")),[])
    def test_windows_transient_replace_retry(self):
        original=os.replace; calls=[]
        error=PermissionError("controlled contention"); error.winerror=32
        def replace(source,destination):
            calls.append(1)
            if len(calls)==1: raise error
            return original(source,destination)
        with patch("utils.process_manager.WINDOWS",True),patch("utils.runtime_status.os.replace",side_effect=replace): self.status.publish(self.manager,"RUNNING")
        self.assertEqual(len(calls),2); self.assertIsNotNone(self.read().snapshot)
    def test_windows_transient_read_retry(self):
        self.status.publish(self.manager,"RUNNING"); original=Path.open; count=[0]
        error=PermissionError("controlled contention"); error.winerror=32
        def open_file(path,*args,**kwargs):
            if path==self.status.path:
                count[0]+=1
                if count[0]==1: raise error
            return original(path,*args,**kwargs)
        with patch("utils.process_manager.WINDOWS",True),patch.object(Path,"open",open_file): self.assertIsNotNone(self.read().snapshot)
        self.assertEqual(count[0],2)
    def test_concurrent_state_updates_safe_snapshots(self):
        errors=[]
        def update():
            try:
                for _ in range(100):
                    self.status.execution_started(self.task); self.status.result_finalized(TaskExecutionResult("task","SUCCESS")); self.status.notification_started(); validate_snapshot(self.data()); self.status.execution_finished()
            except Exception as error: errors.append(type(error).__name__)
        threads=[threading.Thread(target=update) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5); self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[])
    def test_concurrent_publication_never_partial_json(self):
        self.status.publish(self.manager,"RUNNING"); errors=[]
        def publish():
            try:
                for _ in range(30): self.status.publish(self.manager,"RUNNING")
            except Exception as error: errors.append(type(error).__name__)
        threads=[threading.Thread(target=publish) for _ in range(3)]
        for thread in threads: thread.start()
        while any(thread.is_alive() for thread in threads):
            if self.read().snapshot is None: errors.append("invalid read")
        for thread in threads: thread.join(5)
        self.assertEqual(errors,[]); self.assertEqual(list(self.root.glob("engine.status.*.tmp")),[])
    def test_cli_broken_config_still_displays_runtime(self):
        (self.root/"broken.json").write_text("broken JSON"); self.status.publish(self.manager,"RUNNING","alive")
        code,output=self.output(); self.assertEqual(code,0); self.assertIn("Tasks loaded: 1 enabled",output); self.assertIn("unavailable or invalid",output)
    def test_cli_from_another_directory(self):
        self.status.publish(self.manager,"RUNNING"); original=Path.cwd()
        try:
            os.chdir(self.root); code,output=self.output(); self.assertEqual(code,0); self.assertIn(self.manager.instance_id,output)
        finally: os.chdir(original)
    def test_instance_changes_during_read_discards_runtime(self):
        self.status.publish(self.manager,"RUNNING"); first=self.manager.inspect_status(); metadata=dict(first.metadata,instance_id="f"*32)
        from utils.process_manager import ProcessStatus
        fake=Mock(); fake.runtime_dir=self.root; fake.inspect_status.side_effect=[first,ProcessStatus("RUNNING",metadata)]
        code,output=self.output(fake); self.assertEqual(code,0); self.assertIn("ownership changed",output); self.assertNotIn("Tasks loaded: 1 enabled",output)
    def test_stopped_engine_ignores_leftover_snapshot(self):
        self.status.publish(self.manager,"RUNNING"); self.manager.release(); code,output=self.output()
        self.assertEqual(code,0); self.assertIn("STOPPED",output); self.assertNotIn("Tasks loaded:",output); self.assertTrue(self.status.path.exists())
    def test_new_owner_replaces_stale_snapshot(self):
        self.status.publish(self.manager,"RUNNING"); previous=self.manager.instance_id; self.manager.release(); self.manager.acquire()
        self.status.bind(self.manager); self.status.publish(self.manager,"STARTING"); self.assertNotEqual(previous,self.read().snapshot["instance_id"])
    def test_runner_observer_errors_preserve_original_result(self):
        observer=Mock(); observer.execution_started.side_effect=RuntimeError("PRIVATE ERROR"); observer.result_finalized.side_effect=RuntimeError("PRIVATE ERROR"); observer.execution_finished.side_effect=RuntimeError("PRIVATE ERROR")
        registry=Mock(); original=TaskExecutionResult("task","SUCCESS",notification_accepted=True); registry.resolve.return_value.execute.return_value=original
        with patch("tasks.runner.logger") as logger: self.assertIs(TaskRunner(registry,status_observer=observer).run(self.task),original)
        self.assertNotIn("PRIVATE ERROR",str(logger.mock_calls))
    def test_runner_hooks_skipped_and_interrupt_cleanup(self):
        disabled=TaskDefinition("task","task","email",False,DailyTrigger(0,0),{})
        observer=Mock(); runner=TaskRunner(Mock(),status_observer=observer); runner.run(disabled)
        observer.execution_started.assert_not_called(); self.assertEqual(observer.result_finalized.call_args.args[0].status,"SKIPPED")
        for error in (KeyboardInterrupt,SystemExit):
            registry=Mock(); registry.resolve.return_value.execute.side_effect=error
            with self.assertRaises(error): TaskRunner(registry,status_observer=self.status).run(self.task)
            self.assertIsNone(self.data()["current"])
    def test_runner_notification_visible_until_execution_finished(self):
        registry=Mock(); registry.resolve.return_value.execute.return_value=TaskExecutionResult("task","SUCCESS")
        service=Mock()
        def notify(result):
            data=self.data(); self.assertEqual(data["current"]["phase"],"lifecycle notification"); self.assertEqual(data["last_result"]["status"],"SUCCESS")
        service.notify.side_effect=notify
        TaskRunner(registry,service,self.status).run(self.task); self.assertIsNone(self.data()["current"])
    def test_engine_publication_deletion_failures_do_not_break_cleanup(self):
        manager=ProcessManager(self.root/"engine"); manager.runtime_dir.mkdir(); self.addCleanup(manager.release)
        worker=Mock(); worker.is_running.return_value=False
        with patch.object(main,"JobScheduler",return_value=worker),patch.object(manager,"stop_requested",return_value=True),patch.object(main,"logger") as logger,contextlib.redirect_stdout(io.StringIO()):
            engine=main.Engine(WorkflowConfiguration(EMAIL,(self.task,)),manager)
            with patch.object(engine.runtime_status,"publish",side_effect=RuntimeError("PRIVATE ERROR")),patch.object(engine.runtime_status,"cleanup",side_effect=OSError("PRIVATE ERROR")):
                self.assertEqual(engine.start(),0)
        self.assertFalse(manager.owns_instance); self.assertNotIn("PRIVATE ERROR",str(logger.mock_calls))
    def test_engine_worker_sampling_and_owned_cleanup_before_release(self):
        directory=self.root/"engine"; directory.mkdir(); manager=ProcessManager(directory); self.addCleanup(manager.release)
        workers=[Mock(),Mock()]
        for worker in workers: worker.is_running.return_value=True
        report=TaskDefinition("report","report","folder_report",True,DailyTrigger(12,35),{"path":self.root})
        with patch.object(main,"FileMonitor",return_value=workers[0]),patch.object(main,"JobScheduler",return_value=workers[1]): engine=main.Engine(WorkflowConfiguration(EMAIL,(report,)),manager)
        manager.acquire(); engine.runtime_status.bind(manager); engine._status_lifecycle="RUNNING"; engine._publish_runtime_status()
        data=read_runtime_snapshot(engine.runtime_status.path,{"pid":os.getpid(),"instance_id":manager.instance_id}).snapshot
        self.assertEqual(data["scheduler"],"alive"); self.assertEqual(data["monitors"],[{"label":"monitor-1","state":"alive"}])
        engine.runtime_status.cleanup(manager); self.assertFalse(engine.runtime_status.path.exists()); self.assertTrue(manager.owns_instance)

    def test_controlled_subprocess_status_observes_current_engine(self):
        from test_cli import CHILD
        directory=self.root/"child"; directory.mkdir(); (directory/"invalid-settings.json").write_text("broken JSON")
        manager=ProcessManager(directory)
        arguments=[sys.executable,"-B","-c",CHILD,str(PROJECT_ROOT),str(directory),"start","normal"]
        process=subprocess.Popen(arguments,cwd=directory,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            deadline=time.monotonic()+8
            while manager.inspect_status().state!="RUNNING":
                if process.poll() is not None:
                    stdout,stderr=process.communicate(); self.fail(stdout+stderr)
                self.assertLess(time.monotonic(),deadline); time.sleep(.02)
            status=subprocess.run([sys.executable,"-B","-c",CHILD,str(PROJECT_ROOT),str(directory),"status","normal"],cwd=PROJECT_ROOT.parent,capture_output=True,text=True,timeout=8)
            self.assertEqual(status.returncode,0,status.stderr); self.assertIn("Tasks loaded: 1 enabled",status.stdout)
            self.assertIn("Scheduler: stopped",status.stdout); self.assertIn("unavailable or invalid",status.stdout)
            self.assertEqual(manager.request_stop(timeout=4).state,"STOPPED")
            stdout,stderr=process.communicate(timeout=5); self.assertEqual(process.returncode,0,stderr)
            self.assertFalse((directory/"engine.status.json").exists())
        finally:
            if process.poll() is None: manager.request_stop(timeout=3)
            process.communicate(timeout=20)  # Child cooperatively exits on its own deadline.

    def test_controlled_crash_snapshot_ignored_and_replaced(self):
        from test_cli import CHILD
        directory=self.root/"crash"; directory.mkdir(); (directory/"invalid-settings.json").write_text("broken JSON")
        result=subprocess.run([sys.executable,"-B","-c",CHILD,str(PROJECT_ROOT),str(directory),"start","crash"],cwd=directory,capture_output=True,text=True,timeout=8)
        self.assertEqual(result.returncode,7); path=directory/"engine.status.json"; self.assertTrue(path.exists())
        manager=ProcessManager(directory)
        with contextlib.redirect_stdout(io.StringIO()) as output: self.assertEqual(main.run_cli(["status"],manager),0)
        self.assertIn("STOPPED",output.getvalue()); self.assertNotIn("Tasks loaded:",output.getvalue()); self.assertTrue(path.exists())
        manager.acquire()
        try:
            status=RuntimeStatus((self.task,),directory); status.bind(manager); status.publish(manager,"STARTING")
            self.assertEqual(read_runtime_snapshot(path,{"pid":os.getpid(),"instance_id":manager.instance_id}).snapshot["instance_id"],manager.instance_id)
            status.cleanup(manager)
        finally: manager.release()

    def test_subprocess_read_replace_contention_stress(self):
        self.status.publish(self.manager,"RUNNING")
        code = r"""
import sys,time,json,socket
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
from utils.process_manager import ProcessManager
from utils.runtime_status import read_runtime_snapshot
root=Path(sys.argv[2]); manager=ProcessManager(root); metadata=manager.inspect_status().metadata
print('READY',flush=True)
count=0; errors=[]; deadline=time.monotonic()+2
with patch('smtplib.SMTP',side_effect=AssertionError('SMTP forbidden')),patch('os.kill',side_effect=AssertionError('Signals forbidden')),patch.object(socket.socket,'connect',side_effect=AssertionError('Network forbidden')):
    while time.monotonic()<deadline:
        result=read_runtime_snapshot(root/'engine.status.json',metadata)
        if result.snapshot is None and len(errors)<5: errors.append(result.reason)
        else: count+=1
print(json.dumps({'count':count,'errors':errors}),flush=True)
"""
        readers=[]
        try:
            for _ in range(2):
                process=subprocess.Popen([sys.executable,"-B","-c",code,str(PROJECT_ROOT),str(self.root)],cwd=PROJECT_ROOT.parent,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                readers.append(process); self.assertEqual(process.stdout.readline().strip(),"READY")
            for _ in range(150): self.status.publish(self.manager,"RUNNING")
            for process in readers:
                stdout,stderr=process.communicate(timeout=5); self.assertEqual(process.returncode,0,stderr)
                data=json.loads(stdout); self.assertGreater(data["count"],0); self.assertEqual(data["errors"],[])
        finally:
            for process in readers: process.communicate(timeout=5)

    def test_blocked_notification_runtime_visible_through_stopping_and_cleanup(self):
        directory=self.root/"blocked"; directory.mkdir(); manager=ProcessManager(directory); self.addCleanup(manager.release)
        config=WorkflowConfiguration(EMAIL,(self.task,),notifications=NotificationSettings(True))
        engine=main.Engine(config,manager); engine.WORKER_JOIN_TIMEOUT=.01
        engine.job_scheduler._wall_clock=lambda:datetime(2026,1,1,12,35)
        entered=threading.Event(); release=threading.Event(); waiting=threading.Event(); results=[]
        def notification(*args): entered.set(); release.wait(5); return True
        def read_current():
            state=manager.inspect_status()
            return read_runtime_snapshot(engine.runtime_status.path,state.metadata).snapshot if state.metadata else None
        with patch("tasks.email_action.send_email",return_value=True),patch("tasks.notifications.send_email",side_effect=notification),patch.object(main,"logger") as logger,contextlib.redirect_stdout(io.StringIO()):
            logger.warning.side_effect=lambda *args:waiting.set()
            thread=threading.Thread(target=lambda:results.append(engine.start())); thread.start()
            try:
                self.assertTrue(entered.wait(2)); deadline=time.monotonic()+2
                while True:
                    data=read_current()
                    if data is not None and data["current"] is not None and data["current"]["phase"]=="lifecycle notification": break
                    self.assertLess(time.monotonic(),deadline); time.sleep(.01)
                self.assertEqual(data["last_result"]["status"],"SUCCESS"); self.assertEqual(data["scheduler"],"alive")
                controller=ProcessManager(directory); controller.request_stop(timeout=0); self.assertTrue(waiting.wait(2))
                data=read_current(); self.assertIsNotNone(data); self.assertEqual(data["lifecycle"],"STOPPING")
                self.assertEqual(data["current"]["phase"],"lifecycle notification"); self.assertTrue(manager.owns_instance)
                with self.assertRaises(AlreadyRunning): controller.acquire()
                release.set(); thread.join(3); self.assertFalse(thread.is_alive()); self.assertEqual(results,[0])
                self.assertFalse(engine.runtime_status.path.exists()); self.assertFalse(manager.owns_instance)
            finally:
                release.set(); engine._running=False; engine._shutdown_event.set(); thread.join(5)

    def test_engine_duplicate_start_preserves_existing_status(self):
        self.status.publish(self.manager,"RUNNING"); before=self.status.path.read_bytes()
        engine=main.Engine(WorkflowConfiguration(EMAIL,(self.task,)),ProcessManager(self.root))
        with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(engine.start(),1)
        self.assertEqual(self.status.path.read_bytes(),before)

    def test_engine_startup_failure_removes_owned_status(self):
        directory=self.root/"startup"; directory.mkdir(); manager=ProcessManager(directory); self.addCleanup(manager.release)
        scheduler=Mock(); scheduler.start.side_effect=RuntimeError("controlled start failure"); scheduler.is_running.return_value=False
        with patch.object(main,"JobScheduler",return_value=scheduler),patch.object(main,"logger"),contextlib.redirect_stdout(io.StringIO()):
            engine=main.Engine(WorkflowConfiguration(EMAIL,(self.task,)),manager); self.assertEqual(engine.start(),1)
        self.assertFalse(engine.runtime_status.path.exists()); self.assertFalse(manager.owns_instance)


if __name__ == "__main__": unittest.main()
