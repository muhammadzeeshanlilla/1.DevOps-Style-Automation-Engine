import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import create_app
from api.service import EngineAPIService
from config.loader import load_config
from utils.process_manager import ProcessManager


class MonitoringJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.folder = self.root / "watched"
        self.folder.mkdir()
        self.config_path = self.root / "settings.json"
        self.initial = {
            "schema_version": 2,
            "email": {
                "sender": "sender@example.com", "receiver": "receiver@example.com",
                "smtp_server": "smtp.example.com", "smtp_port": 587,
            },
            "tasks": [{
                "id": "existing-email", "name": "Keep me", "type": "email",
                "enabled": False,
                "trigger": {"type": "interval", "every_minutes": 5},
                "parameters": {"subject": "Private", "body": "Private body"},
            }],
            "notifications": {
                "enabled": False, "notify_on_success": True, "notify_on_failure": True,
            },
        }
        self.config_path.write_text(json.dumps(self.initial), encoding="utf-8")
        self.runtime_root = self.root / "runtime"
        self.runtime_root.mkdir()
        self.manager = ProcessManager(self.runtime_root)
        self.service = EngineAPIService(
            manager=self.manager, config_loader=lambda: load_config(self.config_path),
            log_path=self.root / "engine.log", config_path=self.config_path,
        )
        self.client = TestClient(create_app(self.service), base_url="http://localhost")

    def tearDown(self):
        self.client.close()
        self.manager.release()
        self.temporary.cleanup()

    def body(self, schedule=None, enabled=True, job_id="report-one", folder=None):
        return {
            "id": job_id,
            "folder_path": str(folder or self.folder),
            "schedule": schedule or {"type": "daily", "hour": 9, "minute": 15},
            "enabled": enabled,
        }

    def create(self, **kwargs):
        return self.client.post("/api/monitoring-jobs", json=self.body(**kwargs))

    def test_valid_folder(self):
        response = self.client.post("/api/folders/validate", json={"path": str(self.folder)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "valid": True, "exists": True, "is_directory": True, "accessible": True,
        })

    def test_nonexistent_folder(self):
        data = self.client.post("/api/folders/validate",
                                json={"path": str(self.root / "missing")}).json()
        self.assertFalse(data["valid"])
        self.assertFalse(data["exists"])

    def test_file_is_not_a_directory(self):
        file_path = self.root / "file.txt"
        file_path.write_text("x", encoding="utf-8")
        data = self.client.post("/api/folders/validate", json={"path": str(file_path)}).json()
        self.assertTrue(data["exists"])
        self.assertFalse(data["is_directory"])
        self.assertFalse(data["valid"])

    def test_inaccessible_folder_returns_safe_result(self):
        with patch("api.config_store.os.scandir", side_effect=PermissionError("private")):
            response = self.client.post("/api/folders/validate", json={"path": str(self.folder)})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["valid"])
        self.assertNotIn("private", response.text)

    def test_empty_folder_request_is_rejected_without_echo(self):
        response = self.client.post("/api/folders/validate", json={"path": ""})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "Invalid API request parameters.")

    def test_create_daily_job(self):
        response = self.create()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["schedule"],
                         {"type": "daily", "hour": 9, "minute": 15, "every_minutes": None})
        self.assertEqual(response.json()["monitored_events"], ["new", "modified", "deleted"])

    def test_create_minutes_job(self):
        response = self.create(schedule={"type": "minutes", "every": 30})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["schedule"]["every_minutes"], 30)

    def test_create_hours_job_maps_to_minutes(self):
        response = self.create(schedule={"type": "hours", "every": 2})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["schedule"]["every_minutes"], 120)
        self.assertEqual(load_config(self.config_path).tasks[-1].trigger.every_minutes, 120)

    def test_duplicate_job_id_is_rejected(self):
        self.assertEqual(self.create().status_code, 201)
        self.assertEqual(self.create().status_code, 409)

    def test_duplicate_id_with_generic_task_is_rejected(self):
        response = self.create(job_id="existing-email")
        self.assertEqual(response.status_code, 409)

    def test_invalid_time_is_rejected(self):
        response = self.create(schedule={"type": "daily", "hour": 24, "minute": 0})
        self.assertEqual(response.status_code, 422)

    def test_invalid_interval_is_rejected(self):
        response = self.create(schedule={"type": "minutes", "every": 0})
        self.assertEqual(response.status_code, 422)

    def test_invalid_folder_is_rejected_for_disabled_job(self):
        response = self.create(enabled=False, folder=self.root / "missing")
        self.assertEqual(response.status_code, 400)

    def test_list_and_get_job(self):
        self.create()
        listing = self.client.get("/api/monitoring-jobs").json()
        self.assertEqual(listing["count"], 1)
        self.assertEqual(self.client.get("/api/monitoring-jobs/report-one").status_code, 200)

    def test_edit_job(self):
        second = self.root / "second"
        second.mkdir()
        self.create()
        response = self.client.put("/api/monitoring-jobs/report-one", json={
            "folder_path": str(second), "schedule": {"type": "minutes", "every": 45},
            "enabled": False,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["schedule"]["every_minutes"], 45)
        self.assertFalse(response.json()["enabled"])

    def test_enable_and_disable(self):
        self.create(enabled=False)
        enabled = self.client.patch("/api/monitoring-jobs/report-one/enabled",
                                    json={"enabled": True})
        self.assertTrue(enabled.json()["enabled"])
        disabled = self.client.patch("/api/monitoring-jobs/report-one/enabled",
                                     json={"enabled": False})
        self.assertFalse(disabled.json()["enabled"])

    def test_delete_preserves_unrelated_task(self):
        self.create()
        response = self.client.delete("/api/monitoring-jobs/report-one")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["deleted"])
        config = load_config(self.config_path)
        self.assertEqual([task.id for task in config.tasks], ["existing-email"])

    def test_missing_job(self):
        self.assertEqual(self.client.get("/api/monitoring-jobs/missing").status_code, 404)
        self.assertEqual(self.client.delete("/api/monitoring-jobs/missing").status_code, 404)

    def test_mutations_are_blocked_while_engine_owns_runtime(self):
        self.manager.acquire()
        response = self.create()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"],
                         "Stop the engine before modifying monitoring configuration.")

    def test_atomic_replace_failure_preserves_original(self):
        original = self.config_path.read_bytes()
        self.service.monitoring.replace = lambda source, destination: (
            (_ for _ in ()).throw(OSError("disk error"))
        )
        response = self.create()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.config_path.read_bytes(), original)

    def test_written_configuration_loads_and_preserves_settings(self):
        self.create()
        config = load_config(self.config_path)
        self.assertEqual(len(config.tasks), 2)
        self.assertEqual(config.email["receiver"], "receiver@example.com")
        self.assertFalse(config.notifications.enabled)

    def test_email_settings_are_safe(self):
        os.environ["SMTP_PASSWORD"] = "DO_NOT_EXPOSE"
        try:
            response = self.client.get("/api/email-settings")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["receiver"], "receiver@example.com")
            self.assertNotIn("DO_NOT_EXPOSE", response.text)
            self.assertNotIn("password", response.text.lower().replace("password_source", ""))
            self.assertEqual(self.client.put("/api/email-settings", json={}).status_code, 422)
        finally:
            os.environ.pop("SMTP_PASSWORD", None)

    def test_request_cannot_supply_password_or_hidden_fields(self):
        body = self.body()
        body["smtp_password"] = "secret"
        response = self.client.post("/api/monitoring-jobs", json=body)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("secret", response.text)

    def test_cors_allows_phase_two_mutation_methods(self):
        response = self.client.options("/api/monitoring-jobs/report-one", headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "Content-Type",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("PUT", response.headers["access-control-allow-methods"])
