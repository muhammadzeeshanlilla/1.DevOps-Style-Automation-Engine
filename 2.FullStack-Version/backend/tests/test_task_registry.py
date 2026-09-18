import unittest
from unittest.mock import patch

from tasks.email_action import EmailHandler
from tasks.registry import TaskRegistry, UnknownTaskType
from tasks.report_task import FolderReportHandler


class TaskRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = TaskRegistry({"sender": "sender@example.com", "receiver": "receiver@example.com",
                                      "smtp_server": "smtp.example.com", "smtp_port": 587}, {})

    def test_resolves_report(self):
        self.assertIsInstance(self.registry.resolve("folder_report"), FolderReportHandler)

    def test_resolves_email(self):
        self.assertIsInstance(self.registry.resolve("email"), EmailHandler)

    def test_unknown_types_are_rejected_without_echoing_values(self):
        for value in ("unknown-sensitive-value", "os.system", "subprocess.run", None, []):
            with self.assertRaises(UnknownTaskType) as caught:
                self.registry.resolve(value)
            self.assertNotIn("unknown-sensitive-value", str(caught.exception))

    def test_no_dynamic_registration_and_read_only_mapping(self):
        self.assertFalse(hasattr(self.registry, "register"))
        self.assertEqual(set(self.registry._handlers), {"email", "folder_report"})
        with self.assertRaises(TypeError):
            self.registry._handlers["shell"] = object()

    def test_dependencies_and_registry_are_instance_specific(self):
        another = TaskRegistry({}, {})
        self.assertIsNot(self.registry.resolve("email"), another.resolve("email"))
        self.assertIs(self.registry.resolve("email"), self.registry.resolve("email"))

    def test_lookup_has_no_dynamic_import_or_execution(self):
        with patch("os.system", side_effect=AssertionError("Shell forbidden")), patch("smtplib.SMTP", side_effect=AssertionError("Real SMTP forbidden")), patch("threading.Thread.start", side_effect=AssertionError("Workers forbidden")), patch("importlib.import_module", side_effect=AssertionError("Dynamic import forbidden")):
            self.registry.resolve("email")
            with self.assertRaises(UnknownTaskType):
                self.registry.resolve("os.system")
