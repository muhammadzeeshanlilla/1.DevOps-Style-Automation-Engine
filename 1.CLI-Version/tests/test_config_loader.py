import contextlib
import copy
import io
import json
import os
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

from config.loader import ConfigurationError, load_config, validate_config
from tasks.models import DailyTrigger, FileEventTrigger, IntervalTrigger, WorkflowConfiguration
from utils.paths import CONFIG_PATH, PROJECT_ROOT, resolve_project_path


class ConfigurationLoaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="task-config-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.folder = self.root / "watched"
        self.folder.mkdir()
        self.email = {"sender": "sender@example.com", "receiver": "receiver@example.com",
                      "smtp_server": "smtp.example.com", "smtp_port": 587}
        self.legacy = {"watch_folder": str(self.folder), "email": self.email,
                       "schedule": {"hour": 12, "minute": 35}}
        self.task = {"id": "daily-folder-report", "name": "Daily folder report",
                     "type": "folder_report", "enabled": True,
                     "trigger": {"type": "daily", "hour": 12, "minute": 35},
                     "parameters": {"path": str(self.folder)}}
        self.new = {"schema_version": 2, "email": self.email, "tasks": [self.task]}
        for replacement in (
            patch.dict(os.environ, {"SMTP_PASSWORD": "dummy-test-only"}),
            patch("smtplib.SMTP", side_effect=AssertionError("SMTP is forbidden")),
            patch("threading.Thread.start", side_effect=AssertionError("Workers are forbidden")),
            patch("os.kill", side_effect=AssertionError("Process signals are forbidden")),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def email_task(self, identity="email-task", trigger=None, enabled=True):
        return {"id": identity, "name": "Configured email", "type": "email", "enabled": enabled,
                "trigger": trigger or {"type": "interval", "every_minutes": 15},
                "parameters": {"subject": "Configured subject", "body": "Configured body"}}

    def invalid(self, config, field):
        with self.assertRaises(ConfigurationError) as caught:
            validate_config(config)
        self.assertIn(field, str(caught.exception))

    def write(self, text, encoding="utf-8"):
        path = self.root / "settings.json"
        path.write_text(text, encoding=encoding)
        return path

    def test_valid_legacy_normalizes_to_daily_report(self):
        config = validate_config(self.legacy)
        self.assertIsInstance(config, WorkflowConfiguration)
        self.assertEqual(config.schema_version, 2)
        self.assertEqual(config.email, self.email)
        self.assertEqual(len(config.tasks), 1)
        task = config.tasks[0]
        self.assertEqual((task.id, task.name, task.type, task.enabled),
                         ("daily-folder-report", "Daily folder report", "folder_report", True))
        self.assertEqual(task.trigger, DailyTrigger(12, 35))
        self.assertEqual(task.parameters["path"], self.folder)

    def test_valid_new_configuration(self):
        self.assertEqual(validate_config(self.new).tasks[0].parameters["path"], self.folder)

    def test_equivalent_legacy_and_new_normalize_identically(self):
        self.assertEqual(validate_config(self.legacy), validate_config(self.new))

    def test_normalization_does_not_mutate_input(self):
        for data in (self.legacy, self.new):
            original = copy.deepcopy(data)
            validate_config(data)
            self.assertEqual(data, original)

    def test_normalized_configuration_is_independent_of_input_mutation(self):
        config = validate_config(self.new)
        self.email["sender"] = "changed@example.com"
        self.task["name"] = "Changed name"
        self.task["parameters"]["path"] = "changed"
        self.assertEqual(config.email["sender"], "sender@example.com")
        self.assertEqual(config.tasks[0].name, "Daily folder report")
        self.assertEqual(config.tasks[0].parameters["path"], self.folder)

    def test_multiple_valid_tasks_and_shared_folder_file_trigger(self):
        self.new["tasks"].extend([
            self.email_task(),
            self.email_task("file-email", {"type": "file_event", "path": str(self.folder),
                                          "events": ["new", "modified", "deleted"]}),
        ])
        config = validate_config(self.new)
        self.assertEqual(len(config.tasks), 3)
        self.assertIsInstance(config.tasks[1].trigger, IntervalTrigger)
        self.assertIsInstance(config.tasks[2].trigger, FileEventTrigger)

    def test_disabled_missing_report_folder_is_allowed(self):
        self.task["enabled"] = False
        self.task["parameters"]["path"] = str(self.root / "missing")
        self.assertFalse(validate_config(self.new).tasks[0].enabled)

    def test_disabled_missing_file_event_folder_is_allowed(self):
        self.new["tasks"] = [self.email_task(trigger={"type": "file_event", "path": str(self.root / "missing"),
                                                     "events": ["new"]}, enabled=False)]
        self.assertFalse(validate_config(self.new).tasks[0].enabled)

    def test_all_disabled_configuration_returns_idle_warning(self):
        self.task["enabled"] = False
        config = validate_config(self.new)
        self.assertIn("idle engine", config.warnings[0])

    def test_empty_task_list_is_valid_idle_configuration(self):
        self.new["tasks"] = []
        config = validate_config(self.new)
        self.assertEqual(config.tasks, ())
        self.assertIn("No enabled tasks", config.warnings[0])

    def test_disabled_tasks_still_require_valid_structure(self):
        self.task["enabled"] = False
        self.task["trigger"]["hour"] = 24
        self.invalid(self.new, "tasks[0].trigger.hour")

    def test_duplicate_ids_including_disabled_tasks_are_rejected(self):
        duplicate = copy.deepcopy(self.task)
        duplicate["enabled"] = False
        self.new["tasks"].append(duplicate)
        self.invalid(self.new, "tasks[1].id")

    def test_valid_safe_ids(self):
        for identity in ("a", "task-1", "task_2", "123", "a-b_c"):
            with self.subTest(identity=identity):
                self.task["id"] = identity
                self.assertEqual(validate_config(self.new).tasks[0].id, identity)

    def test_invalid_task_ids(self):
        for identity in (None, 1, True, "", " ", "Uppercase", "has space", "a.b", "../escape", "a\n", "é"):
            with self.subTest(value_type=type(identity).__name__):
                self.task["id"] = identity
                self.invalid(self.new, "tasks[0].id")

    def test_duplicate_names_are_allowed_and_do_not_change_ids(self):
        second = self.email_task()
        second["name"] = self.task["name"]
        self.new["tasks"].append(second)
        config = validate_config(self.new)
        self.assertEqual(config.tasks[0].name, config.tasks[1].name)
        self.assertNotEqual(config.tasks[0].id, config.tasks[1].id)

    def test_unknown_task_type(self):
        self.task["type"] = "shell"
        self.invalid(self.new, "tasks[0].type")

    def test_unknown_trigger_type(self):
        self.task["trigger"]["type"] = "cron"
        self.invalid(self.new, "tasks[0].trigger.type")

    def test_folder_report_file_event_combination_is_rejected(self):
        self.task["trigger"] = {"type": "file_event", "path": str(self.folder), "events": ["new"]}
        self.invalid(self.new, "tasks[0].trigger.type")

    def test_folder_report_interval_is_valid(self):
        self.task["trigger"] = {"type": "interval", "every_minutes": 5}
        self.assertEqual(validate_config(self.new).tasks[0].trigger, IntervalTrigger(5))

    def test_email_daily_is_valid(self):
        self.new["tasks"] = [self.email_task(trigger={"type": "daily", "hour": 0, "minute": 0})]
        self.assertEqual(validate_config(self.new).tasks[0].trigger, DailyTrigger(0, 0))

    def test_missing_common_task_fields(self):
        for field in self.task:
            config = copy.deepcopy(self.new)
            del config["tasks"][0][field]
            with self.subTest(field=field):
                self.invalid(config, f"tasks[0].{field}")

    def test_unexpected_common_task_field(self):
        self.task["command"] = "do not execute"
        self.invalid(self.new, "tasks[0]")

    def test_wrong_common_task_field_types(self):
        for field, values in {
            "name": [None, 1, "", " "], "type": [None, [], 1],
            "enabled": [0, 1, "true", None], "trigger": [None, [], "daily"],
            "parameters": [None, [], "parameters"],
        }.items():
            for value in values:
                config = copy.deepcopy(self.new)
                config["tasks"][0][field] = value
                with self.subTest(field=field, value_type=type(value).__name__):
                    self.invalid(config, f"tasks[0].{field}")

    def test_tasks_must_be_a_list_of_objects(self):
        for value in (None, {}, "tasks", [None], [[]]):
            self.new["tasks"] = value
            with self.subTest(value_type=type(value).__name__):
                self.invalid(self.new, "tasks")

    def test_daily_boundaries(self):
        for hour, minute in ((0, 0), (23, 59)):
            self.task["trigger"] = {"type": "daily", "hour": hour, "minute": minute}
            self.assertEqual(validate_config(self.new).tasks[0].trigger, DailyTrigger(hour, minute))

    def test_invalid_daily_ranges_and_integer_types(self):
        for field, values in {"hour": [-1, 24, True, False, "12", 12.0, None],
                              "minute": [-1, 60, True, False, "35", 35.0, None]}.items():
            for value in values:
                config = copy.deepcopy(self.new)
                config["tasks"][0]["trigger"][field] = value
                with self.subTest(field=field, value_type=type(value).__name__):
                    self.invalid(config, f"tasks[0].trigger.{field}")

    def test_missing_daily_fields(self):
        for field in ("hour", "minute", "type"):
            config = copy.deepcopy(self.new)
            del config["tasks"][0]["trigger"][field]
            self.invalid(config, f"tasks[0].trigger.{field}")

    def test_unexpected_trigger_fields(self):
        self.task["trigger"]["timezone"] = "unsupported"
        self.invalid(self.new, "tasks[0].trigger")

    def test_positive_interval(self):
        self.task["trigger"] = {"type": "interval", "every_minutes": 1}
        self.assertEqual(validate_config(self.new).tasks[0].trigger.every_minutes, 1)

    def test_invalid_interval_values_and_types(self):
        for value in (0, -1, True, False, "15", 15.0, None):
            self.task["trigger"] = {"type": "interval", "every_minutes": value}
            self.invalid(self.new, "tasks[0].trigger.every_minutes")

    def test_missing_interval_value(self):
        self.task["trigger"] = {"type": "interval"}
        self.invalid(self.new, "tasks[0].trigger.every_minutes")

    def test_file_event_preserves_event_order(self):
        self.new["tasks"] = [self.email_task(trigger={"type": "file_event", "path": str(self.folder),
                                                     "events": ["deleted", "new"]})]
        self.assertEqual(validate_config(self.new).tasks[0].trigger.events, ("deleted", "new"))

    def test_invalid_file_event_lists(self):
        for events in ([], None, "new", {}, ["unknown"], [True], [[]], ["NEW"], ["new", "new"]):
            self.new["tasks"] = [self.email_task(trigger={"type": "file_event", "path": str(self.folder), "events": events})]
            self.invalid(self.new, "tasks[0].trigger.events")

    def test_missing_file_event_fields(self):
        for field in ("path", "events"):
            trigger = {"type": "file_event", "path": str(self.folder), "events": ["new"]}
            del trigger[field]
            self.new["tasks"] = [self.email_task(trigger=trigger)]
            self.invalid(self.new, f"tasks[0].trigger.{field}")

    def test_project_relative_folder_resolution(self):
        self.task["parameters"]["path"] = "watched"
        with patch("utils.paths.PROJECT_ROOT", self.root):
            self.assertEqual(validate_config(self.new).tasks[0].parameters["path"], self.folder)

    def test_absolute_folder_preserved(self):
        self.assertEqual(validate_config(self.new).tasks[0].parameters["path"], self.folder)

    def test_project_relative_file_event_folder_resolution(self):
        self.new["tasks"] = [self.email_task(trigger={"type": "file_event", "path": "watched", "events": ["new"]})]
        with patch("utils.paths.PROJECT_ROOT", self.root):
            self.assertEqual(validate_config(self.new).tasks[0].trigger.path, self.folder)

    def test_relative_definition_resolves_from_real_project_root_when_cwd_changes(self):
        self.task["enabled"] = False
        self.task["parameters"]["path"] = "missing-relative-folder"
        old_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            self.assertEqual(validate_config(self.new).tasks[0].parameters["path"],
                             PROJECT_ROOT / "missing-relative-folder")
        finally:
            os.chdir(old_cwd)

    def test_enabled_missing_folder_rejected(self):
        self.task["parameters"]["path"] = str(self.root / "missing")
        self.invalid(self.new, "tasks[0].parameters.path")

    def test_enabled_missing_file_event_folder_rejected(self):
        self.new["tasks"] = [self.email_task(trigger={"type": "file_event", "path": str(self.root / "missing"), "events": ["new"]})]
        self.invalid(self.new, "tasks[0].trigger.path")

    def test_watched_path_must_be_directory(self):
        file = self.write("{}"); self.task["parameters"]["path"] = str(file)
        self.invalid(self.new, "tasks[0].parameters.path")

    def test_invalid_path_values_rejected_even_when_disabled(self):
        self.task["enabled"] = False
        for value in (None, 1, "", " ", "invalid\x00path"):
            self.task["parameters"]["path"] = value
            self.invalid(self.new, "tasks[0].parameters.path")

    @unittest.skipUnless(os.name == "nt", "Windows path syntax is platform-specific")
    def test_invalid_windows_path_syntax_rejected_for_disabled_tasks(self):
        self.task["enabled"] = False
        for value in ("NUL", "bad*folder", "bad?folder", "bad|folder", "bad<folder", "trailing.", "trailing "):
            self.task["parameters"]["path"] = value
            self.invalid(self.new, "tasks[0].parameters.path")

    def test_duplicate_enabled_report_consumer_rejected(self):
        duplicate = copy.deepcopy(self.task)
        duplicate["id"] = "another-report"
        duplicate["parameters"]["path"] = str(self.folder / ".." / self.folder.name)
        self.new["tasks"].append(duplicate)
        self.invalid(self.new, "tasks[1].parameters.path")

    def test_disabled_report_consumer_does_not_compete(self):
        duplicate = copy.deepcopy(self.task)
        duplicate.update(id="disabled-report", enabled=False)
        self.new["tasks"].append(duplicate)
        self.assertEqual(len(validate_config(self.new).tasks), 2)

    def test_reports_for_distinct_folders_are_allowed(self):
        other = self.root / "other"
        other.mkdir()
        duplicate = copy.deepcopy(self.task)
        duplicate["id"] = "other-report"
        duplicate["parameters"]["path"] = str(other)
        self.new["tasks"].append(duplicate)
        self.assertEqual(len(validate_config(self.new).tasks), 2)

    def test_missing_task_parameters(self):
        self.task["parameters"] = {}
        self.invalid(self.new, "tasks[0].parameters.path")
        for field in ("subject", "body"):
            email = self.email_task()
            del email["parameters"][field]
            self.new["tasks"] = [email]
            self.invalid(self.new, f"tasks[0].parameters.{field}")

    def test_invalid_email_content(self):
        for field in ("subject", "body"):
            for value in (None, 1, "", " "):
                email = self.email_task()
                email["parameters"][field] = value
                self.new["tasks"] = [email]
                self.invalid(self.new, f"tasks[0].parameters.{field}")

    def test_email_subject_rejects_header_injection_but_body_allows_newlines(self):
        email = self.email_task()
        email["parameters"]["body"] = "First line\nSecond line"
        self.new["tasks"] = [email]
        self.assertIn("\n", validate_config(self.new).tasks[0].parameters["body"])
        for subject in ("Report\nBcc: injection", "Report\rInjected"):
            email["parameters"]["subject"] = subject
            self.invalid(self.new, "tasks[0].parameters.subject")

    def test_task_password_secret_and_command_fields_are_rejected(self):
        for field in ("password", "SMTP_PASSWORD", "secret", "token", "command", "python"):
            config = copy.deepcopy(self.new)
            config["tasks"][0]["parameters"][field] = "dummy-secret-do-not-echo"
            self.invalid(config, "tasks[0].parameters")
            config = copy.deepcopy(self.new)
            config["tasks"][0][field] = "dummy-secret-do-not-echo"
            self.invalid(config, "tasks[0]")

    def test_secret_in_shared_email_config_is_rejected(self):
        self.email["password"] = "dummy-secret-do-not-echo"
        self.invalid(self.new, "email")

    def test_error_does_not_expose_supplied_values_or_unknown_keys(self):
        sentinel = "dummy-sensitive-value-not-for-output"
        for config in ({**self.new, sentinel: sentinel},
                       {**self.new, "schema_version": sentinel},
                       {**self.new, "email": {**self.email, "smtp_port": sentinel}}):
            with self.assertRaises(ConfigurationError) as caught:
                validate_config(config)
            self.assertNotIn(sentinel, str(caught.exception))

    def test_unknown_root_fields(self):
        self.new["extra"] = "unsupported"
        self.invalid(self.new, "configuration")

    def test_unsupported_schema_versions(self):
        for version in (0, 1, 3, 99, True, False, "2", 2.0, None):
            self.new["schema_version"] = version
            self.invalid(self.new, "schema_version")

    def test_versioned_tasks_require_schema_version(self):
        del self.new["schema_version"]
        self.invalid(self.new, "schema_version")

    def test_mixed_legacy_and_versioned_fields_rejected(self):
        for field, value in (("watch_folder", str(self.folder)), ("schedule", {"hour": 12, "minute": 35})):
            config = copy.deepcopy(self.new)
            config[field] = value
            self.invalid(config, "cannot mix")
        self.legacy["tasks"] = []
        self.invalid(self.legacy, "cannot mix")

    def test_missing_root_fields(self):
        for field in ("email", "tasks"):
            config = copy.deepcopy(self.new)
            del config[field]
            self.invalid(config, f"configuration.{field}")
        for field in self.legacy:
            config = copy.deepcopy(self.legacy)
            del config[field]
            self.invalid(config, f"configuration.{field}")

    def test_nonobject_root_rejected(self):
        for value in (None, [], "config", 1):
            self.invalid(value, "configuration")

    def test_legacy_schedule_validation(self):
        for schedule in (None, [], {}, {"hour": True, "minute": 0}, {"hour": 24, "minute": 0},
                         {"hour": 0, "minute": 60}, {"hour": 0, "minute": 0, "extra": 1}):
            self.legacy["schedule"] = schedule
            self.invalid(self.legacy, "schedule")

    def test_legacy_missing_folder_rejected(self):
        self.legacy["watch_folder"] = str(self.root / "missing")
        self.invalid(self.legacy, "watch_folder")

    def test_shared_email_structure_validation(self):
        for email in (None, [], {}, {**self.email, "smtp_port": True}, {**self.email, "smtp_port": 0},
                      {**self.email, "smtp_port": 65536}, {**self.email, "sender": ""},
                      {**self.email, "receiver": 1}, {**self.email, "smtp_server": "server\nInjected"}):
            self.new["email"] = email
            self.invalid(self.new, "email")

    def test_missing_password_is_warning_not_structural_error(self):
        with patch.dict(os.environ):
            os.environ.pop("SMTP_PASSWORD", None)
            config = validate_config(self.new)
            self.assertIn("SMTP_PASSWORD", config.warnings[0])
            self.assertEqual(len(config.tasks), 1)

    def test_loads_json_without_rewriting_file(self):
        path = self.write(json.dumps(self.legacy))
        original = path.read_bytes()
        self.assertEqual(load_config(path), validate_config(self.new))
        self.assertEqual(path.read_bytes(), original)

    def test_reads_utf8_bom_json(self):
        path = self.write(json.dumps(self.new), encoding="utf-8-sig")
        self.assertEqual(load_config(path), validate_config(self.new))

    def test_malformed_json_has_safe_location_error(self):
        sentinel = "dummy-sensitive-source-line"
        path = self.write('{"' + sentinel + '": invalid}')
        with self.assertRaises(ConfigurationError) as caught:
            load_config(path)
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertIn("invalid JSON at line", str(caught.exception))
        self.assertNotIn(sentinel, rendered)

    def test_duplicate_json_fields_are_rejected(self):
        for text in ('{"schema_version":2,"schema_version":2}',
                     '{"email":{"password":"dummy-value","password":"dummy-value"}}'):
            path = self.write(text)
            with self.assertRaisesRegex(ConfigurationError, "duplicate JSON field"):
                load_config(path)

    def test_nonstandard_json_numbers_are_rejected(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            path = self.write('{"schema_version":' + constant + '}')
            with self.assertRaisesRegex(ConfigurationError, "nonstandard JSON"):
                load_config(path)

    def test_missing_unreadable_or_invalid_utf8_file_has_safe_error(self):
        path = self.root / "missing.json"
        with self.assertRaisesRegex(ConfigurationError, "cannot read"):
            load_config(path)
        with patch("pathlib.Path.open", side_effect=PermissionError("dummy-sensitive-file-name")):
            with self.assertRaisesRegex(ConfigurationError, "cannot read") as caught:
                load_config(path)
            self.assertNotIn("dummy-sensitive-file-name", str(caught.exception))
        path = self.root / "invalid.json"
        path.write_bytes(b"\xff")
        with self.assertRaisesRegex(ConfigurationError, "cannot read"):
            load_config(path)

    def test_default_configuration_path_and_resolution_ignore_cwd(self):
        expected = load_config()
        old_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            self.assertEqual(load_config(), expected)
            self.assertEqual(load_config(CONFIG_PATH), expected)
            self.assertEqual(resolve_project_path("watched_folder"), PROJECT_ROOT / "watched_folder")
        finally:
            os.chdir(old_cwd)

    def test_loading_is_quiet_and_has_no_worker_or_smtp_side_effects(self):
        path = self.write(json.dumps(self.new))
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            load_config(path)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
