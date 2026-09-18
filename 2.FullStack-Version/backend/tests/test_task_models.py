import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from tasks.models import (
    DailyTrigger, FileEventTrigger, IntervalTrigger, TaskDefinition,
    WorkflowConfiguration, TaskExecutionResult,
)


class TaskModelTests(unittest.TestCase):
    def task(self, **changes):
        values = dict(id="report", name="Friendly report", type="folder_report",
                      enabled=True, trigger=DailyTrigger(12, 35),
                      parameters={"path": Path("watched_folder")})
        values.update(changes)
        return TaskDefinition(**values)

    def test_daily_trigger_contains_data_only(self):
        trigger = DailyTrigger(0, 0)
        self.assertEqual((trigger.type, trigger.hour, trigger.minute), ("daily", 0, 0))
        self.assertFalse(hasattr(trigger, "run"))

    def test_interval_trigger_contains_definition_without_execution(self):
        trigger = IntervalTrigger(15)
        self.assertEqual((trigger.type, trigger.every_minutes), ("interval", 15))
        self.assertFalse(hasattr(trigger, "start"))

    def test_file_event_trigger_copies_event_sequence_and_path(self):
        events = ["new", "modified"]
        trigger = FileEventTrigger("watched_folder", events)
        events.append("deleted")
        self.assertEqual(trigger.path, Path("watched_folder"))
        self.assertEqual(trigger.events, ("new", "modified"))
        self.assertEqual(trigger.type, "file_event")

    def test_task_identity_is_independent_of_name(self):
        first = self.task()
        renamed = self.task(name="Another friendly name")
        self.assertEqual(first.id, renamed.id)
        self.assertNotEqual(first.name, renamed.name)

    def test_definition_fields_are_read_only(self):
        task = self.task()
        with self.assertRaises(FrozenInstanceError):
            task.enabled = False
        with self.assertRaises(FrozenInstanceError):
            task.trigger.hour = 1

    def test_parameters_are_copied_and_read_only(self):
        parameters = {"subject": "Report", "body": "Original body"}
        task = self.task(type="email", parameters=parameters)
        parameters["body"] = "Changed input"
        self.assertEqual(task.parameters["body"], "Original body")
        with self.assertRaises(TypeError):
            task.parameters["body"] = "Cannot modify validated data"

    def test_workflow_copies_email_tasks_and_warnings(self):
        email = {"sender": "sender@example.com", "smtp_port": 587}
        tasks = [self.task()]
        warnings = ["Example warning"]
        config = WorkflowConfiguration(email, tasks, warnings)
        email["smtp_port"] = 25
        tasks.clear()
        warnings.clear()
        self.assertEqual(config.schema_version, 2)
        self.assertEqual(config.email["smtp_port"], 587)
        self.assertEqual(config.tasks, (self.task(),))
        self.assertEqual(config.warnings, ("Example warning",))
        with self.assertRaises(TypeError):
            config.email["sender"] = "modified@example.com"

    def test_model_representation_omits_content_and_email_settings(self):
        content = "sensitive-example-content"
        task = self.task(type="email", parameters={"subject": content, "body": content})
        config = WorkflowConfiguration({"sender": content}, [task])
        self.assertNotIn(content, repr(task))
        self.assertNotIn(content, repr(config))

    def test_equivalent_models_compare_equal(self):
        self.assertEqual(WorkflowConfiguration({"smtp_port": 587}, [self.task()]),
                         WorkflowConfiguration({"smtp_port": 587}, (self.task(),)))

    def test_execution_success(self):
        result = TaskExecutionResult("report", "SUCCESS", notification_accepted=True)
        self.assertTrue(result.success)
        self.assertTrue(result.notification_accepted)

    def test_execution_failure(self):
        result = TaskExecutionResult("report", "FAILED", error="Safe failure", notification_accepted=False)
        self.assertFalse(result.success)
        self.assertEqual(result.error, "Safe failure")

    def test_execution_skipped(self):
        result = TaskExecutionResult("report", "SKIPPED")
        self.assertFalse(result.success)
        self.assertIsNone(result.notification_accepted)

    def test_execution_fields_are_frozen(self):
        result = TaskExecutionResult("report", "SUCCESS")
        with self.assertRaises(FrozenInstanceError):
            result.status = "FAILED"

    def test_execution_details_are_copied_and_read_only(self):
        details = {"event_count": 2, "events_restored": True}
        result = TaskExecutionResult("report", "FAILED", details=details)
        details["event_count"] = 10
        self.assertEqual(result.details["event_count"], 2)
        with self.assertRaises(TypeError):
            result.details["event_count"] = 5

    def test_execution_rejects_invalid_status_and_field_types(self):
        for values in ({"task_id": ""}, {"status": "UNKNOWN"}, {"error": 3},
                       {"notification_accepted": 1}, {"details": {"nested": []}},
                       {"details": {1: "value"}}, {"status": "SUCCESS", "error": "failure"},
                       {"status": "SKIPPED", "notification_accepted": False}):
            arguments = {"task_id": "report", "status": "FAILED"}
            arguments.update(values)
            with self.subTest(fields=tuple(values)), self.assertRaises(ValueError):
                TaskExecutionResult(**arguments)

    def test_execution_and_notification_can_have_different_outcomes(self):
        result = TaskExecutionResult("future-action", "SUCCESS", notification_accepted=False)
        self.assertTrue(result.success)
        self.assertFalse(result.notification_accepted)

    def test_execution_repr_omits_error_and_details(self):
        result = TaskExecutionResult("report", "FAILED", error="sensitive-example",
                                     details={"example": "sensitive-example"})
        self.assertNotIn("sensitive-example", repr(result))


if __name__ == "__main__":
    unittest.main()
