import copy
import os
import smtplib
import ssl
import unittest
from unittest.mock import patch

from tasks import email_task


class EmailTaskTests(unittest.TestCase):
    def setUp(self):
        self.config = {"email": {
            "sender": "sender@example.com", "receiver": "receiver@example.com",
            "smtp_server": "smtp.example.com", "smtp_port": 587,
        }}
        self.password = "dummy-test-password-not-a-secret"
        environment = patch.dict(os.environ, {"SMTP_PASSWORD": self.password})
        environment.start()
        self.addCleanup(environment.stop)
        replacement = patch("tasks.email_task.smtplib.SMTP")
        self.constructor = replacement.start()
        self.addCleanup(replacement.stop)
        self.connection = self.constructor.return_value
        self.smtp = self.connection.__enter__.return_value
        self.smtp.sendmail.return_value = {}
        replacement = patch("tasks.email_task.logger")
        self.logger = replacement.start()
        self.addCleanup(replacement.stop)

    def send(self, config=None, subject="Daily report"):
        return email_task.send_email(self.config if config is None else config, subject, "Report body")

    def logs(self):
        return "\n".join(
            str(call.args[0]) % call.args[1:] if len(call.args) > 1 else str(call.args[0])
            for call in self.logger.mock_calls if call.args
        )

    def test_success_returns_true_and_preserves_message(self):
        self.assertIs(self.send(), True)
        sender, receiver, message = self.smtp.sendmail.call_args.args
        self.assertEqual((sender, receiver), ("sender@example.com", "receiver@example.com"))
        self.assertIn("Subject: Daily report", message)
        self.assertIn("Report body", message)
        self.smtp.login.assert_called_once_with(sender, self.password)

    def test_constructor_receives_named_ten_second_timeout(self):
        self.send()
        self.assertEqual(email_task.SMTP_TIMEOUT_SECONDS, 10)
        self.constructor.assert_called_once_with("smtp.example.com", 587, timeout=10)

    def test_starttls_receives_verified_default_context(self):
        with patch("tasks.email_task.ssl.create_default_context", wraps=ssl.create_default_context) as factory:
            self.assertTrue(self.send())
        factory.assert_called_once_with()
        context = self.smtp.starttls.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual([call[0] for call in self.smtp.method_calls], ["starttls", "login", "sendmail"])

    def test_missing_or_empty_password_prevents_connection(self):
        for password in (None, ""):
            with self.subTest(password_is_missing=password is None), patch.dict(os.environ):
                if password is None:
                    os.environ.pop("SMTP_PASSWORD", None)
                else:
                    os.environ["SMTP_PASSWORD"] = password
                self.assertFalse(self.send())
        self.constructor.assert_not_called()

    def test_missing_or_malformed_configuration_prevents_connection(self):
        for config in (None, [], {}, {"email": None}, {"email": []}, {"email": "invalid"}):
            with self.subTest(config_type=type(config).__name__):
                self.assertFalse(email_task.send_email(config, "Report", "Body"))
        self.constructor.assert_not_called()

    def test_missing_email_fields_prevent_connection(self):
        for field in self.config["email"]:
            config = copy.deepcopy(self.config)
            del config["email"][field]
            with self.subTest(field=field):
                self.assertFalse(self.send(config))
        self.constructor.assert_not_called()

    def test_invalid_string_fields_prevent_connection(self):
        for field in ("sender", "receiver", "smtp_server"):
            for value in (None, 123, [], "", "   "):
                config = copy.deepcopy(self.config)
                config["email"][field] = value
                with self.subTest(field=field, value_type=type(value).__name__):
                    self.assertFalse(self.send(config))
        self.constructor.assert_not_called()

    def test_invalid_ports_are_rejected_without_logging_values(self):
        for value in (None, "587", True, False, 0, -1, 65536, 587.0, self.password):
            config = copy.deepcopy(self.config)
            config["email"]["smtp_port"] = value
            with self.subTest(value_type=type(value).__name__):
                self.assertFalse(self.send(config))
        self.constructor.assert_not_called()
        self.assertNotIn(self.password, self.logs())

    def test_header_and_server_line_breaks_prevent_connection(self):
        for field in ("sender", "receiver", "smtp_server"):
            for newline in ("\r", "\n", "\r\n"):
                config = copy.deepcopy(self.config)
                config["email"][field] += newline + "Bcc: injected@example.com"
                with self.subTest(field=field, newline=repr(newline)):
                    self.assertFalse(self.send(config))
        for subject in ("Report\nBcc: injected@example.com", "Report\rInjected", None):
            self.assertFalse(self.send(subject=subject))
        self.constructor.assert_not_called()

    def test_tls_verification_failure_prevents_authentication(self):
        self.smtp.starttls.side_effect = ssl.SSLCertVerificationError("untrusted certificate")
        self.assertFalse(self.send())
        self.smtp.login.assert_not_called()
        self.smtp.sendmail.assert_not_called()

    def test_unsupported_tls_never_falls_back_to_plaintext(self):
        self.smtp.starttls.side_effect = smtplib.SMTPNotSupportedError("STARTTLS unavailable")
        self.assertFalse(self.send())
        self.smtp.login.assert_not_called()
        self.smtp.sendmail.assert_not_called()

    def test_authentication_failure_returns_false(self):
        self.smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"authentication rejected")
        self.assertFalse(self.send())
        self.smtp.sendmail.assert_not_called()

    def test_connection_timeout_returns_false(self):
        self.constructor.side_effect = TimeoutError("simulated connection timeout")
        self.assertFalse(self.send())

    def test_connection_failure_returns_false(self):
        self.constructor.side_effect = ConnectionRefusedError("simulated refused connection")
        self.assertFalse(self.send())

    def test_send_timeout_returns_false(self):
        self.smtp.sendmail.side_effect = TimeoutError("simulated acknowledgement timeout")
        self.assertFalse(self.send())

    def test_recipient_rejection_exception_returns_false(self):
        self.smtp.sendmail.side_effect = smtplib.SMTPRecipientsRefused({"receiver@example.com": (550, b"refused")})
        self.assertFalse(self.send())

    def test_refused_recipient_result_returns_false(self):
        self.smtp.sendmail.return_value = {"receiver@example.com": (550, b"refused")}
        self.assertFalse(self.send())

    def test_unknown_sendmail_result_is_not_confirmed_acceptance(self):
        for result in (None, False, [], ""):
            self.smtp.sendmail.return_value = result
            self.assertIs(self.send(), False)

    def test_password_is_redacted_from_failure_logs(self):
        self.smtp.login.side_effect = RuntimeError("rejected " + self.password)
        self.assertFalse(self.send())
        self.assertNotIn(self.password, self.logs())
        self.assertIn("[REDACTED]", self.logs())

    def test_accepted_message_remains_successful_after_cleanup_failure(self):
        self.connection.__exit__.side_effect = TimeoutError("cleanup " + self.password)
        self.assertIs(self.send(), True)
        self.logger.warning.assert_called_once()
        self.assertNotIn(self.password, self.logs())
        self.assertIn("after confirmed acceptance", self.logs())

    def test_cleanup_failure_before_acceptance_does_not_claim_success(self):
        self.smtp.sendmail.return_value = {"receiver@example.com": (550, b"refused")}
        self.connection.__exit__.side_effect = TimeoutError("cleanup failed")
        self.assertFalse(self.send())

    def test_original_error_context_is_preserved_and_redacted(self):
        original = RuntimeError("authentication " + self.password)
        cleanup = RuntimeError("cleanup " + self.password)
        cleanup.__context__ = original
        self.smtp.login.side_effect = original
        self.connection.__exit__.side_effect = cleanup
        self.assertFalse(self.send())
        self.assertNotIn(self.password, self.logs())
        self.assertIn("authentication", self.logs())
        self.assertIn("cleanup", self.logs())

    def test_success_log_also_redacts_password(self):
        self.assertTrue(self.send(subject="Report " + self.password))
        self.assertNotIn(self.password, self.logs())

    def test_report_propagates_both_results_and_preserves_text(self):
        changes = ["NEW file detected: a.txt", "MODIFIED file: b.txt"]
        expected = ("The following changes were detected in the watched folder:\n\n"
                    "NEW file detected: a.txt\nMODIFIED file: b.txt\n\n"
                    "This report was generated automatically.")
        for result in (True, False):
            with patch("tasks.email_task.send_email", return_value=result) as sender:
                self.assertIs(email_task.send_report_email(self.config, changes), result)
                sender.assert_called_once_with(self.config, "Automation Engine — Daily Folder Report", expected)

    def test_empty_report_preserves_text_and_result(self):
        with patch("tasks.email_task.send_email", return_value=True) as sender:
            self.assertTrue(email_task.send_report_email(self.config, []))
        sender.assert_called_once_with(self.config, "Automation Engine — Daily Folder Report",
                                       "No changes were detected in the watched folder today.")


if __name__ == "__main__":
    unittest.main()
