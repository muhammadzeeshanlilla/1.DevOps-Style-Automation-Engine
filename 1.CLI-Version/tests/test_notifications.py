import contextlib
import copy
import io
import os
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from dataclasses import FrozenInstanceError
from unittest.mock import Mock, patch
import main
from config.loader import validate_config, ConfigurationError
from tasks.models import NotificationSettings, WorkflowConfiguration, TaskExecutionResult, TaskDefinition, DailyTrigger, IntervalTrigger, FileEventTrigger
from tasks.notifications import LifecycleNotificationService
from tasks.runner import TaskRunner
from tasks.registry import TaskRegistry
from tasks.events import FileEvent
from scheduler.job_scheduler import JobScheduler
from utils.process_manager import ProcessManager, AlreadyRunning

EMAIL={'sender':'sender@example.com','receiver':'receiver@example.com','smtp_server':'smtp.example.com','smtp_port':587}

class NotificationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='notifications-')
        self.addCleanup(temporary.cleanup); self.folder=Path(temporary.name).resolve()
        self.policy=NotificationSettings(True,True,True)
        self.service=LifecycleNotificationService(EMAIL,self.policy)
        self.task=TaskDefinition('task','Sensitive name','email',True,DailyTrigger(12,35),{'subject':'action','body':'PRIVATE BODY'})
        guard=patch('smtplib.SMTP',side_effect=AssertionError('Real SMTP forbidden')); guard.start(); self.addCleanup(guard.stop)
    def result(self,status='SUCCESS'):
        return TaskExecutionResult('task',status,error='PRIVATE ERROR' if status=='FAILED' else None,notification_accepted=True if status=='SUCCESS' else None)
    def config(self,modern=True):
        if modern:
            return {'schema_version':2,'email':EMAIL,'tasks':[{'id':'task','name':'task','type':'email','enabled':True,'trigger':{'type':'daily','hour':12,'minute':35},'parameters':{'subject':'s','body':'b'}}]}
        return {'email':EMAIL,'watch_folder':str(self.folder),'schedule':{'hour':12,'minute':35}}
    def test_success_notification(self):
        with patch('tasks.notifications.send_email',return_value=True) as sender:
            self.assertIs(self.service.notify(self.result()),True)
        self.assertEqual(sender.call_args.args[1],'Automation Engine - Task SUCCESS')
    def test_failure_notification_fixed_safe_summary(self):
        with patch('tasks.notifications.send_email',return_value=True) as sender: self.service.notify(self.result('FAILED'))
        self.assertIn('Task execution failed',sender.call_args.args[2]); self.assertNotIn('PRIVATE ERROR',sender.call_args.args[2])
    def test_skipped_no_notification(self):
        with patch('tasks.notifications.send_email') as sender: self.assertIsNone(self.service.notify(self.result('SKIPPED')))
        sender.assert_not_called()
    def test_disabled_no_notification(self):
        with patch('tasks.notifications.send_email') as sender: self.assertIsNone(LifecycleNotificationService(EMAIL,NotificationSettings()).notify(self.result()))
        sender.assert_not_called()
    def test_success_switch(self):
        with patch('tasks.notifications.send_email') as sender: self.assertIsNone(LifecycleNotificationService(EMAIL,NotificationSettings(True,False,True)).notify(self.result()))
        sender.assert_not_called()
    def test_failure_switch(self):
        with patch('tasks.notifications.send_email') as sender: self.assertIsNone(LifecycleNotificationService(EMAIL,NotificationSettings(True,True,False)).notify(self.result('FAILED')))
        sender.assert_not_called()
    def test_unconfirmed_acceptance(self):
        for outcome in (False,None,1,'yes'):
            with patch('tasks.notifications.send_email',return_value=outcome): self.assertIs(self.service.notify(self.result()),False)
    def test_ordinary_send_exception_safe_log(self):
        with patch('tasks.notifications.send_email',side_effect=RuntimeError('PRIVATE ERROR')),patch('tasks.notifications.logger') as logger:
            self.assertIs(self.service.notify(self.result()),False)
        self.assertNotIn('PRIVATE ERROR',str(logger.mock_calls)); logger.error.assert_called_once()
    def test_service_interrupts_propagate(self):
        for error in (KeyboardInterrupt,SystemExit):
            with patch('tasks.notifications.send_email',side_effect=error):
                with self.assertRaises(error): self.service.notify(self.result())
    def test_timezone_timestamp(self):
        with patch('tasks.notifications.send_email',return_value=True) as sender: self.service.notify(self.result())
        timestamp=sender.call_args.args[2].split('Completed at: ')[1].splitlines()[0]
        self.assertIsNotNone(datetime.fromisoformat(timestamp).utcoffset())
    def test_password_redacted_from_content(self):
        with patch.dict(os.environ,{'SMTP_PASSWORD':'task'}),patch('tasks.notifications.send_email',return_value=True) as sender: self.service.notify(self.result())
        self.assertNotIn('task',sender.call_args.args[1]+sender.call_args.args[2])
    def test_forged_identity_not_leaked(self):
        result=TaskExecutionResult('SECRET\n/path','FAILED',error='PRIVATE BODY')
        with patch('tasks.notifications.send_email',return_value=True) as sender: self.service.notify(result)
        body=sender.call_args.args[2]; self.assertNotIn('SECRET',body); self.assertNotIn('/path',body); self.assertNotIn('PRIVATE BODY',body)
    def test_no_configuration_body_or_event_path_leak(self):
        event=FileEvent('new',self.folder,self.folder/'PRIVATE_PATH',datetime.now())
        runner=TaskRunner(TaskRegistry(EMAIL,{}),self.service)
        with patch('tasks.email_action.send_email',return_value=True),patch('tasks.notifications.send_email',return_value=True) as sender: runner.run(self.task,context=event)
        body=sender.call_args.args[2]
        for private in ('PRIVATE BODY','Sensitive name','PRIVATE_PATH','smtp.example.com','receiver@example.com'): self.assertNotIn(private,body)
    def test_no_recursive_notification(self):
        runner=TaskRunner(TaskRegistry(EMAIL,{}),self.service)
        with patch('tasks.email_action.send_email',return_value=True) as action,patch('tasks.notifications.send_email',return_value=True) as notification: runner.run(self.task)
        action.assert_called_once(); notification.assert_called_once()
    def test_runner_preserves_result_identity_and_acceptance(self):
        result=self.result(); registry=Mock(); registry.resolve.return_value.execute.return_value=result
        service=Mock(); service.notify.return_value=False
        self.assertIs(TaskRunner(registry,service).run(self.task),result); self.assertTrue(result.notification_accepted)
    def test_notification_exception_preserves_success(self):
        service=Mock(); service.notify.side_effect=RuntimeError('PRIVATE ERROR')
        with patch('tasks.email_action.send_email',return_value=True),patch('tasks.runner.logger') as logger:
            result=TaskRunner(TaskRegistry(EMAIL,{}),service).run(self.task)
        self.assertTrue(result.success); self.assertTrue(result.notification_accepted); self.assertNotIn('PRIVATE ERROR',str(logger.mock_calls))
    def test_runner_notification_interrupts_propagate(self):
        for error in (KeyboardInterrupt,SystemExit):
            service=Mock(); service.notify.side_effect=error
            with patch('tasks.email_action.send_email',return_value=True):
                with self.assertRaises(error): TaskRunner(TaskRegistry(EMAIL,{}),service).run(self.task)
    def test_outcome_log_precedes_notification(self):
        order=[]; service=Mock(); service.notify.side_effect=lambda result: order.append('notify')
        with patch('tasks.email_action.send_email',return_value=True),patch('tasks.runner.logger') as logger:
            logger.info.side_effect=lambda message,*args: order.append(message)
            TaskRunner(TaskRegistry(EMAIL,{}),service).run(self.task)
        self.assertEqual(order[-2:],['Task completed | %s','notify'])
    def test_disabled_task_never_calls_service(self):
        task=TaskDefinition('task','task','email',False,DailyTrigger(0,0),{})
        service=Mock(); result=TaskRunner(Mock(),service).run(task)
        self.assertEqual(result.status,'SKIPPED'); service.notify.assert_not_called()
    def test_missing_section_legacy_and_modern(self):
        for modern in (False,True): self.assertEqual(validate_config(self.config(modern)).notifications,NotificationSettings())
    def test_valid_section_both_formats(self):
        for modern in (False,True):
            data=self.config(modern); data['notifications']={'enabled':True,'notify_on_success':False,'notify_on_failure':True}
            self.assertEqual(validate_config(data).notifications,NotificationSettings(True,False,True))
    def test_invalid_boolean_values_all_fields(self):
        for field in ('enabled','notify_on_success','notify_on_failure'):
            for value in (1,0,'true',None,[],{}):
                data=self.config(); data['notifications']={'enabled':True,'notify_on_success':True,'notify_on_failure':True}; data['notifications'][field]=value
                with self.assertRaises(ConfigurationError): validate_config(data)
    def test_unknown_secret_fields_rejected_without_values(self):
        for field in ('password','secret','token','unexpected'):
            data=self.config(); data['notifications']={'enabled':True,'notify_on_success':True,'notify_on_failure':True,field:'PRIVATE VALUE'}
            with self.assertRaises(ConfigurationError) as raised: validate_config(data)
            self.assertNotIn('PRIVATE VALUE',str(raised.exception))
    def test_missing_policy_fields_rejected(self):
        for field in ('enabled','notify_on_success','notify_on_failure'):
            data=self.config(); data['notifications']={'enabled':True,'notify_on_success':True,'notify_on_failure':True}; del data['notifications'][field]
            with self.assertRaises(ConfigurationError): validate_config(data)
    def test_invalid_section_type(self):
        for value in (None,True,[],1,'enabled'):
            data=self.config(); data['notifications']=value
            with self.assertRaises(ConfigurationError): validate_config(data)
    def test_settings_frozen(self):
        with self.assertRaises(FrozenInstanceError): self.policy.enabled=False
    def test_model_rejects_non_booleans(self):
        with self.assertRaises(TypeError): NotificationSettings(1)
    def test_environment_secret_not_in_normalized_settings(self):
        with patch.dict(os.environ,{'SMTP_PASSWORD':'PRIVATE PASSWORD'}): config=validate_config(self.config())
        self.assertNotIn('PRIVATE PASSWORD',repr(config)); self.assertNotIn('password',config.email)
    def test_missing_password_fails_without_smtp(self):
        with patch.dict(os.environ,{},clear=True),patch('tasks.email_task.smtplib.SMTP') as smtp,patch('tasks.email_task.logger'):
            self.assertFalse(self.service.notify(self.result()))
        smtp.assert_not_called()
    def test_mocked_secure_smtp_acceptance(self):
        with patch.dict(os.environ,{'SMTP_PASSWORD':'dummy-only'}),patch('tasks.email_task.smtplib.SMTP') as smtp:
            smtp.return_value.__enter__.return_value.sendmail.return_value={}
            self.assertTrue(self.service.notify(self.result()))
        self.assertEqual(smtp.call_args.kwargs['timeout'],10)
        self.assertTrue(smtp.return_value.__enter__.return_value.starttls.call_args.kwargs['context'].check_hostname)
    def test_daily_and_interval_use_same_service(self):
        for trigger in (DailyTrigger(12,35),IntervalTrigger(1)):
            task=TaskDefinition('task','task','email',True,trigger,self.task.parameters); runner=TaskRunner(TaskRegistry(EMAIL,{}),self.service)
            tick=[0.0]; scheduler=JobScheduler((task,),runner,lambda:datetime(2026,1,1,12,35),lambda:tick[0]); scheduler._initialize_schedule(); tick[0]=60
            with patch('tasks.email_action.send_email',return_value=True),patch('tasks.notifications.send_email',return_value=True) as sender: scheduler._run_due_tasks()
            sender.assert_called_once()
    def test_file_event_notification_integrated(self):
        task=TaskDefinition('task','task','email',True,FileEventTrigger(self.folder,('new',)),self.task.parameters)
        config=WorkflowConfiguration(EMAIL,(task,),notifications=self.policy); engine=main.Engine(config)
        engine.event_dispatcher.reopen()
        with patch('tasks.email_action.send_email',return_value=True),patch('tasks.notifications.send_email',return_value=True) as sender: engine.job_scheduler._dispatch_event(FileEvent('new',self.folder,self.folder/'a',datetime.now()))
        sender.assert_called_once(); engine.event_dispatcher.close()
    def test_report_acceptance_and_failure_restoration_before_notification(self):
        task=TaskDefinition('report','report','folder_report',True,DailyTrigger(0,0),{'path':self.folder})
        for accepted in (True,False):
            engine=main.Engine(WorkflowConfiguration(EMAIL,(task,),notifications=self.policy)); monitor=engine.file_monitors[self.folder]; monitor._record_change('A')
            def notify(*args):
                self.assertEqual(monitor.changes,[] if accepted else ['A']); return False
            with patch('tasks.report_task.send_report_email',return_value=accepted),patch('tasks.notifications.send_email',side_effect=notify) as sender,patch.object(monitor,'restore_changes',wraps=monitor.restore_changes) as restore:
                result=engine.task_runner.run(task)
            self.assertEqual(result.status,'SUCCESS' if accepted else 'FAILED'); self.assertIs(result.notification_accepted,accepted)
            self.assertEqual(restore.call_count,0 if accepted else 1); sender.assert_called_once()
    def test_shutdown_waits_for_notification_retaining_ownership(self):
        tasks=(self.task,TaskDefinition('next','next','email',True,DailyTrigger(12,35),self.task.parameters))
        manager=ProcessManager(self.folder); self.addCleanup(manager.release)
        engine=main.Engine(WorkflowConfiguration(EMAIL,tasks,notifications=self.policy),manager); engine.WORKER_JOIN_TIMEOUT=.01
        engine.job_scheduler._wall_clock=lambda:datetime(2026,1,1,12,35)
        entered=threading.Event(); release=threading.Event(); waiting=threading.Event(); results=[]
        def notify(*args): entered.set(); release.wait(5); return True
        with patch('tasks.email_action.send_email',return_value=True) as action,patch('tasks.notifications.send_email',side_effect=notify) as sender,patch.object(main,'logger') as logger,contextlib.redirect_stdout(io.StringIO()):
            logger.warning.side_effect=lambda *args:waiting.set(); thread=threading.Thread(target=lambda:results.append(engine.start())); thread.start()
            try:
                self.assertTrue(entered.wait(2)); controller=ProcessManager(self.folder); controller.request_stop(timeout=0)
                self.assertTrue(waiting.wait(2)); self.assertEqual(controller.inspect_status().state,'STOPPING'); self.assertTrue(manager.owns_instance)
                with self.assertRaises(AlreadyRunning): controller.acquire()
                release.set(); thread.join(3); self.assertFalse(thread.is_alive()); self.assertEqual(results,[0]); self.assertFalse(manager.owns_instance)
                action.assert_called_once(); sender.assert_called_once()
            finally: release.set(); engine._running=False; engine._shutdown_event.set(); thread.join(5)
