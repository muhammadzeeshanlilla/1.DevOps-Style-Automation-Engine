import os
import unittest
from unittest.mock import Mock, patch

from tasks.models import DailyTrigger, TaskDefinition, TaskExecutionResult
from tasks.registry import UnknownTaskType
from tasks.runner import TaskRunner


class TaskRunnerTests(unittest.TestCase):
    def setUp(self):
        self.task = self.definition()
        self.registry = Mock()
        self.handler = self.registry.resolve.return_value
        self.handler.execute.return_value = TaskExecutionResult(self.task.id, "SUCCESS", notification_accepted=True)
        self.runner = TaskRunner(self.registry)
        replacement = patch("tasks.runner.logger")
        self.logger = replacement.start()
        self.addCleanup(replacement.stop)
        for replacement in (patch("smtplib.SMTP", side_effect=AssertionError("Real SMTP forbidden")),
                            patch("threading.Thread.start", side_effect=AssertionError("Workers forbidden"))):
            replacement.start()
            self.addCleanup(replacement.stop)

    def definition(self, enabled=True, name="Friendly task"):
        return TaskDefinition("email-task", name, "email", enabled, DailyTrigger(12, 35),
                              {"subject": "Subject", "body": "private-body-not-for-logs"})

    def messages(self):
        return "\n".join(call.args[0] % call.args[1:] for call in self.logger.mock_calls if call.args)

    def test_disabled_task_skips_lookup_and_handler(self):
        result = self.runner.run(self.definition(enabled=False))
        self.assertEqual(result.status, "SKIPPED")
        self.registry.resolve.assert_not_called()
        self.assertIn("Task skipped", self.messages())
        self.assertNotIn("Task started", self.messages())

    def test_start_and_completion_logs_include_identity(self):
        observer = Mock()
        self.runner.status_observer = observer
        result = self.runner.run(self.task)
        self.assertTrue(result.success)
        self.assertIn("Task started", self.messages())
        self.assertIn("Task completed", self.messages())
        self.assertIn(self.task.id, self.messages())
        self.assertIn(self.task.name, self.messages())
        self.assertNotIn(self.task.parameters["body"], self.messages())
        self.registry.resolve.assert_called_once_with("email")
        self.handler.execute.assert_called_once_with(self.task)
        observer.execution_started.assert_called_once_with(self.task, None)
        observer.result_finalized.assert_called_once_with(result)
        observer.execution_finished.assert_called_once_with()

    def test_handler_failure_is_preserved_and_logged(self):
        failure = TaskExecutionResult(self.task.id, "FAILED", error="Safe failure", notification_accepted=False)
        self.handler.execute.return_value = failure
        self.assertIs(self.runner.run(self.task), failure)
        self.assertIn("Task failed", self.messages())

    def test_unknown_type_is_structured_failure(self):
        self.registry.resolve.side_effect = UnknownTaskType("sensitive-example")
        result = self.runner.run(self.task)
        self.assertEqual(result.status, "FAILED")
        self.handler.execute.assert_not_called()
        self.assertNotIn("sensitive-example", result.error)

    def test_runtime_exceptions_become_safe_structured_failures(self):
        for error in (OSError("sensitive-example"), RuntimeError("sensitive-example")):
            self.handler.execute.side_effect = error
            result = self.runner.run(self.task)
            self.assertEqual(result.status, "FAILED")
            self.assertIn(type(error).__name__, result.error)
            self.assertNotIn("sensitive-example", result.error)
            self.assertNotIn("sensitive-example", self.messages())

    def test_invalid_handler_results_are_failures(self):
        for result in (None, True, {}, "success", TaskExecutionResult("wrong-task", "SUCCESS"),
                       TaskExecutionResult(self.task.id, "SKIPPED")):
            self.handler.execute.return_value = result
            self.assertEqual(self.runner.run(self.task).status, "FAILED")

    def test_runner_is_usable_after_previous_failure(self):
        self.handler.execute.side_effect = [RuntimeError("failed"), TaskExecutionResult(self.task.id, "SUCCESS")]
        self.assertFalse(self.runner.run(self.task).success)
        self.assertTrue(self.runner.run(self.task).success)

    def test_keyboard_interrupt_and_system_exit_propagate(self):
        for error in (KeyboardInterrupt(), SystemExit(2)):
            self.handler.execute.side_effect = error
            with self.assertRaises(type(error)):
                self.runner.run(self.task)

    def test_lookup_interrupts_also_propagate(self):
        self.registry.resolve.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.runner.run(self.task)

    def test_secret_safe_labels_and_failure_logging(self):
        password = "dummy-password-not-real"
        task = self.definition(name="Friendly\nInjected " + password)
        self.handler.execute.side_effect = RuntimeError(password + " private-body-not-for-logs")
        with patch.dict(os.environ, {"SMTP_PASSWORD": password}):
            result = self.runner.run(task)
        self.assertNotIn(password, self.messages())
        self.assertNotIn(password, result.error)
        self.assertNotIn("private-body-not-for-logs", self.messages())
        self.assertIn("\\n", self.messages())
        self.assertNotIn("Friendly\nInjected", self.messages())

    def test_runner_does_not_restore_batches(self):
        self.handler.execute.return_value = TaskExecutionResult(self.task.id, "FAILED")
        self.runner.run(self.task)
        self.assertEqual([call[0] for call in self.handler.mock_calls], ["execute"])

    def test_non_definition_input_is_rejected(self):
        with self.assertRaises(TypeError):
            self.runner.run({"type": "email"})
