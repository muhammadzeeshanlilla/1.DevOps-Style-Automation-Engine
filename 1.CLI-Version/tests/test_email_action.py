import os
import ssl
import unittest
from types import MappingProxyType
from unittest.mock import patch

from tasks.email_action import EmailHandler
from tasks.models import DailyTrigger, TaskDefinition


class EmailActionTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"sender": "sender@example.com", "receiver": "receiver@example.com",
                         "smtp_server": "smtp.example.com", "smtp_port": 587}
        self.handler = EmailHandler(MappingProxyType(self.settings))
        self.task = TaskDefinition("email-task", "Configured email", "email", True, DailyTrigger(12, 35),
                                   {"subject": "Configured subject", "body": "Configured\nbody"})
        for replacement in (patch("smtplib.SMTP", side_effect=AssertionError("Real SMTP forbidden")),
                            patch("threading.Thread.start", side_effect=AssertionError("Workers forbidden"))):
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_uses_configured_content_and_adapts_read_only_settings(self):
        with patch("tasks.email_action.send_email", return_value=True) as sender:
            result = self.handler.execute(self.task)
        sender.assert_called_once_with({"email": self.settings}, "Configured subject", "Configured\nbody")
        self.assertEqual(result.task_id, self.task.id)
        self.assertEqual(result.status, "SUCCESS")
        self.assertTrue(result.notification_accepted)
        self.assertIsNone(result.error)

    def test_false_result_is_failure_without_retry(self):
        with patch("tasks.email_action.send_email", return_value=False) as sender:
            result = self.handler.execute(self.task)
        sender.assert_called_once()
        self.assertEqual(result.status, "FAILED")
        self.assertFalse(result.notification_accepted)

    def test_only_explicit_true_confirms_acceptance(self):
        for value in (None, 1, "yes", {}, False):
            with patch("tasks.email_action.send_email", return_value=value):
                self.assertFalse(self.handler.execute(self.task).success)

    def test_settings_are_snapshotted(self):
        self.settings["sender"] = "changed@example.com"
        with patch("tasks.email_action.send_email", return_value=True) as sender:
            self.handler.execute(self.task)
        self.assertEqual(sender.call_args.args[0]["email"]["sender"], "sender@example.com")

    def test_unexpected_send_exception_reaches_runner_boundary(self):
        with patch("tasks.email_action.send_email", side_effect=RuntimeError("dummy error")):
            with self.assertRaises(RuntimeError):
                self.handler.execute(self.task)

    def test_secure_existing_sender_is_reused_with_mocked_smtp(self):
        with patch.dict(os.environ, {"SMTP_PASSWORD": "dummy-test-only"}), patch("tasks.email_task.smtplib.SMTP") as smtp, patch("tasks.email_task.logger"):
            client = smtp.return_value.__enter__.return_value
            client.sendmail.return_value = {}
            result = self.handler.execute(self.task)
        self.assertTrue(result.success)
        smtp.assert_called_once_with("smtp.example.com", 587, timeout=10)
        context = client.starttls.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_cleanup_failure_after_acceptance_remains_success(self):
        with patch.dict(os.environ, {"SMTP_PASSWORD": "dummy-test-only"}), patch("tasks.email_task.smtplib.SMTP") as smtp, patch("tasks.email_task.logger"):
            smtp.return_value.__enter__.return_value.sendmail.return_value = {}
            smtp.return_value.__exit__.side_effect = TimeoutError("dummy cleanup")
            self.assertTrue(self.handler.execute(self.task).success)

    def test_existing_sender_redacts_dummy_password(self):
        password = "dummy-password-not-a-real-secret"
        with patch.dict(os.environ, {"SMTP_PASSWORD": password}), patch("tasks.email_task.smtplib.SMTP") as smtp, patch("tasks.email_task.logger") as logger:
            smtp.return_value.__enter__.return_value.login.side_effect = RuntimeError("rejected " + password)
            result = self.handler.execute(self.task)
        self.assertFalse(result.success)
        self.assertNotIn(password, str(logger.mock_calls))
        self.assertNotIn(password, result.error)
